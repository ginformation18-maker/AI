"""Append-only audit events + immutable signal snapshot storage.

No secrets, no raw account credentials, and no MT5 calls belong in this layer.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any
import uuid

from .schema import initialize_database
from .db import read_connection, write_transaction


def utc_iso(value: datetime | str) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware UTC/offset datetimes")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return utc_iso(datetime.now(timezone.utc))


def json_obj(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def positive(value: float, field_name: str) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and positive")


@dataclass(frozen=True)
class SignalSpec:
    signal_id: str
    symbol: str
    side: str
    entry_time_utc: datetime | str
    entry_bid: float
    entry_ask: float
    take_profit: float
    stop_loss: float
    max_holding_bars: int
    decision: str
    model_version: str
    feature_version: str
    features: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        if not self.signal_id or not self.symbol or not self.decision:
            raise ValueError("signal_id, symbol and decision are required")
        if not self.model_version or not self.feature_version:
            raise ValueError("model and feature version required for reproducibility")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if not isinstance(self.max_holding_bars, int) or isinstance(self.max_holding_bars, bool) or self.max_holding_bars <= 0:
            raise ValueError("max_holding_bars must be a positive integer")
        for name in ("entry_bid", "entry_ask", "take_profit", "stop_loss"):
            positive(getattr(self, name), name)
        if self.entry_ask < self.entry_bid:
            raise ValueError("crossed entry quote (ask < bid)")
        if self.side == "BUY" and not (self.stop_loss < self.entry_bid <= self.entry_ask < self.take_profit):
            raise ValueError("BUY requires SL < bid <= ask < TP")
        if self.side == "SELL" and not (self.take_profit < self.entry_bid <= self.entry_ask < self.stop_loss):
            raise ValueError("SELL requires TP < bid <= ask < SL")
        if not isinstance(self.features, dict):
            raise ValueError("features must be a JSON object")
        # Validate JSON serializability early (e.g. reject NaNs and numpy non-primitives).
        json_obj(self.features)
        return {
            "signal_id": self.signal_id, "symbol": self.symbol, "side": self.side,
            "entry_time_utc": utc_iso(self.entry_time_utc),
            "entry_bid": float(self.entry_bid), "entry_ask": float(self.entry_ask),
            "entry_price": float(self.entry_ask if self.side == "BUY" else self.entry_bid),
            "take_profit": float(self.take_profit), "stop_loss": float(self.stop_loss),
            "max_holding_bars": self.max_holding_bars,
            "decision": self.decision, "model_version": self.model_version,
            "feature_version": self.feature_version, "features_json": json_obj(self.features),
        }


class EventLogger:
    def __init__(self, db_path: str | Path):
        self.db_path = initialize_database(db_path)

    def record_signal(self, signal: SignalSpec) -> bool:
        """Atomic signal + audit write. Returns False for an identical repeated signal."""
        data = signal.canonical()
        event_payload = json_obj(data)
        with write_transaction(self.db_path) as db:
            existing = db.execute("SELECT payload_json FROM events WHERE event_id=?", (f"signal:{signal.signal_id}",)).fetchone()
            if existing:
                if existing["payload_json"] != event_payload:
                    raise ValueError("signal_id reused with different immutable features")
                return False
            created = now_iso()
            db.execute("""INSERT INTO hypothetical_signals
                (signal_id, symbol, side, entry_time_utc, entry_bid, entry_ask, entry_price,
                 take_profit, stop_loss, max_holding_bars, decision, model_version,
                 feature_version, features_json, created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["signal_id"],data["symbol"],data["side"],data["entry_time_utc"],
                 data["entry_bid"],data["entry_ask"],data["entry_price"],data["take_profit"],
                 data["stop_loss"],data["max_holding_bars"],data["decision"],data["model_version"],
                 data["feature_version"],data["features_json"],created))
            self._event(db, f"signal:{signal.signal_id}", "SIGNAL", signal.signal_id,
                        data["entry_time_utc"], event_payload)
        return True

    @staticmethod
    def _event(db, event_id: str, kind: str, entity_id: str, event_time: str, payload_json: str) -> None:
        db.execute("""INSERT INTO events
                     (event_id,event_type,entity_id,event_time_utc,payload_json,persisted_at_utc)
                     VALUES (?,?,?,?,?,?)""",
                   (event_id, kind, entity_id, event_time, payload_json, now_iso()))

    def record_market_snapshot(self, symbol: str, at_utc: datetime | str, snapshot: dict,
                               *, event_id: str | None = None) -> str:
        """Record a sampled quote/snapshot. Not for every tick at high frequency."""
        if not symbol or not isinstance(snapshot, dict):
            raise ValueError("symbol and snapshot JSON object required")
        timestamp = utc_iso(at_utc)
        payload = json_obj({"symbol":symbol, "snapshot":snapshot})
        event_id = event_id or f"snapshot:{uuid.uuid4()}"
        self._record_idempotent(event_id, "SNAPSHOT", symbol, timestamp, payload)
        return event_id

    def record_trade_event(self, trade_ref: str, kind: str, at_utc: datetime | str,
                           details: dict, *, event_id: str) -> str:
        """Audit a broker order/deal status; no actual order is submitted here."""
        if not trade_ref or not kind.startswith("TRADE_") or not event_id or not isinstance(details, dict):
            raise ValueError("trade_ref, TRADE_ kind, details and deterministic event_id required")
        payload = json_obj({"trade_ref":trade_ref, "details":details})
        self._record_idempotent(event_id, kind, trade_ref, utc_iso(at_utc), payload)
        return event_id

    def _record_idempotent(self, event_id, kind, entity_id, timestamp, payload):
        with write_transaction(self.db_path) as db:
            old = db.execute("SELECT event_type,entity_id,event_time_utc,payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if tuple(old) != (kind, entity_id, timestamp, payload):
                    raise ValueError("conflicting event_id payload")
                return
            self._event(db, event_id, kind, entity_id, timestamp, payload)

    def record_execution_intent(self, signal_id: str, request: dict, *, at_utc: datetime | str) -> None:
        """Durable, ONE-TIME pre-send gate; a repeated intent never authorizes retry.

        This must be called *inside* the order execution gateway immediately
        before order_send, after all broker-side preflight checks. If commit
        fails, a PersistenceError propagates and no order is submitted.
        """
        timestamp = utc_iso(at_utc)
        payload = json_obj({"signal_id":signal_id,"request":request})
        with write_transaction(self.db_path) as db:
            signal = db.execute("SELECT signal_id FROM hypothetical_signals WHERE signal_id=?", (signal_id,)).fetchone()
            if signal is None:
                raise ValueError("Execution cannot proceed without a persisted signal")
            event_id = f"execution_intent:{signal_id}"
            if db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
                raise RuntimeError("Execution intent already exists; reconcile broker before any retry")
            self._event(db, event_id, "EXECUTION_INTENT", signal_id, timestamp, payload)

    def get_events(self, entity_id: str) -> list[dict]:
        with read_connection(self.db_path) as db:
            rows = db.execute("SELECT * FROM events WHERE entity_id=? ORDER BY event_time_utc, event_id", (entity_id,)).fetchall()
        return [{**dict(row), "payload":json.loads(row["payload_json"])} for row in rows]


    def unsettled_execution_intents(self) -> list[dict]:
        """Return PRE-SEND intents lacking an unambiguous completed broker reply.

        A DONE receipt with ticket is NOT sufficient: an additional durable
        TRADE_SERVER_PROTECTION_VERIFIED record must exist. Without it, a
        missing or unprotected position stays on manual hold even after crash.
        All ambiguous results and ticketless receipts also stay blocked.
        """
        with read_connection(self.db_path) as db:
            rows = db.execute("""
                SELECT i.entity_id AS signal_id, i.event_time_utc AS intent_time,
                       i.payload_json AS intent_payload, r.payload_json AS result_payload,
                       p.event_type AS protection_event_type
                FROM events AS i
                LEFT JOIN events AS r
                  ON r.event_id = 'broker_result:' || i.entity_id
                LEFT JOIN events AS p
                  ON p.event_id = 'protection:' || i.entity_id
                WHERE i.event_type = 'EXECUTION_INTENT'
                ORDER BY i.event_time_utc, i.entity_id
            """).fetchall()
        unsettled = []
        for row in rows:
            request = json.loads(row["intent_payload"])["request"]
            result = json.loads(row["result_payload"])["details"] if row["result_payload"] else None
            confirmed = (result is not None and result.get("status") == "CONFIRMED_DONE"
                         and (int(result.get("deal") or 0) > 0 or int(result.get("order") or 0) > 0)
                         and row["protection_event_type"] == "TRADE_SERVER_PROTECTION_VERIFIED")
            if not confirmed:
                unsettled.append({"signal_id": row["signal_id"],
                                  "intent_time": row["intent_time"],
                                  "request": request, "broker_result": result})
        return unsettled
