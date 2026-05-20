//! Per-record cost re-derivation, ported from the Epoch Finalizer's
//! `gm_epoch_finalizer.aggregation._compute_record_costs`.
//!
//! The finalizer publishes `earnings_pdollars` and `surcharge_pdollars`
//! per aggregated `(miner_id, product)` row. Nothing in the artifact set
//! lets a validator check those numbers — `raw_hash` only proves the raw
//! records are untampered, not that the finalizer summed them correctly.
//! This module reproduces the exact integer arithmetic the finalizer
//! uses so the verifier can re-derive both totals from the raw records
//! and fail the epoch on any mismatch.
//!
//! Parity requirements (see `docs/contracts/epoch-artifacts.md`,
//! `docs/contracts/picodollar-denomination.md`, and
//! `docs/contracts/validator-log-record.md`):
//!
//! - All amounts are picodollars, integers only — no floating point.
//! - `per_token = Σ usage[dim] × price[dim] / 1_000_000`, where `/` is
//!   floor division applied **per dimension** before summing.
//! - Modifiers are integer basis points (`10_000` == 1.0×) applied
//!   sequentially in the fixed order `batch_bps`, `priority_bps`,
//!   `residency_bps`, each as `value × bps / 10_000` (floor).
//! - Surcharges are summed separately and are **never** multiplied by
//!   modifiers. Each entry is either `count × unit_pdollars` or
//!   `container_hours_bps × per_hour_pdollars / 10_000` (floor).
//! - `success == false` records contribute zero to both totals.
//! - Picodollar amounts in the JSON are decimal strings (`U64String`),
//!   token counts are JSON numbers.

use serde_json::Value;

use crate::errors::VerifierError;
use crate::record::ValidatorLogRecord;

/// Token dimensions tracked for earnings, paired with their picodollar
/// price-per-million-token field name in `miner_price.dimensions`.
///
/// Order mirrors `TOKEN_DIMENSIONS` in the finalizer's `aggregation.py`.
/// Because every per-dimension term is floored independently before
/// summing, the iteration order does not change the result — but we keep
/// it identical to the finalizer so the two implementations read the
/// same.
const TOKEN_DIMENSIONS: &[(&str, &str)] = &[
    ("input_tokens", "input_per_mtok_pdollars"),
    ("output_tokens", "output_per_mtok_pdollars"),
    ("cache_read_tokens", "cache_read_per_mtok_pdollars"),
    ("cache_write_5m_tokens", "cache_write_5m_per_mtok_pdollars"),
    ("cache_write_1h_tokens", "cache_write_1h_per_mtok_pdollars"),
    ("audio_input_tokens", "audio_input_per_mtok_pdollars"),
    ("audio_output_tokens", "audio_output_per_mtok_pdollars"),
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
        "input_per_mtok_pdollars",
        "long_context_input_per_mtok_pdollars",
    ),
    (
        "output_per_mtok_pdollars",
        "long_context_output_per_mtok_pdollars",
    ),
];

/// Picodollar denominator: dimension prices are per **million** tokens.
const PER_MTOK_DIVISOR: u128 = 1_000_000;

/// Basis-point denominator: `10_000` bps == 1.0×.
const BPS_DIVISOR: u128 = 10_000;

/// The picodollar earnings + surcharge contribution of a single record.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RecordCost {
    /// Per-token earnings after modifiers, in picodollars.
    pub earnings_pdollars: u128,
    /// Surcharge total, in picodollars (modifiers never applied).
    pub surcharge_pdollars: u128,
}

