"""Read-only report over phase 3.6 shadow manifests and Parquet files."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mt5_agents.phase36_session_coverage import audit

def main():
    parser = argparse.ArgumentParser(description='READ-ONLY shadow coverage: sessions/DST candidates, gaps, host cadence, file sizes')
    parser.add_argument('--deep', action='store_true', help='Additionally verify Parquet schema, row count, raw timestamps (requires pyarrow)')
    args = parser.parse_args()
    report = audit(ROOT / 'runtime' / 'shadow_tick_archive', deep=args.deep)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report['assessment'] == 'AUDIT_FAILED':
        raise SystemExit(2)

if __name__ == '__main__':
    main()
