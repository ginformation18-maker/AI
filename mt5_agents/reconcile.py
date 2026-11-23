"""Read-only diagnostic command: python -m mt5_agents.reconcile --report.

No broker order, no automatic release and no STOP deletion.
"""
import argparse
import json

import MetaTrader5 as mt5

from .settings import ROOT, Settings
from .market import connect_mt5
from .memory import EventLogger
from .broker_reconciliation import inspect_unsettled_intents


def main():
    parser = argparse.ArgumentParser(description="Inspect unsettled MT5 trade intents (READ ONLY)")
    parser.add_argument("--report", action="store_true", required=True)
    args = parser.parse_args()
    cfg = Settings.load()
    path = ROOT / "runtime" / "memory.sqlite3"
    if not path.exists():
        raise RuntimeError("Audit DB missing. Do not infer that there are no in-flight orders.")
    connected = False
    try:
        connect_mt5(cfg)
        connected = True
        memory = EventLogger(path)
        observations = inspect_unsettled_intents(memory, mt5, cfg.login)
        print(json.dumps({"account":cfg.login,"count":len(observations),
                          "note":"Inspection only. No order sent. No hold cleared.",
                          "observations":[x.to_audit() for x in observations]},
                         ensure_ascii=False, indent=2))
    finally:
        if connected:
            mt5.shutdown()


if __name__ == "__main__":
    main()
