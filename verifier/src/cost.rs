//! Per-record cost re-derivation, ported from the Epoch Finalizer's
//! `gm_epoch_finalizer.aggregation._compute_record_costs`.
//!
//! The finalizer publishes `earnings_ndollars` and `surcharge_ndollars`
//! per aggregated `(miner_id, product)` row. Nothing in the artifact set
//! lets a validator check those numbers — `raw_hash` only proves the raw
//! records are untampered, not that the finalizer summed them correctly.
//! This module reproduces the exact integer arithmetic the finalizer
//! uses so the verifier can re-derive both totals from the raw records
//! and fail the epoch on any mismatch.
//!
//! The miner-payout math mirrors the gateway's per-dimension
//! settlement under the pct-discount pricing model
//! (`docs/plans/miner-pct-discount-pricing.md` §4 + §9): the gateway
//! materialises the post-discount per-Mtok prices onto every record as
//! `effective_price_ndollars`, and this module simply re-derives
//! `Σ floor(usage[dim] × effective_price[dim] / 1_000_000)` from that
//! block. There is no separate miner-price block: the field
//! `miner_discount_bp` is carried for transparency / audit only, while
//! `effective_price_ndollars` is the byte-for-byte source of truth for
//! payout.
//!
//! Parity requirements (see `docs/contracts/epoch-artifacts.md`,
//! `docs/contracts/nano-dollar-denomination.md`, and
//! `docs/contracts/validator-log-record.md`):
//!
//! - All amounts are nano-dollars, integers only — no floating point.
//! - `per_token = Σ usage[dim] × effective_price[dim] / 1_000_000`,
//!   where `/` is floor division applied **per dimension** before
//!   summing.
//! - Modifiers are integer basis points (`10_000` == 1.0×) applied
//!   sequentially in the fixed order `batch_bps`, `priority_bps`,
//!   `residency_bps`, each as `value × bps / 10_000` (floor).
//! - Surcharges are summed separately and are **never** multiplied by
//!   modifiers. Each entry is either `count × unit_ndollars` or
//!   `container_hours_bps × per_hour_ndollars / 10_000` (floor).
//! - `success == false` records contribute zero to both totals.
//! - All nano-dollar amounts and token counts are JSON numbers (`U64`).

use serde_json::Value;

use crate::errors::VerifierError;
use crate::record::ValidatorLogRecord;

/// Token dimensions tracked for earnings, paired with their nano-dollar
/// price-per-million-token field name in `effective_price_ndollars`.
///
/// Order mirrors `TOKEN_DIMENSIONS` in the finalizer's `aggregation.py`.
/// Because every per-dimension term is floored independently before
/// summing, the iteration order does not change the result — but we keep
/// it identical to the finalizer so the two implementations read the
/// same.
const TOKEN_DIMENSIONS: &[(&str, &str)] = &[
    ("input_tokens", "input_per_mtok_ndollars"),
    ("output_tokens", "output_per_mtok_ndollars"),
    ("cache_read_tokens", "cache_read_per_mtok_ndollars"),
    ("cache_write_5m_tokens", "cache_write_5m_per_mtok_ndollars"),
    ("cache_write_1h_tokens", "cache_write_1h_per_mtok_ndollars"),
    ("audio_input_tokens", "audio_input_per_mtok_ndollars"),
    ("audio_output_tokens", "audio_output_per_mtok_ndollars"),
];

/// Modifier basis-point fields, applied to `per_token` in this exact
/// order. Mirrors the `("batch_bps", "priority_bps", "residency_bps")`
/// tuple in the finalizer. Order matters: integer floor division is not
/// associative, so the verifier must apply modifiers in the same
/// sequence the finalizer does.
const MODIFIER_FIELDS: &[&str] = &["batch_bps", "priority_bps", "residency_bps"];

/// Long-context-tier price overrides: when `usage.long_context_tier` is
/// true and the override field is present on the product, the override
/// price replaces the base price for that dimension.
const LONG_CONTEXT_OVERRIDES: &[(&str, &str)] = &[
    (
        "input_per_mtok_ndollars",
        "long_context_input_per_mtok_ndollars",
    ),
    (
        "output_per_mtok_ndollars",
        "long_context_output_per_mtok_ndollars",
    ),
];

/// nano-dollar denominator: dimension prices are per **million** tokens.
const PER_MTOK_DIVISOR: u128 = 1_000_000;

/// Basis-point denominator: `10_000` bps == 1.0×.
const BPS_DIVISOR: u128 = 10_000;

/// The nano-dollar earnings + surcharge contribution of a single record.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RecordCost {
    /// Per-token earnings after modifiers, in nano-dollars.
    pub earnings_ndollars: u128,
    /// Surcharge total, in nano-dollars (modifiers never applied).
    pub surcharge_ndollars: u128,
}

