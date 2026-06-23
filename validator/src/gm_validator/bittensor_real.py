"""Real bittensor submitter — submits weights to the subtensor chain.

This wraps the ``bittensor`` SDK (v10). It builds the validator hotkey
keypair in memory from a seed, connects to a subtensor endpoint, and
calls ``subtensor.set_weights`` once per epoch. The validator's hot-path
code never imports ``bittensor`` directly — it talks to the ``Submitter``
protocol in ``bittensor_adapter`` — so the test suite (which uses
``MockSubmitter``) does not pull in the heavy dependency.

The ``bittensor`` import is deferred to the methods that need it so that
merely importing this module is cheap; only constructing ``RealSubmitter``
or calling ``submit`` requires the SDK to be installed.

The hotkey comes from one of two sources. By default it is loaded
entirely in memory from ``BITTENSOR_HOTKEY_SEED`` (a BIP-39 mnemonic or
a ``0x``-prefixed hex seed) — no keyfile touched. Alternatively, an
operator who already keeps a btcli wallet on disk can set ``WALLET_NAME``
+ ``WALLET_HOTKEY`` (+ optional ``WALLET_PATH``) to import the hotkey
keypair from ``{path}/{name}/hotkeys/{hotkey}``; the wallet takes
precedence over the seed when both are set.

Connection lifecycle:

The submitter holds ONE long-lived ``Subtensor`` for the life of the
process — the same websocket the blockmachine validator runs unchanged
for 9+ days. A submission failure does NOT drop the connection: dropping
and reopening the websocket on every failed or rejected submit leaks
subscriptions inside the SDK and OOMs the pod in minutes. Instead a
``_consecutive_failures`` counter reconnects only after three consecutive
connection-level (raised-exception) failures, mirroring the bm validator's
``_maybe_reconnect``; a clean success resets it to zero.

Submit contract:

``set_weights`` blocks for inclusion *and* finalization
(``wait_for_inclusion=True, wait_for_finalization=True``), exactly like
the bm validator. The SDK signs, submits, and waits for the chain's real
accept/reject receipt before returning, so a rejected or pool-dropped
vector surfaces here instead of being silently lost. Under commit-reveal
the wait covers the timelocked *commit* landing on-chain; the chain
reveals it automatically once the drand round elapses.

The wait is deliberately NOT wrapped in ``run_with_timeout``. Bounding
the blocking wait is exactly what leaks SDK subscriptions: a fired
timeout abandons (does not cancel) the worker thread, leaving its
per-submit block-event subscription alive on the shared long-lived
socket, and over multi-hour runs those accumulate and OOM the pod. The
bm validator avoids the leak by never bounding the wait — the SDK tears
its own subscription down when the extrinsic finalizes — and recreating
the socket only after three consecutive raised failures
(``_maybe_reconnect``). We follow that same contract. The cost of an
unbounded wait is that a genuinely wedged websocket blocks the loop
until the kubelet liveness probe restarts the pod; that is the bm
trade-off, accepted here in exchange for real inclusion receipts and no
leaked subscriptions.

The submit uses the SDK default ``raise_error=False``, so substrate
errors come back as an ``ExtrinsicResponse`` (unpacks as
``(success, message)``) rather than a raised exception. Two outcomes
drive the lifecycle:

- A raised exception is a connection-level failure (dead websocket, RPC
  transport error). It increments the failure counter so the connection
  is recreated once three pile up, and surfaces as
  ``WeightSubmissionError``.
- A ``(False, message)`` return is a chain answer, so the socket is
  healthy: it neither resets nor increments the counter, and surfaces as
  ``WeightSubmissionError`` so the validator loop retries the epoch on the
  next tick. This covers a chain rejection (weight-set rate limit, hotkey
  not registered, stale version key) as well as the chain's pre-flight
  checks.

This counts only raised exceptions toward reconnect, exactly like the bm
validator that has run this contract for 9+ days. The SDK can also wrap a
transport failure into a ``(False, message)`` return, and its ``.error``
field is populated for ordinary chain rejections as well as transport
errors — so there is no reliable way to tell the two apart from the
response without coupling to SDK-internal exception types. We accept the
bm trade-off rather than reintroduce that coupling: the websocket-idle
close that actually occurs in practice raises, and a persistently dead
endpoint still surfaces through the startup connect on the kubelet
restart.

Like the bm validator, there is no "already submitted" short-circuit. A
``(False, "...Already Imported...")`` return carries no inclusion receipt —
it can be the transaction pool rejecting a still-pending duplicate that
may yet be dropped — so it is treated as a plain failure and retried.
Re-submitting the identical weight vector is idempotent, and a genuinely
landed epoch is retired by the superseded-epoch sweep once newer epochs
finalize, so retrying never double-submits or loops forever.
"""

