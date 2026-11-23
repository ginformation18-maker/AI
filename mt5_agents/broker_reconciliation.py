"""Read-only MT5 reconciliation for orders whose acknowledgements may be lost.

SAFETY INVARIANTS
- Never call order_send, modify positions, or auto-resubmit.
- A missing match does NOT prove the order was rejected.
- A matched order without a durable acknowledgement is STILL a manual hold.
- Broker comments may be truncated/modified; magic alone isn't proof of identity.
- Query failures, account mismatches, or wide history gaps fail closed.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from .memory.event_logger import utc_iso

# A broker may acknowledge or publish a fill seconds after an apparent disconnect.
# 60s is a MINIMUM observation delay, not a timeout that permits resubmission.
RECONCILIATION_COOLDOWN_SECONDS = 60


class ReconciliationRequired(RuntimeError):
    """Trading must remain halted until manual broker/account reconciliation."""


def correlation_comment(signal_id: str, account_id: int) -> str:
    """Short deterministic marker kept within conservative MT5 comment sizes.

    Unique per signal/account in practical terms (96 hash bits); idempotency is
    enforced by SQLite, not by relying on broker-side unique comments.
    """
    if not signal_id or account_id <= 0:
        raise ValueError("signal_id and positive account ID are required")
    return "AI" + hashlib.sha256(f"{account_id}|{signal_id}".encode("utf-8")).hexdigest()[:24]


def _utc_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Audit timestamp must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _row_dict(row: Any) -> dict:
    if hasattr(row, "_asdict"):
        return row._asdict()
    if isinstance(row, dict):
        return row
    # MT5 Python namedtuple-like rows are the usual case.
    return {k: getattr(row, k, None) for k in (
        "ticket", "order", "position_id", "symbol", "magic", "comment", "volume", "volume_initial"
    )}


@dataclass(frozen=True)
class ReconciliationObservation:
    signal_id: str
    status: str
    matched_objects: tuple[dict, ...] = ()
    weak_count: int = 0
    details: str = ""

    def to_audit(self) -> dict:
        return asdict(self)


class BrokerReconciler:
    def __init__(self, broker: Any, expected_login: int, max_history_days: int = 31):
        self.broker = broker
        self.expected_login = expected_login
        self.max_history_days = max_history_days

    def inspect(self, *, signal_id: str, intent_time: str, request: dict,
                as_of: datetime | None = None) -> ReconciliationObservation:
        """Inspect the *current* broker ledger for a persisted pre-send intent.

        The returned observation is evidence only. Never use it to automatically
        clear the manual hold, and never infer rejection from missing records.
        """
        now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            intent_at = _utc_datetime(intent_time)
        except (ValueError, TypeError, OverflowError):
            return ReconciliationObservation(signal_id, "INVALID_INTENT", details="Malformed intent timestamp; manual audit required")
        elapsed_seconds = (now - intent_at).total_seconds()
        if elapsed_seconds < 0:
            return ReconciliationObservation(signal_id, "CLOCK_UNSAFE", details="Intent is in the future; clock synchronization must be investigated")
        if elapsed_seconds < RECONCILIATION_COOLDOWN_SECONDS:
            return ReconciliationObservation(
                signal_id, "COOLDOWN_PENDING",
                details=f"Wait at least {RECONCILIATION_COOLDOWN_SECONDS} seconds since intent "
                        f"({RECONCILIATION_COOLDOWN_SECONDS - elapsed_seconds:.1f}s remain). "
                        "No broker absence inference or new orders permitted.",
            )
        since = intent_at - timedelta(minutes=5)
        if since > now or now - since > timedelta(days=self.max_history_days):
            return ReconciliationObservation(signal_id, "HISTORY_WINDOW_UNSAFE", details="History window outside supported range")
        account = self.broker.account_info()
        if account is None or int(getattr(account, "login", -1)) != self.expected_login:
            return ReconciliationObservation(signal_id, "ACCOUNT_MISMATCH", details="Account inaccessible or identity changed")
        symbol = str(request.get("symbol", ""))
        magic = request.get("magic")
        marker = str(request.get("comment", ""))
        if not symbol or not marker or not isinstance(magic, int):
            return ReconciliationObservation(signal_id, "INVALID_INTENT", details="Request has insufficient correlation fields")
        try:
            sources = (
                ("position", self.broker.positions_get()),
                ("active_order", self.broker.orders_get()),
                ("historical_order", self.broker.history_orders_get(since, now)),
                ("deal", self.broker.history_deals_get(since, now)),
            )
        except Exception as exc:
            return ReconciliationObservation(signal_id, "QUERY_FAILED", details=f"Broker query exception: {type(exc).__name__}")
        if any(rows is None for _, rows in sources):
            return ReconciliationObservation(signal_id, "QUERY_FAILED", details="One or more MT5 queries returned None")

        exact, weak = [], 0
        distinct_order_tickets = set()
        for kind, rows in sources:
            for original in rows:
                row = _row_dict(original)
                if row.get("symbol") != symbol or row.get("magic") != magic:
                    continue
                comment = str(row.get("comment") or "")
                # Only *strong* matches count as evidence for this signal.
                if comment == marker or comment.startswith(marker):
                    ticket = row.get("ticket")
                    order_ref = row.get("order") if kind == "deal" else ticket if kind in ("active_order", "historical_order") else None
                    if order_ref:
                        distinct_order_tickets.add(int(order_ref))
                    exact.append({
                        "kind": kind, "ticket": int(ticket or 0),
                        "order": int(order_ref or 0), "position_id": int(row.get("position_id") or 0),
                        "volume": float(row.get("volume") or row.get("volume_initial") or 0),
                    })
                else:
                    weak += 1
        if len(distinct_order_tickets) > 1:
            status = "MULTIPLE_MATCHES_MANUAL_REVIEW"
        elif exact:
            status = "BROKER_ACTIVITY_FOUND_MANUAL_REVIEW"
        elif weak:
            status = "WEAK_MAGIC_ONLY_MANUAL_REVIEW"
        else:
            status = "NO_MATCH_STILL_UNCERTAIN"
        return ReconciliationObservation(signal_id, status, tuple(exact[:50]), weak,
                                         "Broker records are evidence, not permission to retransmit")


def inspect_unsettled_intents(memory, broker, expected_login: int) -> list[ReconciliationObservation]:
    """Persist observations for every unresolved intent; all outcomes remain a hold.

    Failing to record observations propagates and must block resumed trading.
    """
    intents = memory.unsettled_execution_intents()
    reconciler = BrokerReconciler(broker, expected_login)
    observations = []
    for intent in intents:
        obs = reconciler.inspect(signal_id=intent["signal_id"],
                                 intent_time=intent["intent_time"], request=intent["request"])
        memory.record_trade_event(
            trade_ref=obs.signal_id, kind="TRADE_RECON_OBSERVATION",
            at_utc=datetime.now(timezone.utc), details=obs.to_audit(),
            event_id=f"recon:{obs.signal_id}:{uuid4().hex}",
        )
        observations.append(obs)
    return observations


def require_settled_before_new_order(memory, broker=None, expected_login: int | None = None) -> None:
    """Fail closed during cooldown AND after it; zero matches never grants release."""
    unsettled = memory.unsettled_execution_intents()
    if not unsettled:
        return
    if broker is not None and expected_login is not None:
        # Store best-available evidence; even an apparently matched order stays on hold.
        inspect_unsettled_intents(memory, broker, expected_login)
    raise ReconciliationRequired(
        f"{len(unsettled)} unresolved execution intent(s). STOP: inspect MT5 history "
        "and positions manually. NEVER retry the original signal."
    )
