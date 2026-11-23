"""Mandatory local write/read/integrity preflight; no MT5 connection and no trades.

A fresh synthetic Parquet file is written in the SAME archive filesystem as the
collector and read back with the production writer and schema checks. A failed
read/write, missing pyarrow, or failed corruption detector stops startup.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
from .parquet_integrity import canonical_frame, write_verified_parquet, verify_parquet


def run_preflight(archive_root: str | Path) -> dict:
    base = Path(archive_root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    # Known timestamps, duplicate millisecond quote, fractional milliseconds, exact dtypes.
    start = int(datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    source = pd.DataFrame({
        "time_msc": np.array([start, start, start + 1, start + 17], dtype="int64"),
        "bid": np.array([4100.0, 4100.01, 4100.02, 4100.03], dtype="float64"),
        "ask": np.array([4100.2, 4100.21, 4100.22, 4100.23], dtype="float64"),
        "flags": np.array([2, 6, 2, 6], dtype="int32"),
        "spread_points": np.array([20.0] * 4, dtype="float64"),
    })
    with TemporaryDirectory(prefix="parquet_preflight_", dir=base) as folder:
        target = Path(folder) / "probe.parquet"
        write_verified_parquet(source, target)
        report = verify_parquet(target, expected=canonical_frame(source))
        if report["rows"] != 4 or not report["schema_ok"]:
            raise RuntimeError("Parquet round-trip check did not reproduce all sample rows")
        # Verify corrupted bytes are actually detectable, not just successfully written.
        broken = Path(folder) / "corrupted.parquet"
        broken.write_bytes(target.read_bytes()[:24])
        try:
            verify_parquet(broken)
        except Exception:
            pass
        else:
            raise RuntimeError("Parquet corruption detector failed: unsafe to collect")
        return {"status": "PASS", "real_arrow_roundtrip": True,
                "corruption_detected": True, "tested_rows": report["rows"],
                "timestamp_precision": report["timestamp_precision"],
                "timezone": report["timezone"], "archive_root": str(base)}


def main() -> None:
    p = argparse.ArgumentParser(description="Mandatory actual Parquet write/read preflight; no broker connection")
    p.add_argument("--archive-root", default="runtime/tick_archive")
    args = p.parse_args()
    try:
        print(json.dumps(run_preflight(args.archive_root), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status":"FAIL","error":f"{type(exc).__name__}: {exc}",
                          "action":"Do not start collector until a real Parquet round-trip passes"},
                         ensure_ascii=False))
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
