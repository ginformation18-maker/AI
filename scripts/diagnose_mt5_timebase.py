"""MT5 timestamp timebase audit. READ ONLY. Never sends orders or changes datasets.

Run from the mt5_ai_agents project root with the project's venv.
Does not print credentials or account identifiers. This script is NOT a fix for MT5 timebases.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Make package imports work when called as 'python scripts/diagnose_mt5_timebase.py'.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def rounded_offset(seconds: float, bucket_seconds: int = 900, tolerance_seconds: int = 30) -> int | None:
    """Return a plausible broker clock offset ONLY if near a quarter-hour boundary."""
    nearest = round(seconds / bucket_seconds) * bucket_seconds
    return nearest if abs(seconds - nearest) <= tolerance_seconds else None


def utc(ts: float | int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def check_clock(samples: list[dict]) -> dict:
    if not samples:
        return {"status": "NO_TICKS", "candidate_offset_seconds": None}
    offsets = [s["clock_delta_seconds"] for s in samples]
    candidate = rounded_offset(statistics.median(offsets))
    # Require all tick samples to correspond to the *same* offset, not merely a median.
    aligned = candidate is not None and all(abs(x-candidate)<=30 for x in offsets)
    changing = {}
    for symbol in {s['symbol'] for s in samples}:
        t = [s["tick_raw_msc"] for s in samples if s['symbol']==symbol]
        changing[symbol] = len(set(t))>1
    return {"status": "CONSISTENT_CANDIDATE_NOT_VERIFIED" if aligned else "INCONSISTENT_OR_UNKNOWN",
            "candidate_offset_seconds": candidate if aligned else None,
            "sample_clock_delta_min": round(min(offsets), 2),
            "sample_clock_delta_max": round(max(offsets), 2),
            "ticks_advanced_during_test": changing}


def main() -> int:
    from mt5_agents.settings import Settings
    from mt5_agents.market import connect_mt5
    import MetaTrader5 as mt5

    cfg = Settings.load()
    if cfg.mode != 'DRY_RUN':
        print(json.dumps({"status":"FAIL_CLOSED", "reason":"Only MODE=DRY_RUN supported"}))
        return 2
    data = {"tool": "MT5 Timebase Diagnostic", "read_only": True,
            "prints_credentials": False, "automatic_offset_change": False,
            "utc_clock_at_start": utc(time.time()), "broker_server": cfg.server}
    try:
        account = connect_mt5(cfg)
        if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO or str(account.server) != cfg.server:
            raise RuntimeError('Demo account identity/server failed verification')
        symbols = tuple(dict.fromkeys(["XAUUSD", "EURUSD"]))
        samples: list[dict] = []
        for i in range(3):
            for symbol in symbols:
                selected = mt5.symbol_select(symbol, True)
                tick = mt5.symbol_info_tick(symbol) if selected else None
                before = time.time()
                if tick is None or not tick.time_msc or tick.bid<=0 or tick.ask<=tick.bid:
                    data.setdefault('warnings', []).append(f'{symbol}: quote unavailable/invalid, sample {i}')
                    continue
                tick_s = int(tick.time_msc)/1000
                samples.append({'symbol':symbol, 'tick_raw_msc':int(tick.time_msc),
                                'clock_delta_seconds':round(tick_s-before, 3),
                                'tick_raw_time_label':utc(tick_s)})
            if i < 2:
                time.sleep(2)
        # Tiny bounded windows for the two time domains; no multi-hour fetches.
        domains = {}
        now_s = time.time()
        for symbol in symbols:
            domains[symbol] = {}
            if not mt5.symbol_select(symbol, True):
                continue
            for label, shift in [('utc_window',0),('utc_plus_2h_window',7200)]:
                start = datetime.fromtimestamp(now_s-35+shift,timezone.utc)
                end = datetime.fromtimestamp(now_s-15+shift,timezone.utc)
                start_perf = time.monotonic()
                ticks = mt5.copy_ticks_range(symbol,start,end,mt5.COPY_TICKS_INFO)
                elapsed = round(time.monotonic()-start_perf, 3)
                if ticks is None:
                    domains[symbol][label] = {'status':'ERROR','error_code':mt5.last_error()[0],
                                               'duration_seconds':elapsed}
                else:
                    domains[symbol][label] = {'status':'OK','count':int(len(ticks)),
                                               'duration_seconds':elapsed,
                                               'first_raw_tick_label': utc(int(ticks[0]['time_msc'])/1000) if len(ticks) else None,
                                               'last_raw_tick_label': utc(int(ticks[-1]['time_msc'])/1000) if len(ticks) else None}
        bars = {}
        for symbol in symbols:
            rates = mt5.copy_rates_from_pos(symbol,mt5.TIMEFRAME_M1,0,3)
            if rates is None or not len(rates):
                bars[symbol] = {'available': False}
                continue
            ts = int(rates[-1]['time'])
            bars[symbol] = {'available': True, 'latest_raw_bar_label':utc(ts),
                            'bar_delta_from_utc_now_seconds':round(ts - time.time(),1),
                            'minute_alignment': ts%60 == 0}
        data['clock_analysis'] = check_clock(samples)
        data['historical_20s_windows'] = domains
        data['m1_bar_clock'] = bars
        data['samples'] = [{k:v for k,v in s.items() if k != 'tick_raw_msc'} for s in samples]
        data['archive_provenance_warning'] = ('Existing Parquet may label broker-clock timestamps as UTC. '
                                              'Do not alter/relabel existing files without a separate provenance audit.')
        data['status'] = 'AUDIT_ONLY_NOT_AUTHORIZED_TO_TRADE_OR_CORRECT_ARCHIVE'
    except Exception as exc:
        data['status']='FAIL_CLOSED'
        data['error_type']=type(exc).__name__
        data['error_summary']=str(exc)[:180]
        returncode=2
    else:
        returncode=0
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return returncode


if __name__ == '__main__':
    raise SystemExit(main())
