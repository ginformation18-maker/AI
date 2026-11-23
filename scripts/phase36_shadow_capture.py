"""Manual-only independent MT5 shadow recorder; never sends orders."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time

# Make direct `python scripts/phase36_shadow_capture.py` work from repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mt5_agents.phase36_shadow_capture import ShadowCapture, capture_status, exclusive_shadow_process, require_windows_ntp_sync
from mt5_agents.settings import Settings
from mt5_agents.observation_timebase import ObservationTimebase


def main() -> None:
    parser = argparse.ArgumentParser(description='Provisional raw-tick SHADOW ONLY (NO ORDERS / NO original archive writes)')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true', help='Capture latest closed raw minute once per symbol')
    mode.add_argument('--loop', action='store_true', help='Record future completed minutes; no backfill')
    mode.add_argument('--status', action='store_true', help='Verify shadow manifests/checksums only; no MT5 connection')
    parser.add_argument('--symbols', nargs='+', default=['XAUUSD', 'EURUSD'])
    args = parser.parse_args()
    shadow_root = ROOT / 'runtime' / 'shadow_tick_archive'
    if args.status:
        print(json.dumps(capture_status(shadow_root), ensure_ascii=False, indent=2), flush=True)
        return
    cfg = Settings.load()
    if cfg.mode != 'DRY_RUN':
        raise RuntimeError('Shadow recorder requires MODE=DRY_RUN')
    if (ROOT / 'STOP').exists():
        raise RuntimeError('STOP safety file exists: no capture allowed')
    timebase = ObservationTimebase.from_env(cfg)
    if not timebase.provisional or timebase.offset_seconds != 7200:
        raise RuntimeError('This test release requires verified configured PROVISIONAL +7200 on MetaQuotes-Demo')
    if cfg.server != 'MetaQuotes-Demo':
        raise RuntimeError('Shadow capture currently limited to the diagnosed MetaQuotes-Demo server')
    if any(s not in cfg.symbols for s in args.symbols):
        raise RuntimeError('Requested symbol not in approved SYMBOLS from .env')
    from mt5_agents.market import connect_mt5
    import MetaTrader5 as mt5
    with exclusive_shadow_process(shadow_root):
        # Scan for orphans and modified old windows before writing anything.
        capture_status(shadow_root)
        connected = False
        try:
            acc = connect_mt5(cfg)
            connected = True
            if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO or acc.server != cfg.server:
                raise RuntimeError('Only connected DEMO account on configured broker is permitted')
            cap = ShadowCapture(root=shadow_root, mt5=mt5, login=cfg.login,
                                server=cfg.server, offset_sec=timebase.offset_seconds)
            print(json.dumps({'status':'SHADOW_ONLY','timebase':'UNVERIFIED_PENDING_DST',
                              'no_trading':True,'original_parquet_unchanged':True,
                              'symbols':args.symbols}, ensure_ascii=False), flush=True)
            previous_states = {}
            while True:
                require_windows_ntp_sync()
                if (ROOT / 'STOP').exists():
                    raise RuntimeError('STOP appeared: stop shadow capture')
                for symbol in args.symbols:
                    result = cap.capture_once(symbol)
                    if (args.once or result.get('archived') or
                            previous_states.get(symbol) != result['state']):
                        print(json.dumps(result, ensure_ascii=False), flush=True)
                    previous_states[symbol] = result['state']
                if args.once:
                    break
                time.sleep(5)
        finally:
            if connected:
                mt5.shutdown()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('SHADOW STOPPED by operator; closed windows remain immutable.', flush=True)
