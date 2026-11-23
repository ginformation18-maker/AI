"""Persisted event-time triple-barrier shadow labels, no MT5 order execution.

Input MUST be fully CLOSED, strictly ordered, broker-tradeable BID/ASK OHLC.
Standard MT5 copy_rates_* does NOT provide historical Ask OHLC: do not
fabricate it from Bid OHLC here. Integrators need tick-derived quote bars.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path

from .event_logger import EventLogger, json_obj, now_iso, positive, utc_iso
from .db import read_connection, write_transaction


@dataclass(frozen=True)
class QuoteBar:
    bar_open_utc: datetime | str
    bar_close_utc: datetime | str
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float

    def canonical(self) -> dict:
        d = asdict(self)
        d["bar_open_utc"], d["bar_close_utc"] = utc_iso(self.bar_open_utc), utc_iso(self.bar_close_utc)
        if d["bar_close_utc"] <= d["bar_open_utc"]:
            raise ValueError("bar_close must be after bar_open")
        for key in ("bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close"):
            positive(d[key], key)
            d[key] = float(d[key])
        for prefix in ("bid", "ask"):
            if not (d[f"{prefix}_low"] <= d[f"{prefix}_open"] <= d[f"{prefix}_high"] and
                    d[f"{prefix}_low"] <= d[f"{prefix}_close"] <= d[f"{prefix}_high"]):
                raise ValueError(f"invalid {prefix} OHLC extrema")
        if any(d[f"bid_{part}"] > d[f"ask_{part}"] for part in ("open", "high", "low", "close")):
            raise ValueError("crossed BID/ASK quote bar")
        return d


@dataclass(frozen=True)
class ProcessingResult:
    status: str  # PENDING, LABELED, DUPLICATE, ALREADY_RESOLVED, SKIPPED_OVERLAP
    bars_seen: int
    outcome: str | None = None
    reason: str | None = None


class PendingLabelQueue:
    def __init__(self, db_path: str | Path):
        self.db_path = EventLogger(db_path).db_path

    def pending(self, limit: int = 1000) -> list[dict]:
        if not 1 <= limit <= 100_000:
            raise ValueError("invalid limit")
        with read_connection(self.db_path) as db:
            rows = db.execute("SELECT * FROM hypothetical_signals WHERE status='PENDING' ORDER BY entry_time_utc, signal_id LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_label(self, signal_id: str) -> dict | None:
        with read_connection(self.db_path) as db:
            row = db.execute("SELECT * FROM closed_labels WHERE signal_id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def process_closed_bar(self, signal_id: str, bar: QuoteBar) -> ProcessingResult:
        """Atomic, idempotent progress. A missing interval must be backfilled first.

        Full bars with open >= signal entry time only (avoid pre-entry high/low).
        Does not close actual broker positions. Same-bar dual touch uses SL-first.
        ALL output labels remain training_eligible=0 until external net-cost audit.
        """
        data = bar.canonical()
        serialized = json_obj(data)
        with write_transaction(self.db_path) as db:
            row = db.execute("SELECT * FROM hypothetical_signals WHERE signal_id=?", (signal_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown signal: {signal_id}")
            if data["bar_close_utc"] <= row["entry_time_utc"] or data["bar_open_utc"] < row["entry_time_utc"]:
                return ProcessingResult("SKIPPED_OVERLAP", row["bars_seen"])
            prev = db.execute("SELECT quote_json FROM observed_bars WHERE signal_id=? AND bar_open_utc=?",
                              (signal_id, data["bar_open_utc"])).fetchone()
            if prev:
                if prev["quote_json"] != serialized:
                    raise ValueError("same bar timestamp with conflicting quote data")
                label = db.execute("SELECT outcome,reason FROM closed_labels WHERE signal_id=?", (signal_id,)).fetchone()
                return ProcessingResult("DUPLICATE", row["bars_seen"], label["outcome"] if label else None,
                                        label["reason"] if label else None)
            if row["status"] != "PENDING":
                return ProcessingResult("ALREADY_RESOLVED", row["bars_seen"])
            if row["last_bar_close_utc"] and data["bar_open_utc"] != row["last_bar_close_utc"]:
                raise ValueError("missing/out-of-order interval: backfill or validate session gap")

            count = row["bars_seen"] + 1
            side = row["side"]
            tp, sl = row["take_profit"], row["stop_loss"]
            if side == "BUY":
                open_px, high, low, close_px = (data[f"bid_{x}"] for x in ("open", "high", "low", "close"))
    
                hit_tp, hit_sl = high >= tp, low <= sl
            else:
                open_px, high, low, close_px = (data[f"ask_{x}"] for x in ("open", "high", "low", "close"))
    
                hit_tp, hit_sl = low <= tp, high >= sl

            outcome = reason = exit_px = label_y = None
            # At the opening quote we can bound adverse gaps conservatively.
            # Favorable gaps are capped at the quoted TP; adverse ones can
            # exit WORSE than the SL. Without ticks, all these are hypothetical.
            adverse_gap = (side == "BUY" and open_px <= sl) or (side == "SELL" and open_px >= sl)
            favorable_gap = (side == "BUY" and open_px >= tp) or (side == "SELL" and open_px <= tp)
            if adverse_gap:
                outcome, reason, exit_px, label_y = "SL", "adverse_gap_at_open_shadow_only", open_px, -1
            elif favorable_gap:
                outcome, reason, exit_px, label_y = "TP", "favorable_gap_at_open_capped_at_tp", tp, 1
            elif hit_tp and hit_sl:
                outcome, reason, exit_px, label_y = "SL", "both_barriers_touched_SL_first_conservative", sl, -1
            elif hit_sl:
                outcome, reason, exit_px, label_y = "SL", "stop_loss_touch_shadow_only", sl, -1
            elif hit_tp:
                outcome, reason, exit_px, label_y = "TP", "take_profit_touch_shadow_only", tp, 1
            elif count >= row["max_holding_bars"]:
                outcome, reason, exit_px = "TIME", "vertical_barrier_shadow_close", close_px

            db.execute("INSERT INTO observed_bars(signal_id,bar_open_utc,bar_close_utc,quote_json) VALUES (?,?,?,?)",
                       (signal_id, data["bar_open_utc"], data["bar_close_utc"], serialized))
            if outcome:
                status = "LABELED"
                gross = (float(exit_px) - float(row["entry_price"])) * (1 if side == "BUY" else -1) if exit_px is not None else None
                db.execute("""INSERT INTO closed_labels
                    (signal_id,outcome,provisional_y,exit_time_utc,exit_quote,gross_price_move,bars_seen,reason,created_at_utc)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (signal_id,outcome,label_y,data["bar_close_utc"],exit_px,gross,count,reason,now_iso()))
                EventLogger._event(db, f"label:{signal_id}", "SHADOW_LABEL", signal_id,
                                   data["bar_close_utc"], json_obj({"outcome":outcome,"reason":reason,
                                    "exit_quote":exit_px,"gross_price_move":gross,"bars_seen":count,
                                    "training_eligible":False}))
            else:
                status = "PENDING"
            db.execute("UPDATE hypothetical_signals SET status=?,bars_seen=?,last_bar_close_utc=? WHERE signal_id=?",
                       (status,count,data["bar_close_utc"],signal_id))
            return ProcessingResult(status,count,outcome,reason)
