"""Phase 3.6.2 offline, read-only quote-tick statistical context.

Important: all tick times here are RAW MT5 broker labels, NOT certified UTC.
Metrics are descriptive only; no inferred order flow, trade volumes, alpha,
orders, UTC correction, or readiness for model training is claimed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import re

import numpy as np

from .parquet_integrity import verify_parquet

_WINDOW_RE = re.compile(r"^ticks_(\d{13})_(\d{13})\.parquet$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_.#-]{1,40}$")
MAX_FILE_TICKS = 250_000


def _f(value: float, ndigits: int = 8) -> float:
    x = float(value)
    if not math.isfinite(x):
        raise ValueError("Non-finite computed statistic")
    return round(x, ndigits)


def _validate_arrays(ms, bid, ask, spread_points, start_ms: int, end_ms: int):
    """Validate market data and raw window bounds, never fix/sort/drop a row."""
    ms = np.asarray(ms)
    bid = np.asarray(bid, dtype=np.float64)
    ask = np.asarray(ask, dtype=np.float64)
    sp = np.asarray(spread_points, dtype=np.float64)
    n = len(ms)
    if n == 0 or n > MAX_FILE_TICKS or not (len(bid) == len(ask) == len(sp) == n):
        raise ValueError("Empty, excessive or unequal-length tick arrays")
    if not np.issubdtype(ms.dtype, np.integer):
        raise ValueError("time_msc must be an integer millisecond array")
    ms = ms.astype(np.int64, copy=False)
    if not (0 <= start_ms < end_ms and (end_ms - start_ms) % 1000 == 0
            and 10 <= (end_ms - start_ms) / 1000 <= 120):
        raise ValueError("Invalid bounded raw broker-clock window")
    if ms[0] < start_ms or ms[-1] >= end_ms or np.any(np.diff(ms) < 0):
        raise ValueError("Tick timestamps outside half-open raw window or out of order")
    if (not np.isfinite(bid).all() or not np.isfinite(ask).all()
            or not np.isfinite(sp).all() or np.any(bid <= 0)
            or np.any(ask <= bid) or np.any(sp <= 0)):
        raise ValueError("Invalid quote or spread")
    # The archived spread must correspond to a *single consistent* symbol point
    # size; otherwise statistics in 'points' are not comparable.
    point_estimates = (ask - bid) / sp
    p = float(np.median(point_estimates))
    if not (math.isfinite(p) and p > 0 and np.allclose(point_estimates, p, rtol=0.005, atol=1e-10)):
        raise ValueError("Spread points inconsistent with Bid/Ask or symbol point size")
    return ms, bid, ask, sp, p


def compute_window_context(ms, bid, ask, spread_points, *, start_ms: int, end_ms: int) -> dict:
    """Compute descriptive metrics for a single raw MT5 tick window.

    No UTC clock meaning is assigned to start/end, and no statistical baseline
    is claimed even if the window happens to have many quote updates.
    """
    ms, bid, ask, sp, point = _validate_arrays(ms, bid, ask, spread_points, start_ms, end_ms)
    elapsed_s = (end_ms - start_ms) / 1000
    gaps = np.diff(ms)
    max_gap_s = float(np.max(gaps) / 1000) if len(gaps) else elapsed_s
    first_edge_s = float((ms[0] - start_ms) / 1000)
    last_edge_s = float((end_ms - ms[-1]) / 1000)
    mid = (bid / 2) + (ask / 2)
    log_returns = np.diff(np.log(mid))
    # Mid-quote realized variation: quote microstructure noise may inflate it.
    # It is window-local, not annualized, and does not estimate traded volume.
    realized_volatility = float(np.sqrt(np.sum(np.square(log_returns), dtype=np.float64)))
    one_second_counts = np.bincount(((ms - start_ms) // 1000).astype(np.int64),
                                    minlength=int(elapsed_s))
    covers_window = bool(first_edge_s <= 5 and last_edge_s <= 5 and max_gap_s <= 10)
    return {
        "raw_window_start_ms": int(start_ms),
        "raw_window_end_exclusive_ms": int(end_ms),
        "timebase": "BROKER_RAW_NOT_CERTIFIED_UTC",
        "quote_tick_count": int(len(ms)),
        "spread_points_first": _f(sp[0], 5),
        "spread_points_last": _f(sp[-1], 5),
        "spread_points_p50": _f(np.percentile(sp, 50), 5),
        "spread_points_p95_window_only": _f(np.percentile(sp, 95), 5),
        "spread_points_max": _f(np.max(sp), 5),
        "spread_change_points_last_minus_first": _f(sp[-1] - sp[0], 5),
        "quote_ticks_per_second_mean_over_full_window": _f(len(ms) / elapsed_s, 5),
        "quote_ticks_per_second_p95_across_one_second_buckets": _f(np.percentile(one_second_counts, 95), 5),
        "seconds_with_quote_update": int(np.count_nonzero(one_second_counts)),
        "mid_quote_realized_volatility_log_returns_window": _f(realized_volatility, 10),
        "largest_intertick_gap_seconds": _f(max_gap_s, 3),
        "first_quote_edge_lag_seconds": _f(first_edge_s, 3),
        "last_quote_edge_lag_seconds": _f(last_edge_s, 3),
        "edge_and_gap_coverage_candidate": covers_window,
        "point_size_inferred_from_archive": _f(point, 10),
        "is_historical_statistical_baseline": False,
        "is_tradable_signal": False,
    }


def inspect_archive_quant(archive_root: Path, symbol: str = "XAUUSD", *, max_files: int = 10000, details_limit: int = 10) -> dict:
    """Read verified Parquet windows, fail closed on corruption/partial coverage.

    Never writes files. The caller is responsible for ensuring this is an
    observation-only Demo project; the stand-alone script enforces that too.
    """
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("Invalid symbol")
    root = Path(archive_root)
    if not root.is_dir():
        raise FileNotFoundError("Archive directory not found")
    paths = sorted(p for p in root.rglob("*.parquet") if f"symbol={symbol}" in p.parts)
    if not paths:
        raise ValueError(f"No project archive files for {symbol}")
    if max_files < 1 or len(paths) > max_files or details_limit < 1:
        raise ValueError("Unsafe audit limit or partial archive; no partial baseline permitted")
    import pyarrow.parquet as pq

    entries = []
    windows = set()
    raw_dates = Counter()
    for path in paths:
        match = _WINDOW_RE.fullmatch(path.name)
        if not match:
            raise ValueError("Unexpected Parquet window filename")
        start_ms, end_ms = map(int, match.groups())
        if (start_ms, end_ms) in windows:
            raise ValueError("Duplicate archive window")
        windows.add((start_ms, end_ms))
        checked = verify_parquet(path)
        if checked["rows"] > MAX_FILE_TICKS:
            raise ValueError("Excessive rows in Parquet window")
        # The dedicated verifier above already checks the lossless Arrow schema,
        # timestamp precision, ordering, nulls, and quote validity.
        frame = pq.ParquetFile(path).read(columns=["time_msc", "bid", "ask", "spread_points"]).to_pandas()
        features = compute_window_context(frame.time_msc.to_numpy(), frame.bid.to_numpy(),
                                          frame.ask.to_numpy(), frame.spread_points.to_numpy(),
                                          start_ms=start_ms, end_ms=end_ms)
        features["file"] = path.name
        # Deterministic evidence fingerprint, not a certification of true UTC.
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        features["sha256"] = digest.hexdigest()
        entries.append(features)
        raw_dates[datetime.fromtimestamp(start_ms / 1000, timezone.utc).date().isoformat()] += 1
    total_ticks = sum(x["quote_tick_count"] for x in entries)
    complete_coverage = sum(int(x["edge_and_gap_coverage_candidate"]) for x in entries)
    return {
        "tool": "Phase 3.6.2 Read-only Quant Context",
        "read_only": True,
        "writes_archive": False,
        "sends_orders": False,
        "symbol": symbol,
        "timebase": "BROKER_RAW_NOT_CERTIFIED_UTC",
        "historical_offset_certified": False,
        "ready_for_baseline": False,
        "baseline_blockers": [
            "Historical UTC offset / DST evidence not certified",
            "Five independently verified broker sessions not yet demonstrated",
            "Current quote-window percentiles must not be used as multi-session reference percentiles",
        ],
        "archived_windows_read": len(entries),
        "total_quote_ticks": total_ticks,
        "raw_broker_label_dates_not_utc_dates": dict(sorted(raw_dates.items())),
        "windows_with_candidate_edge_and_gap_coverage": complete_coverage,
        "window_metrics": entries[-details_limit:],
        "windows_verified_but_not_listed": max(0, len(entries) - details_limit),
        "note": "Quote updates are NOT exchange trade volume or order-book depth. Numbers are descriptive only.",
    }
