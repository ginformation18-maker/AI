"""Standalone offline Phase 3.6.2 report. READ ONLY; never opens MetaTrader5."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only raw-broker-clock quote statistics; no trades")
    parser.add_argument("--symbol", default="XAUUSD", help="Symbol archive partition, e.g. XAUUSD")
    parser.add_argument("--max-files", type=int, default=10000)
    parser.add_argument("--details-limit", type=int, default=10,
                        help="Show only this many most recent checked windows; all files are still verified")
    args = parser.parse_args()
    try:
        from dotenv import dotenv_values
        cfg = dotenv_values(ROOT / '.env')
        # Use the effective explicit .env values; do not print secrets.
        mode = os.environ.get('MODE', cfg.get('MODE', ''))
        timebase_mode = os.environ.get('MT5_OBSERVER_TIMEBASE_MODE',
                                       cfg.get('MT5_OBSERVER_TIMEBASE_MODE', ''))
        ack = os.environ.get('MT5_OBSERVER_TIMEBASE_ACK',
                             cfg.get('MT5_OBSERVER_TIMEBASE_ACK', ''))
        if mode != 'DRY_RUN' or timebase_mode != 'PROVISIONAL_BROKER_OFFSET' or ack != 'I_ACCEPT_OBSERVATION_ONLY_NO_ARCHIVE':
            raise RuntimeError('OBSERVATION_ONLY_PROVISIONAL_TIMEBASE_REQUIRED')
        from mt5_agents.phase36_quant_context import inspect_archive_quant
        payload = inspect_archive_quant(ROOT / 'runtime' / 'tick_archive' / 'parquet',
                                        symbol=args.symbol, max_files=args.max_files,
                                        details_limit=args.details_limit)
        status = 0
    except Exception as exc:
        # Fail closed; do not print exception text since a filesystem path may
        # accidentally contain sensitive material. No partial or green status.
        payload = {"tool":"Phase 3.6.2 Read-only Quant Context", "status":"FAIL_CLOSED",
                   "read_only": True, "writes_archive": False, "sends_orders": False,
                   "error_type": type(exc).__name__,
                   "operator_message": "Check .env DRY_RUN/provisional settings and archive integrity. No files changed."}
        status = 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
