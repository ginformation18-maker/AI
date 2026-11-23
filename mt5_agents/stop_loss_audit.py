"""Read-only actual stop-loss fill/slippage audit based on the broker deal ledger.

NEVER sends or modifies orders. This is an observation, not a claim that every
price deviation was caused by broker misconduct. No guessed joins by magic alone:
link close deals via the verified opening broker deal/order -> position_id.
If the effective stop at exit is absent, log it as UNMEASURABLE, not 0 slippage.
Partial exits are recorded per deal, never misreported as a full-position fill.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from .memory.event_logger import utc_iso, EventLogger


def _get(obj: Any, name: str, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _valid_price(x):
    try:
        return math.isfinite(float(x)) and float(x) > 0
    except (ValueError, TypeError):
        return False


def _intent_rows(memory_db: Path):
    if not memory_db.is_file():
        raise RuntimeError("Missing immutable trade audit database; cannot attribute SL fills")
    with sqlite3.connect(f"file:{memory_db.resolve().as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("""SELECT i.entity_id, i.payload_json, b.payload_json
                            FROM events i LEFT JOIN events b
                              ON b.event_id='broker_result:' || i.entity_id
                            WHERE i.event_type='EXECUTION_INTENT'
                            ORDER BY i.event_time_utc""").fetchall()
    for signal, intent_json, result_json in rows:
        intent = json.loads(intent_json)["request"]
        result = json.loads(result_json)["details"] if result_json else {}
        yield signal, intent, result


def audit_stop_exits(broker, memory_db: str | Path, expected_login: int,
                     since: datetime, until: datetime, *, persist: bool = True) -> dict:
    """Query a bounded UTC interval, record actual stop fills via durable idempotent events."""
    if (since.tzinfo is None or until.tzinfo is None or since >= until
            or until - since > timedelta(days=90)):
        raise ValueError("Require ascending aware timestamps within at most 90 days")
    account = broker.account_info()
    if account is None or int(_get(account, "login", -1)) != expected_login:
        raise RuntimeError("Broker identity could not be confirmed. STOP report, no guesses")
    raw = broker.history_deals_get(since.astimezone(timezone.utc), until.astimezone(timezone.utc))
    if raw is None:
        raise RuntimeError("Broker history_deals_get returned None: not a complete ledger")
    deals = list(raw)
    in_code = int(broker.DEAL_ENTRY_IN)
    out_codes = {int(broker.DEAL_ENTRY_OUT)}
    if hasattr(broker, "DEAL_ENTRY_OUT_BY"):
        out_codes.add(int(broker.DEAL_ENTRY_OUT_BY))
    sl_code = int(broker.DEAL_REASON_SL)
    results = []
    warnings = []
    memory_db = Path(memory_db)
    records = list(_intent_rows(memory_db))
    # One netting position can contain several entry signals; attributing the
    # entire stop fill to one of them is unsafe. Detect ambiguous ownership.
    position_owners = {}
    for signal_id, request, result in records:
        if result.get("status") != "CONFIRMED_DONE":
            continue
        ref_deal = int(result.get("deal") or 0)
        ref_order = int(result.get("order") or 0)
        for d in deals:
            if (_get(d,"symbol")==request.get("symbol")
                    and int(_get(d,"magic",-1))==int(request.get("magic",-1))
                    and int(_get(d,"entry",-1))==in_code
                    and (ref_deal and int(_get(d,"ticket",0))==ref_deal
                         or ref_order and int(_get(d,"order",0))==ref_order)):
                position_owners.setdefault(int(_get(d,"position_id",0)),set()).add(signal_id)
    for signal_id, request, result in records:
        if result.get("status") != "CONFIRMED_DONE":
            continue
        symbol = str(request.get("symbol", ""))
        magic = int(request.get("magic", -1))
        ref_deal = int(result.get("deal") or 0)
        ref_order = int(result.get("order") or 0)
        if not (ref_deal or ref_order):
            continue
        open_matches = [d for d in deals if _get(d,"symbol")==symbol
                        and int(_get(d,"magic",-1))==magic
                        and int(_get(d,"entry",-1))==in_code
                        and (ref_deal and int(_get(d,"ticket",0))==ref_deal
                             or ref_order and int(_get(d,"order",0))==ref_order)]
        position_ids = {int(_get(d,"position_id",0)) for d in open_matches}
        if len(position_ids) != 1 or 0 in position_ids:
            warnings.append({"signal_id":signal_id,"status":"OPENING_DEAL_NOT_UNIQUELY_LINKED",
                             "reason":"Historical window may not include entry; do not infer a stop fill"})
            continue
        position_id = next(iter(position_ids))
        if len(position_owners.get(position_id,set())) != 1:
            warnings.append({"signal_id":signal_id,"status":"NETTING_POSITION_SHARED_BY_MULTIPLE_SIGNALS",
                             "position_id":position_id})
            continue
        # A short position's SL is adverse if exit buy fills HIGHER than stop.
        opening_types = {int(_get(d,"type",-1)) for d in open_matches}
        if len(opening_types) != 1 or next(iter(opening_types)) not in (int(broker.DEAL_TYPE_BUY), int(broker.DEAL_TYPE_SELL)):
            warnings.append({"signal_id":signal_id,"status":"ENTRY_SIDE_AMBIGUOUS"})
            continue
        observed_type = next(iter(opening_types))
        if observed_type != int(request.get("type", -1)):
            warnings.append({"signal_id":signal_id,"status":"OPENING_SIDE_DIFFERS_FROM_REQUEST"})
            continue
        is_long = observed_type == int(broker.DEAL_TYPE_BUY)
        info = broker.symbol_info(symbol)
        point = float(_get(info,"point",0) or 0)
        if not math.isfinite(point) or point <= 0:
            raise RuntimeError(f"No valid point size for {symbol}")
        stop_deals = [d for d in deals if _get(d,"symbol")==symbol
                      and int(_get(d,"position_id",0))==position_id
                      and int(_get(d,"entry",-1)) in out_codes
                      and int(_get(d,"reason",-1))==sl_code]
        for deal in stop_deals:
            ticket = int(_get(deal,"ticket",0))
            actual = _get(deal,"price")
            effective_stop = _get(deal,"sl",0)
            requested_stop = request.get("sl")
            if ticket <= 0 or not _valid_price(actual):
                raise RuntimeError("Invalid broker exit deal: no ticket/price")
            if not _valid_price(requested_stop):
                raise RuntimeError("Immutable original stop missing from recorded request")
            # MT5 deal.sl is broker-reported S/L at closure, which can differ
            # from original requested stop after an operator modifies it.
            measured = _valid_price(effective_stop)
            signed = ((float(effective_stop) - float(actual)) if is_long
                      else (float(actual) - float(effective_stop))) / point if measured else None
            timestamp_ms = _get(deal, "time_msc")
            stamp = (datetime.fromtimestamp(int(timestamp_ms)/1000, tz=timezone.utc)
                     if timestamp_ms is not None else
                     datetime.fromtimestamp(int(_get(deal,"time")), tz=timezone.utc))
            detail = {
                "signal_id":signal_id,"deal_ticket":ticket,"position_id":position_id,
                "symbol":symbol,"side":"BUY" if is_long else "SELL",
                "deal_reason":"STOP_LOSS","close_entry":int(_get(deal,"entry")),
                "volume":float(_get(deal,"volume",0)),"actual_fill_price":float(actual),
                "original_requested_stop":float(requested_stop),
                "broker_reported_stop_at_exit":float(effective_stop) if measured else None,
                "stop_modified_from_original":(abs(float(effective_stop)-float(requested_stop))>point/2)
                    if measured else None,
                "signed_slippage_points":round(signed, 6) if signed is not None else None,
                "signed_difference_vs_initial_stop_points":round(
                    ((float(requested_stop)-float(actual)) if is_long
                     else (float(actual)-float(requested_stop))) / point, 6),
                "adverse_slippage_points":round(max(signed,0.0),6) if signed is not None else None,
                "status":"MEASURED_FROM_BROKER_STOP" if measured else "STOP_REFERENCE_UNAVAILABLE",
                "point":point,"execution_time_utc":utc_iso(stamp),
                "caveat":"Measured difference is not proof of broker fault or maximum loss; gaps and commissions are separate.",
            }
            results.append(detail)
            if persist:
                EventLogger(memory_db).record_trade_event(
                    trade_ref=signal_id, kind="TRADE_STOP_LOSS_SLIPPAGE_OBSERVED",
                    at_utc=stamp, details=detail, event_id=f"stop_slippage:{signal_id}:{ticket}")
    return {"status":"READ_ONLY","account":expected_login,"deals_scanned":len(deals),
            "stop_fill_observations":results,"unmatched_intents":warnings,
            "note":"History-window and verified opening ticket determine attribution. No order is sent."}


def main():
    import MetaTrader5 as mt5
    from .settings import ROOT, Settings
    from .market import connect_mt5
    p=argparse.ArgumentParser(description="Read-only actual MT5 stop-loss slippage audit; no order actions")
    p.add_argument("--days", type=int, default=30, help="Lookback: 1..90 days")
    p.add_argument("--out", help="Optional JSON report path")
    args=p.parse_args()
    if not (1 <= args.days <= 90):
        p.error("--days must be 1..90")
    cfg=Settings.load()
    if not (ROOT/"runtime"/"memory.sqlite3").is_file():
        raise RuntimeError("No trade audit DB: collector-only phase has no fills to audit")
    try:
        connect_mt5(cfg)
        now=datetime.now(timezone.utc)
        report=audit_stop_exits(mt5, ROOT/"runtime"/"memory.sqlite3",cfg.login,
                                now-timedelta(days=args.days),now)
        txt=json.dumps(report,ensure_ascii=False,indent=2)
        print(txt)
        if args.out:
            Path(args.out).write_text(txt,encoding="utf-8")
    finally:
        mt5.shutdown()


if __name__=="__main__":
    main()