from __future__ import annotations

import logging
from typing import Any

from gm_validator.bittensor_adapter import ValidatorWeightStatus
from gm_validator.subtensor_connect import (
    DEFAULT_CONNECT_TIMEOUT_SECS,
    DEFAULT_RPC_TIMEOUT_SECS,
    run_with_timeout,
)

LOGGER = logging.getLogger(__name__)


# Connection-level failures tolerated before recreating the websocket.
# A single failure never reconnects; only sustained failure does, which
# avoids the per-error reconnect churn that leaked subscriptions.
_RECONNECT_AFTER_FAILURES = 3


class WeightSubmissionError(RuntimeError):
    """The subtensor rejected a ``set_weights`` extrinsic."""


class HotkeyConfigError(RuntimeError):
    """The hotkey seed was missing or could not be parsed into a keypair."""


def _keypair_from_seed(seed: str) -> Any:
    """Build a ``bittensor.Keypair`` in memory from a seed.

    Accepts either a BIP-39 mnemonic (space-separated words) or a
    ``0x``-prefixed hex seed. Nothing is read from or written to disk.

    Args:
        seed: ``BITTENSOR_HOTKEY_SEED`` — a mnemonic or ``0x`` hex seed.

    Returns:
        A ``bittensor.Keypair`` carrying the signing key.

    Raises:
        HotkeyConfigError: ``seed`` is empty, or neither a valid mnemonic
            nor a valid ``0x`` hex seed.
    """
    import bittensor

    candidate = seed.strip()
    if not candidate:
        raise HotkeyConfigError(
            "BITTENSOR_HOTKEY_SEED is empty; provide the validator hotkey "
            "seed as a BIP-39 mnemonic or a 0x-prefixed hex seed"
        )
    try:
        if candidate.startswith("0x"):
            return bittensor.Keypair.create_from_seed(candidate)
        return bittensor.Keypair.create_from_mnemonic(candidate)
    except Exception as exc:
        raise HotkeyConfigError(
            "BITTENSOR_HOTKEY_SEED is not a valid BIP-39 mnemonic or 0x-prefixed hex seed"
        ) from exc


def _keypair_from_wallet(name: str, hotkey: str, path: str | None) -> Any:
    """Load a ``bittensor.Keypair`` from an on-disk wallet keyfile.

    Reads the hotkey keypair from ``{path}/{name}/hotkeys/{hotkey}``; when
    ``path`` is ``None`` bittensor's default (``~/.bittensor/wallets``) is
    used. The hotkey keyfile is normally unencrypted, so this does not
    prompt for a password.

    Args:
        name: Wallet (coldkey) name — ``WALLET_NAME``.
        hotkey: Hotkey name within the wallet — ``WALLET_HOTKEY``.
        path: Wallets directory — ``WALLET_PATH`` (``None`` → SDK default).

    Returns:
        A ``bittensor.Keypair`` carrying the signing key.

    Raises:
        HotkeyConfigError: The wallet/hotkey could not be loaded (missing
            keyfile, encrypted hotkey, bad path, etc.).
    """
    import bittensor

    try:
        wallet = (
            bittensor.Wallet(name=name, hotkey=hotkey, path=path)
            if path
            else bittensor.Wallet(name=name, hotkey=hotkey)
        )
        return wallet.hotkey
    except Exception as exc:
        raise HotkeyConfigError(
            f"could not load validator hotkey from wallet name={name!r} "
            f"hotkey={hotkey!r} path={path or '~/.bittensor/wallets'}: {exc}"
        ) from exc


def _load_keypair(
    hotkey_seed: str | None,
    wallet_name: str | None,
    wallet_hotkey: str | None,
    wallet_path: str | None,
) -> Any:
    """Resolve the validator signing keypair from a wallet or a raw seed.

    A configured wallet (both ``wallet_name`` and ``wallet_hotkey`` set)
    takes precedence over the seed.

    Raises:
        HotkeyConfigError: Neither a usable wallet nor a usable seed is
            configured, or the chosen source fails to load.
    """
    if wallet_name and wallet_hotkey:
        return _keypair_from_wallet(wallet_name, wallet_hotkey, wallet_path)
    if hotkey_seed and hotkey_seed.strip():
        return _keypair_from_seed(hotkey_seed)
    raise HotkeyConfigError(
        "no validator hotkey configured: set WALLET_NAME and WALLET_HOTKEY to "
        "import an on-disk wallet, or BITTENSOR_HOTKEY_SEED for an in-memory seed"
    )


