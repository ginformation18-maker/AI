"""Append-only JSONL audit log; does not include credentials."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from .settings import ROOT

LOG_PATH = ROOT / "runtime" / "events.jsonl"


def audit(event: str, **fields) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    logging.getLogger("mt5_agents").info("%s: %s", event, fields)