/// Re-derive the `(earnings, surcharge)` nano-dollar contribution of one
/// record, matching the finalizer's `_compute_record_costs`.
///
/// A `success == false` record bills zero on both axes, regardless of
/// any usage a buggy gateway may have emitted on it — `success` is the
/// load-bearing field, exactly as the finalizer treats it.
///
/// # Errors
///
/// Returns [`VerifierError::MalformedRecord`] when `success` is missing,
/// or when a numeric field is present but cannot be parsed as a
/// non-negative integer (token count, nano-dollar amount, or basis
/// points).
pub fn compute_record_cost(record: &ValidatorLogRecord) -> Result<RecordCost, VerifierError> {
    if !record.success()? {
        return Ok(RecordCost::default());
    }

    let value = &record.value;
    let dimensions = value
        .get("effective_price_ndollars")
        .and_then(Value::as_object);
    let usage = value.get("usage").and_then(Value::as_object);

    let long_context = usage
        .and_then(|u| u.get("long_context_tier"))
        .and_then(Value::as_bool)
        .unwrap_or(false);

    let mut per_token: u128 = 0;
    for (token_field, price_field) in TOKEN_DIMENSIONS {
        let token_count = usage
            .and_then(|u| u.get(*token_field))
            .map_or(Ok(0), token_count_of)?;
        if token_count == 0 {
            continue;
        }
        let Some(dimensions) = dimensions else {
            // Token counts present but no priced dimensions: the gateway
            // should never emit this. The finalizer treats a missing
            // price as 0; do the same to stay byte-for-byte aligned.
            continue;
        };
        let active_price_field = resolve_price_field(price_field, long_context, dimensions);
        let Some(unit_price) = dimensions.get(active_price_field) else {
            // Dimension not priced on this product — finalizer skips it.
            continue;
        };
        let unit_price = ndollars_of(unit_price, active_price_field)?;
        per_token += token_count * unit_price / PER_MTOK_DIVISOR;
    }

    let modifiers = value.get("modifiers").and_then(Value::as_object);
    for field in MODIFIER_FIELDS {
        let Some(bps) = modifiers.and_then(|m| m.get(*field)) else {
            continue;
        };
        let bps = bps_of(bps, field)?;
        per_token = per_token * bps / BPS_DIVISOR;
    }

    let mut surcharge: u128 = 0;
    if let Some(surcharges) = value.get("surcharges").and_then(Value::as_object) {
        for entry in surcharges.values() {
            surcharge += surcharge_entry(entry)?;
        }
    }

    Ok(RecordCost {
        earnings_ndollars: per_token,
        surcharge_ndollars: surcharge,
    })
}

/// Pick the active price field for a dimension, swapping in the
/// long-context override when the tier is set and the override is priced
/// on this product. Mirrors `LONG_CONTEXT_OVERRIDES` handling in the
/// finalizer: a missing/`null` override falls back to the base field.
fn resolve_price_field<'a>(
    base_field: &'a str,
    long_context: bool,
    dimensions: &serde_json::Map<String, Value>,
) -> &'a str {
    if !long_context {
        return base_field;
    }
    for (base, override_field) in LONG_CONTEXT_OVERRIDES {
        if *base == base_field
            && dimensions
                .get(*override_field)
                .is_some_and(|v| !v.is_null())
        {
            return override_field;
        }
    }
    base_field
}

/// Re-derive one surcharge entry's nano-dollar contribution.
///
/// Two shapes are supported, matching the finalizer:
/// - `{count, unit_ndollars}` → `count × unit_ndollars`.
/// - `{container_hours_bps, per_hour_ndollars}` →
///   `container_hours_bps × per_hour_ndollars / 10_000` (floor).
///
/// Any other shape contributes 0, as the finalizer's `else`-less branch
/// does (a non-object or unrecognised entry is silently skipped).
fn surcharge_entry(entry: &Value) -> Result<u128, VerifierError> {
    let Some(obj) = entry.as_object() else {
        return Ok(0);
    };
    if let (Some(count), Some(unit)) = (obj.get("count"), obj.get("unit_ndollars")) {
        let count = token_count_of(count)?;
        let unit = ndollars_of(unit, "surcharge.unit_ndollars")?;
        return Ok(count * unit);
    }
    if let (Some(hours_bps), Some(per_hour)) =
        (obj.get("container_hours_bps"), obj.get("per_hour_ndollars"))
    {
        let hours_bps = bps_of(hours_bps, "surcharge.container_hours_bps")?;
        let per_hour = ndollars_of(per_hour, "surcharge.per_hour_ndollars")?;
        return Ok(hours_bps * per_hour / BPS_DIVISOR);
    }
    Ok(0)
}