class _KeypairWallet:
    """Shim wrapping a bare ``bittensor.Keypair`` as a wallet.

    ``subtensor.set_weights`` expects a wallet object exposing ``.hotkey``
    and (on newer bittensor versions) ``unlock_hotkey()``. The hotkey is
    already in memory, so ``unlock_hotkey`` is a no-op and no keyfile is
    ever read from disk.
    """

    def __init__(self, hotkey: Any) -> None:
        self.hotkey = hotkey

    def unlock_hotkey(self) -> None:
        """No-op: the hotkey is already loaded in memory."""


class RealSubmitter:
    """Real bittensor weight submitter.

    Builds the hotkey keypair in memory and opens a single subtensor
    connection at construction time so that a malformed seed or
    unreachable endpoint fails fast on startup rather than on the first
    finalized epoch. That connection is held for the life of the process
    and recreated only after sustained connection failures — a single
    failed submit reuses the existing websocket.
    """

    def __init__(
        self,
        netuid: int,
        endpoint: str | None,
        hotkey_seed: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECS,
        rpc_timeout: float = DEFAULT_RPC_TIMEOUT_SECS,
        *,
        wallet_name: str | None = None,
        wallet_hotkey: str | None = None,
        wallet_path: str | None = None,
    ) -> None:
        """Build the signing keypair and connect to subtensor.

        The keypair comes from one of two sources: a configured on-disk
        wallet (``wallet_name`` + ``wallet_hotkey``) takes precedence,
        otherwise the in-memory ``hotkey_seed``.

        Args:
            netuid: Subnet id the validator submits weights for.
            endpoint: Subtensor websocket URL; ``None`` selects the SDK
                default network.
            hotkey_seed: The validator hotkey seed — a BIP-39 mnemonic or
                a ``0x``-prefixed hex seed. The signing keypair is built
                in memory from it; no keyfile is read from disk. Optional
                when a wallet is configured.
            wallet_name: Wallet name to import the hotkey from instead of a
                seed (``WALLET_NAME``); requires ``wallet_hotkey``.
            wallet_hotkey: Hotkey name within ``wallet_name``
                (``WALLET_HOTKEY``).
            wallet_path: Wallets directory for the import (``WALLET_PATH``);
                ``None`` uses bittensor's ``~/.bittensor/wallets``.
            connect_timeout: Per-attempt wall-clock budget for opening the
                subtensor websocket; a hung connect becomes a retryable
                timeout instead of freezing startup.
            rpc_timeout: Per-call wall-clock budget for a chain RPC over
                the open socket (head read, submit); a hung call becomes a
                ``TimeoutError`` that counts toward reconnect instead of
                freezing the loop.

        Raises:
            HotkeyConfigError: The seed is missing or malformed.
            WeightSubmissionError: The subtensor endpoint could not be
                reached.
        """
        self._netuid = netuid
        self._endpoint = endpoint
        self._connect_timeout = connect_timeout
        self._rpc_timeout = rpc_timeout
        self._subtensor: Any | None = None
        self._consecutive_failures = 0
        hotkey = _load_keypair(hotkey_seed, wallet_name, wallet_hotkey, wallet_path)
        self._wallet: Any = _KeypairWallet(hotkey)
        hotkey_ss58 = hotkey.ss58_address
        try:
            self._subtensor = self._open_subtensor()
        except Exception as exc:
            raise WeightSubmissionError(
                f"failed to connect to subtensor (endpoint={endpoint}): {exc}"
            ) from exc
        # Cache the weight-set rate limit once at startup — it's a subnet
        # hyperparameter that rarely changes, and reading it per tick would
        # add an RPC just to pre-gate a submit. A failed read leaves it 0,
        # which disables the local pre-gate so the chain's own gate decides
        # (the SDK re-checks the live rate limit inside set_weights anyway).
        self._weights_rate_limit = self._read_weights_rate_limit()
        LOGGER.info(
            "RealSubmitter ready: netuid=%d hotkey=%s endpoint=%s "
            "weights_rate_limit=%d (in-memory keypair)",
            netuid,
            hotkey_ss58,
            endpoint or "<default>",
            self._weights_rate_limit,
        )

    def _read_weights_rate_limit(self) -> int:
        """Best-effort read of the subnet's weight-set rate limit (blocks).

        Returns 0 when unreadable so the caller's local pre-gate is disabled
        and the chain remains the sole rate-limit authority.
        """
        subtensor = self._subtensor
        if subtensor is None:
            return 0
        try:
            value = run_with_timeout(
                "subtensor weights_rate_limit",
                lambda: subtensor.weights_rate_limit(self._netuid),
                self._rpc_timeout,
            )
            return int(value or 0)
        except Exception as exc:  # noqa: BLE001 — observability-only; never block startup.
            LOGGER.warning("could not read weights_rate_limit: %s (pre-gate disabled)", exc)
            return 0

    def _open_subtensor(self) -> Any:
        """Open a fresh subtensor connection at the configured endpoint.

        Routes through ``connect_subtensor`` so both the construction-time
        connect and the rare post-failure reconnect retry transient
        endpoint failures (HTTP 429 from public testnet RPCs in
        particular) instead of crash-looping the pod.
        """
        from gm_validator.subtensor_connect import connect_subtensor

        return connect_subtensor(self._endpoint, connect_timeout=self._connect_timeout)

    def _maybe_reconnect(self) -> None:
        """Recreate the websocket after sustained connection failures.

        Reconnects only once ``_consecutive_failures`` reaches
        ``_RECONNECT_AFTER_FAILURES`` — a single failed submit reuses the
        existing connection. Dropping and reopening on every failure leaks
        SDK subscriptions and OOMs the pod, so reconnect is the rare
        recovery path, not the per-error default. The old connection is
        closed before the fresh one replaces it so the recovery path does
        not itself leak websockets. A failed reconnect keeps the last
        connection and leaves the counter tripped so the next submit
        retries the reconnect.
        """
        if self._consecutive_failures < _RECONNECT_AFTER_FAILURES:
            return
        LOGGER.warning(
            "reconnecting subtensor after %d consecutive failures",
            self._consecutive_failures,
        )
        try:
            fresh = self._open_subtensor()
        except Exception as exc:  # noqa: BLE001 — keep the last connection on a failed reconnect; the counter stays tripped so the next submit retries.
            LOGGER.error("subtensor reconnect failed: %s", exc)
            return
        self._close_subtensor(self._subtensor)
        self._subtensor = fresh
        self._consecutive_failures = 0
        LOGGER.info("subtensor reconnected")

    @staticmethod
    def _close_subtensor(subtensor: Any | None) -> None:
        """Best-effort close of a retired subtensor's websocket.

        Called only when a fresh connection has already replaced it, so a
        ``close()`` failure is logged and swallowed — the connection is
        being discarded either way.
        """
        if subtensor is None:
            return
        close = getattr(subtensor, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup; the retired connection is discarded regardless.
            LOGGER.debug("retired subtensor close failed: %s", exc)

    def head_block(self) -> int:
        """Return the current chain head block from the long-lived connection.

        Reuses the same websocket the submitter holds — the validator's
        per-tick epoch cursor reads the head here rather than opening a
        second connection. A read failure counts toward reconnect exactly
        like a failed submit (only raised exceptions drive reconnect), so a
        dead socket is recovered on a later tick.

        A clean head read deliberately does NOT reset ``_consecutive_failures``:
        the head poll runs every tick before the submit, and a chain that
        can still serve ``get_current_block()`` while ``set_weights`` keeps
        raising must not have its accumulated submit failures wiped — only a
        clean ``set_weights`` proves the submit path healthy and resets the
        counter. The reset asymmetry keeps the leak-fix's
        reconnect-after-three-submit-failures recovery intact.

        The read is bounded by ``_rpc_timeout``: after a successful
        ``set_weights`` the SDK can leave the websocket wedged so the next
        ``get_current_block`` blocks forever, freezing the whole loop.
        A timeout is a raised exception like any other connection failure —
        it counts toward reconnect so the socket self-heals on a later tick.

        Raises:
            WeightSubmissionError: No connection is available, the head
                read failed at the connection level, or it timed out.
        """
        self._maybe_reconnect()
        subtensor = self._subtensor
        if subtensor is None:
            raise WeightSubmissionError("no subtensor connection available for head-block read")
        try:
            block = run_with_timeout(
                "subtensor get_current_block",
                subtensor.get_current_block,
                self._rpc_timeout,
            )
        except Exception as exc:
            self._consecutive_failures += 1
            raise WeightSubmissionError(f"head-block read failed: {exc}") from exc
        return int(block)

    def metagraph_hotkeys(self, netuid: int) -> dict[str, int]:
        """Return the subnet's hotkey ss58 -> uid mapping over the held socket.

        Reads the metagraph through the submitter's long-lived connection so
        startup and per-tick refreshes use exactly one websocket. A pending
        reconnect (failure threshold already tripped by prior submits/head
        reads) is honoured first so the refresh reads from the fresh socket
        in the same tick. Unlike a submit or head read, a failure here is not
        part of the reconnect accounting: callers keep their last-good lookup
        and the following head read or submit still drives socket health.

        Raises:
            WeightSubmissionError: No connection is available, or the
                metagraph read failed.
        """
        self._maybe_reconnect()
        subtensor = self._subtensor
        if subtensor is None:
            raise WeightSubmissionError("no subtensor connection available for metagraph read")
        try:
            metagraph = run_with_timeout(
                "subtensor metagraph",
                lambda: subtensor.metagraph(netuid),
                self._rpc_timeout,
            )
        except Exception as exc:
            raise WeightSubmissionError(f"metagraph read failed: {exc}") from exc
        return {hotkey: uid for uid, hotkey in enumerate(metagraph.hotkeys)}

    def weight_status(self) -> ValidatorWeightStatus | None:
        """Read the validator hotkey's own on-chain weight-setting status.

        Returns the validator's uid registration, its ``LastUpdate`` block
        (which advances only when weights are *applied* — the reveal, on a
        commit-reveal subnet), the current head, and the cached rate limit.

        Reads over the long-lived socket. Unlike a submit or head read, a
        failure here is observability-only: it does NOT touch the
        reconnect-failure counter and returns None ("unknown"), so the caller
        falls back to submitting and lets the chain's gate decide rather than
        skipping a tick over a transient read failure.
        """
        self._maybe_reconnect()
        subtensor = self._subtensor
        if subtensor is None:
            return None
        hotkey = self._wallet.hotkey.ss58_address
        try:
            uid = run_with_timeout(
                "subtensor get_uid_for_hotkey",
                lambda: subtensor.get_uid_for_hotkey_on_subnet(hotkey, self._netuid),
                self._rpc_timeout,
            )
            head = run_with_timeout(
                "subtensor get_current_block",
                subtensor.get_current_block,
                self._rpc_timeout,
            )
            if uid is None:
                return ValidatorWeightStatus(
                    registered=False,
                    last_update_block=None,
                    current_block=int(head),
                    weights_rate_limit=self._weights_rate_limit,
                )
            last_update = run_with_timeout(
                "subtensor LastUpdate",
                lambda: subtensor.substrate.query(
                    "SubtensorModule", "LastUpdate", [self._netuid]
                ).value[uid],
                self._rpc_timeout,
            )
            return ValidatorWeightStatus(
                registered=True,
                last_update_block=int(last_update),
                current_block=int(head),
                weights_rate_limit=self._weights_rate_limit,
            )
        except Exception as exc:  # noqa: BLE001 — observability-only; never disrupt the loop.
            LOGGER.debug("weight status read failed: %s", exc)
            return None

    def submit(
        self,
        *,
        netuid: int,
        uids: list[int],
        weights: list[int],
        epoch_id: int,
    ) -> None:
        """Submit one epoch's weight vector to the subnet.

        Args:
            netuid: Subnet id; must match the configured netuid.
            uids: Miner uids to set weights for.
            weights: Per-uid u16 weights summing to ``MAX_WEIGHT``.
            epoch_id: Finalized epoch id, for logging only.

        Raises:
            WeightSubmissionError: ``netuid`` mismatch, malformed input, a
                connection-level failure, or the chain rejecting the
                extrinsic for any reason. The validator loop retries the
                epoch on the next tick.
        """
        if netuid != self._netuid:
            raise WeightSubmissionError(
                f"netuid mismatch: submitter configured for {self._netuid}, "
                f"epoch {epoch_id} requested {netuid}"
            )
        if len(uids) != len(weights):
            raise WeightSubmissionError(
                f"uids/weights length mismatch: {len(uids)} != {len(weights)}"
            )
        if not uids:
            LOGGER.info("epoch %d: empty weight vector, nothing to submit", epoch_id)
            return

        self._maybe_reconnect()
        subtensor = self._subtensor
        if subtensor is None:
            raise WeightSubmissionError(f"epoch {epoch_id}: no subtensor connection available")

        LOGGER.info(
            "submitting weights: netuid=%d epoch=%d n_uids=%d sum=%d",
            netuid,
            epoch_id,
            len(uids),
            sum(weights),
        )

        def _set_weights() -> tuple[bool, str | None]:
            # Block for inclusion + finalization, exactly like the bm
            # validator. Waiting returns the chain's real accept/reject
            # receipt instead of a bare mempool accept, so a rejected or
            # pool-dropped vector surfaces here as (False, message) instead
            # of being silently recorded as a success. Under commit-reveal
            # this waits for the timelocked commit to land; the chain
            # reveals it automatically when the drand round elapses.
            #
            # mev_protection=False forces the plain author_submitExtrinsic
            # path regardless of a stray BT_MEV_PROTECTION env var (the SDK
            # default reads it from the env) — the bm validator's effective
            # default.
            return subtensor.set_weights(
                wallet=self._wallet,
                netuid=netuid,
                uids=uids,
                weights=weights,
                mev_protection=False,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )

        try:
            # No run_with_timeout: bounding the blocking wait is precisely
            # what leaked subscriptions — a fired timeout abandons (does not
            # cancel) the worker thread, leaving its block-event
            # subscription alive on the shared socket. The bm validator
            # never bounds the wait; the SDK tears its own subscription down
            # when the extrinsic finalizes, and the socket self-heals via
            # _maybe_reconnect after three consecutive raised failures. The
            # cost is that a wedged websocket blocks the loop until the
            # kubelet liveness probe restarts the pod — the bm trade-off.
            success, message = _set_weights()
        except Exception as exc:
            # A raised exception is a connection-level failure (dead
            # websocket, RPC transport error). Count it toward reconnect but
            # keep the connection — the next submit reconnects once the
            # counter trips. Do NOT drop the socket here; per-error drops leak.
            self._consecutive_failures += 1
            raise WeightSubmissionError(
                f"epoch {epoch_id}: set_weights call failed: {exc}"
            ) from exc

        if not success:
            # A (False, message) return is a chain answer, so the socket is
            # healthy: it neither resets nor increments the failure counter
            # (only raised exceptions drive reconnect, mirroring the bm
            # validator). The validator loop retries the epoch next tick;
            # an "Already Imported" pool duplicate carries no inclusion
            # receipt, so it is retried like any other rejection rather
            # than silently marking the epoch submitted.
            raise WeightSubmissionError(
                f"epoch {epoch_id}: subtensor rejected set_weights: {message or ''}"
            )

        # A clean success proves the websocket is healthy — reset the
        # connection-failure counter.
        self._consecutive_failures = 0
        LOGGER.info(
            "epoch %d: weights included & finalized on-chain (%s)", epoch_id, message or "ok"
        )


class RealChainCursor:
    """Derives the open epoch id from the chain head over the submitter's socket.

    Reads the head block through the ``RealSubmitter``'s long-lived
    connection so the validator holds exactly one websocket for both the
    head poll and weight submission. ``current_epoch`` returns the open
    epoch ``head_block // blocks_per_epoch`` — the same derivation the
    epoch-finalizer uses — or ``None`` when the head read fails, so a
    transient chain hiccup skips the tick instead of crashing the loop.
    """

    def __init__(self, submitter: RealSubmitter, blocks_per_epoch: int) -> None:
        if blocks_per_epoch <= 0:
            raise ValueError(f"blocks_per_epoch must be positive, got {blocks_per_epoch}")
        self._submitter = submitter
        self._blocks_per_epoch = blocks_per_epoch

    def current_epoch(self) -> int | None:
        try:
            block = self._submitter.head_block()
        except WeightSubmissionError as exc:
            LOGGER.warning("chain head read failed: %s — skipping this tick", exc)
            return None
        return block // self._blocks_per_epoch


class RealMetagraphSource:
    """Reads subnet hotkeys through the submitter's long-lived socket.

    The source is intentionally a tiny wrapper around ``RealSubmitter`` so
    validator ticks can refresh hotkey -> uid state without opening a second
    subtensor connection.
    """

    def __init__(self, submitter: RealSubmitter, netuid: int) -> None:
        self._submitter = submitter
        self._netuid = netuid

    def hotkeys(self) -> dict[str, int]:
        """Return the current hotkey ss58 -> uid mapping."""
        return self._submitter.metagraph_hotkeys(self._netuid)