/// Re-derive the `(earnings, surcharge)` picodollar contribution of one
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
/// non-negative integer (token count, picodollar string, or basis
/// points).
pub fn compute_record_cost(record: &ValidatorLogRecord) -> Result<RecordCost, VerifierError> {
    if !record.success()? {
        return Ok(RecordCost::default());
    }

    let value = &record.value;
    let dimensions = value
        .get("miner_price")
        .and_then(|p| p.get("dimensions"))
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
        let unit_price = pdollars_of(unit_price, active_price_field)?;
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
        earnings_pdollars: per_token,
        surcharge_pdollars: surcharge,
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

/// Re-derive one surcharge entry's picodollar contribution.
///
/// Two shapes are supported, matching the finalizer:
/// - `{count, unit_pdollars}` → `count × unit_pdollars`.
/// - `{container_hours_bps, per_hour_pdollars}` →
///   `container_hours_bps × per_hour_pdollars / 10_000` (floor).
///
/// Any other shape contributes 0, as the finalizer's `else`-less branch
/// does (a non-object or unrecognised entry is silently skipped).
fn surcharge_entry(entry: &Value) -> Result<u128, VerifierError> {
    let Some(obj) = entry.as_object() else {
        return Ok(0);
    };
    if let (Some(count), Some(unit)) = (obj.get("count"), obj.get("unit_pdollars")) {
        let count = token_count_of(count)?;
        let unit = pdollars_of(unit, "surcharge.unit_pdollars")?;
        return Ok(count * unit);
    }
    if let (Some(hours_bps), Some(per_hour)) =
        (obj.get("container_hours_bps"), obj.get("per_hour_pdollars"))
    {
        let hours_bps = bps_of(hours_bps, "surcharge.container_hours_bps")?;
        let per_hour = pdollars_of(per_hour, "surcharge.per_hour_pdollars")?;
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

/// Parse a picodollar amount: a decimal string (`U64String`) per the
/// picodollar-denomination contract. `null` is treated as 0.
fn pdollars_of(value: &Value, field: &str) -> Result<u128, VerifierError> {
    if value.is_null() {
        return Ok(0);
    }
    let s = value.as_str().ok_or_else(|| {
        VerifierError::MalformedRecord(format!(
            "{field}: picodollar amount must be a decimal string, got {value}"
        ))
    })?;
    s.parse::<u128>().map_err(|e| {
        VerifierError::MalformedRecord(format!("{field}: invalid picodollar string {s:?}: {e}"))
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
        // input=1_000_000 tokens at $1/Mtok ($1 == 1e12 pUSD/Mtok).
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000",
                    "output_per_mtok_pdollars": "0"
                }},
                "usage": {"input_tokens": 1000000, "output_tokens": 0},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_pdollars, 1_000_000_000_000);
        assert_eq!(cost.surcharge_pdollars, 0);
    }

    #[test]
    fn contract_worked_example() {
        // The validator-log-record.md worked example: 812/1456/1200
        // tokens at the documented prices => 22_993_600_000 pUSD.
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "2800000000000",
                    "output_per_mtok_pdollars": "14000000000000",
                    "cache_read_per_mtok_pdollars": "280000000000"
                }},
                "usage": {
                    "input_tokens": 812,
                    "output_tokens": 1456,
                    "cache_read_tokens": 1200,
                    "long_context_tier": false
                },
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_pdollars, 22_993_600_000);
    }

    #[test]
    fn batch_modifier_halves_earnings() {
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000"
                }},
                "usage": {"input_tokens": 1000000},
                "modifiers": {"batch_bps": 5000},
                "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_pdollars, 500_000_000_000);
    }

    #[test]
    fn modifiers_apply_sequentially_in_fixed_order() {
        // 1_000_000 tokens at $1/Mtok = 1e12. batch 7777 then priority
        // 3333: ((1e12 * 7777 / 10000) * 3333 / 10000).
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000"
                }},
                "usage": {"input_tokens": 1000000},
                "modifiers": {"priority_bps": 3333, "batch_bps": 7777},
                "surcharges": {}
            }"#,
        );
        let after_batch = 1_000_000_000_000_u128 * 7777 / 10_000;
        let expected = after_batch * 3333 / 10_000;
        assert_eq!(cost.earnings_pdollars, expected);
    }

    #[test]
    fn surcharge_not_multiplied_by_modifier() {
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000"
                }},
                "usage": {"input_tokens": 1000000},
                "modifiers": {"batch_bps": 5000},
                "surcharges": {"anthropic_web_search": {
                    "count": 3, "unit_pdollars": "10000000000000"
                }}
            }"#,
        );
        assert_eq!(cost.earnings_pdollars, 500_000_000_000);
        assert_eq!(cost.surcharge_pdollars, 30_000_000_000_000);
    }

    #[test]
    fn container_hours_surcharge() {
        // 12_500 bps (1.25h) * 4_000_000_000 pUSD/h / 10_000.
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {}},
                "usage": {},
                "modifiers": {},
                "surcharges": {"container": {
                    "container_hours_bps": 12500,
                    "per_hour_pdollars": "4000000000"
                }}
            }"#,
        );
        assert_eq!(
            cost.surcharge_pdollars,
            12_500_u128 * 4_000_000_000 / 10_000
        );
    }

    #[test]
    fn failed_record_bills_zero_despite_usage() {
        let cost = cost_of(
            r#"{
                "success": false,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000"
                }},
                "usage": {"input_tokens": 999999},
                "modifiers": {},
                "surcharges": {"x": {"count": 9, "unit_pdollars": "999"}}
            }"#,
        );
        assert_eq!(cost, RecordCost::default());
    }

    #[test]
    fn long_context_tier_swaps_price() {
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000",
                    "long_context_input_per_mtok_pdollars": "2000000000000"
                }},
                "usage": {"input_tokens": 1000000, "long_context_tier": true},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_pdollars, 2_000_000_000_000);
    }

    #[test]
    fn long_context_tier_falls_back_when_override_absent() {
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000"
                }},
                "usage": {"input_tokens": 1000000, "long_context_tier": true},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_pdollars, 1_000_000_000_000);
    }

    #[test]
    fn unpriced_dimension_contributes_zero() {
        let cost = cost_of(
            r#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": "1000000000000"
                }},
                "usage": {"input_tokens": 1000000, "audio_input_tokens": 5000},
                "modifiers": {}, "surcharges": {}
            }"#,
        );
        assert_eq!(cost.earnings_pdollars, 1_000_000_000_000);
    }

    #[test]
    fn rejects_picodollar_json_number() {
        let record = parse_record(
            br#"{
                "success": true,
                "miner_price": {"dimensions": {
                    "input_per_mtok_pdollars": 1000000000000
                }},
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
}