/// Parse a token count: a JSON number per the schema. `null` is treated
/// as 0 (the finalizer's `usage.get(...) or 0`).
fn token_count_of(value: &Value) -> Result<u128, VerifierError> {
    if value.is_null() {
        return Ok(0);
    }
    value.as_u64().map(u128::from).ok_or_else(|| {
        VerifierError::MalformedRecord(format!(
            "expected a non-negative integer token count, got {value}"
        ))
    })
}

/// Parse a nano-dollar amount: a JSON number (`U64`) per the nano-dollar
/// denomination contract. `null` is treated as 0.
fn ndollars_of(value: &Value, field: &str) -> Result<u128, VerifierError> {
    if value.is_null() {
        return Ok(0);
    }
    value.as_u64().map(u128::from).ok_or_else(|| {
        VerifierError::MalformedRecord(format!(
            "{field}: nano-dollar amount must be a non-negative integer, got {value}"
        ))
    })
}

/// Parse a basis-points value: a JSON number. `null` is treated as
/// absent by the caller; reaching here means the value was present.
fn bps_of(value: &Value, field: &str) -> Result<u128, VerifierError> {
    value.as_u64().map(u128::from).ok_or_else(|| {
        VerifierError::MalformedRecord(format!(
            "{field}: basis points must be a non-negative integer, got {value}"
        ))
    })
}

#[cfg(test)]
mod tests {
    #![expect(clippy::expect_used, reason = "test code")]

    use super::*;
    use crate::record::parse_record;

    fn cost_of(json: &str) -> RecordCost {
        let record = parse_record(json.as_bytes()).expect("parse record");
        compute_record_cost(&record).expect("compute cost")
    }

    #[test]
    fn per_token_floor_division_per_dimension() {
        // input=1_000_000 tokens at $1/Mtok ($1 == 1e12 nUSD/Mtok).
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000,
                    "output_per_mtok_ndollars": 0
                },
                "usage": {"input_tokens": 1000000, "output_tokens": 0},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 1_000_000_000);
        assert_eq!(cost.surcharge_ndollars, 0);
    }

    #[test]
    fn per_token_floors_each_dimension_before_summing() {
        // Two dimensions, each with a sub-1e6 product (1 token ×
        // 999_999 nUSD/Mtok). Per-dimension floor: 0 + 0 = 0.
        // Summing first then dividing would give 1 (floor) or 2 (ceil);
        // this fixes the convention shared with the gateway's
        // `money::settle` and the Finalizer's `_compute_record_costs`.
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 999,
                    "output_per_mtok_ndollars": 999
                },
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 0);
    }

    #[test]
    fn modifier_floors_on_non_divisible_product() {
        // 7 tokens × 1e6 nUSD/Mtok = 7 nUSD. batch 3333 bps:
        // 7 × 3333 / 10_000 = 23331 / 10000 = 2 (floor), not 3 (ceil).
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000
                },
                "usage": {"input_tokens": 7},
                "modifiers": {"batch_bps": 3333},
                "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 2);
    }

    #[test]
    fn contract_worked_example() {
        // The validator-log-record.md worked example: 812/1456/1200
        // tokens at the documented prices => 22_993_600_000 nUSD.
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 2800000000,
                    "output_per_mtok_ndollars": 14000000000,
                    "cache_read_per_mtok_ndollars": 280000000
                },
                "usage": {
                    "input_tokens": 812,
                    "output_tokens": 1456,
                    "cache_read_tokens": 1200,
                    "long_context_tier": false
                },
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 22_993_600);
    }

    #[test]
    fn batch_modifier_halves_earnings() {
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000
                },
                "usage": {"input_tokens": 1000000},
                "modifiers": {"batch_bps": 5000},
                "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 500_000_000);
    }

    #[test]
    fn modifiers_apply_sequentially_in_fixed_order() {
        // 1_000_000 tokens at $1/Mtok = 1e12. batch 7777 then priority
        // 3333: ((1e12 * 7777 / 10000) * 3333 / 10000).
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000
                },
                "usage": {"input_tokens": 1000000},
                "modifiers": {"priority_bps": 3333, "batch_bps": 7777},
                "surcharges": {}
            }"#,
        );
        let after_batch = 1_000_000_000_u128 * 7777 / 10_000;
        let expected = after_batch * 3333 / 10_000;
        assert_eq!(cost.earnings_ndollars, expected);
    }

    #[test]
    fn surcharge_not_multiplied_by_modifier() {
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000
                },
                "usage": {"input_tokens": 1000000},
                "modifiers": {"batch_bps": 5000},
                "surcharges": {"anthropic_web_search": {
                    "count": 3, "unit_ndollars": 10000000000
                }}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 500_000_000);
        assert_eq!(cost.surcharge_ndollars, 30_000_000_000);
    }

    #[test]
    fn container_hours_surcharge() {
        // 12_500 bps (1.25h) * 4_000_000 nUSD/h / 10_000.
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {},
                "usage": {},
                "modifiers": {},
                "surcharges": {"container": {
                    "container_hours_bps": 12500,
                    "per_hour_ndollars": 4000000
                }}
            }"#,
        );
        assert_eq!(cost.surcharge_ndollars, 12_500_u128 * 4_000_000 / 10_000);
    }

    #[test]
    fn failed_record_bills_zero_despite_usage() {
        let cost = cost_of(
            r#"{
                "success": false,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000
                },
                "usage": {"input_tokens": 999999},
                "modifiers": {},
                "surcharges": {"x": {"count": 9, "unit_ndollars": 0}}
            }"#,
        );
        assert_eq!(cost, RecordCost::default());
    }

    #[test]
    fn long_context_tier_swaps_price() {
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000,
                    "long_context_input_per_mtok_ndollars": 2000000000
                },
                "usage": {"input_tokens": 1000000, "long_context_tier": true},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 2_000_000_000);
    }

    #[test]
    fn long_context_tier_falls_back_when_override_absent() {
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000
                },
                "usage": {"input_tokens": 1000000, "long_context_tier": true},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 1_000_000_000);
    }

    #[test]
    fn unpriced_dimension_contributes_zero() {
        let cost = cost_of(
            r#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": 1000000000
                },
                "usage": {"input_tokens": 1000000, "audio_input_tokens": 5000},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_ndollars, 1_000_000_000);
    }

    #[test]
    fn rejects_ndollar_decimal_string() {
        // Wire format is JSON Number, not string — a string-form value is
        // a malformed record under the post-cutover contract.
        let record = parse_record(
            br#"{
                "success": true,
                "effective_price_ndollars": {
                    "input_per_mtok_ndollars": "1000000000"
                },
                "usage": {"input_tokens": 1000000},
                "modifiers": {}, "surcharges": {}
            }"#,
        )
        .expect("parse");
        assert!(matches!(
            compute_record_cost(&record),
            Err(VerifierError::MalformedRecord(_))
        ));
    }

    /// At a non-round discount (777 bp = 7.77%) the per-dimension
    /// effective prices floor independently. The verifier's earnings
    /// re-derivation against the materialised `effective_price_ndollars`
    /// block must equal a hand-computed
    /// `Σ floor(T_X × floor(R_X × (10_000 − bp) / 10_000) / 1_000_000)`,
    /// which is exactly the byte-for-byte invariant the gateway test
    /// `miner_payout_matches_per_dimension_finalizer_recompute`
    /// (`gateway/src/money/settle.rs`) enforces on the producer side.
    ///
    /// The retail block (Sonnet-class: $3/$15 input/output, $0.30/Mtok
    /// `cache_read`, $3.75/Mtok `cache_write_5m`) and usage counts match
    /// the gateway test exactly so any drift between the two sides
    /// surfaces here.
    #[test]
    fn earnings_match_per_dimension_finalizer_recompute_at_non_round_discount() {
        // Retail per-Mtok prices (nUSD); same numbers as the gateway's
        // `sonnet_retail()` post-PR-D.
        let r_in: u128 = 3_000_000_000;
        let r_out: u128 = 15_000_000_000;
        let r_cache_read: u128 = 300_000_000;
        let r_cache_write_5m: u128 = 3_750_000_000;
        let bp: u128 = 777;
        let factor: u128 = 10_000 - bp;
        let apply = |r: u128| r * factor / 10_000;
        let eff_in = apply(r_in);
        let eff_out = apply(r_out);
        let eff_cache_read = apply(r_cache_read);
        let eff_cache_write_5m = apply(r_cache_write_5m);

        let t_in: u128 = 1357;
        let t_out: u128 = 2468;
        let t_cache_read: u128 = 369;
        let t_cache_write_5m: u128 = 112;

        let expected = t_in * eff_in / 1_000_000
            + t_out * eff_out / 1_000_000
            + t_cache_read * eff_cache_read / 1_000_000
            + t_cache_write_5m * eff_cache_write_5m / 1_000_000;

        let cost = cost_of(&format!(
            r#"{{
                "success": true,
                "effective_price_ndollars": {{
                    "input_per_mtok_ndollars": {eff_in},
                    "output_per_mtok_ndollars": {eff_out},
                    "cache_read_per_mtok_ndollars": {eff_cache_read},
                    "cache_write_5m_per_mtok_ndollars": {eff_cache_write_5m}
                }},
                "usage": {{
                    "input_tokens": {t_in},
                    "output_tokens": {t_out},
                    "cache_read_tokens": {t_cache_read},
                    "cache_write_5m_tokens": {t_cache_write_5m}
                }},
                "modifiers": {{}}, "surcharges": {{}}
            }}"#,
        ));
        assert_eq!(cost.earnings_ndollars, expected);
    }
}
