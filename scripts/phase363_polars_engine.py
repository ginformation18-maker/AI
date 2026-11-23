"""Phase 3.6.3: read-only, Polars-derived quote metrics from immutable Shadow.

Never uses MT5, Ollama or Discord. Does not certify broker UTC or DST.
One verified completed minute -> one strict quant_context_v2 JSON research record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid

# Keep this command isolated from live collection; Polars workers are capped by
# default BEFORE the import. Does not claim negligible CPU or latency.
os.environ.setdefault('POLARS_MAX_THREADS', '1')

SCHEMA = 'phase363-quant-context-v2'
RAW_STATUS = 'UNVERIFIED_PENDING_DST'
SYMBOLS = ('XAUUSD', 'EURUSD')
INTERVAL_MS = 5000
MAX_BYTES = 32 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_manifest(path: Path, manifest: dict) -> dict:
    """Validate committed sidecar and matching file WITHOUT modifying either."""
    required = {'sha256', 'count', 'schema_version', 'symbol', 'identity_tag',
                'window_start_raw_ms', 'window_end_raw_ms_exclusive',
                'timebase_status', 'capture_batch_id'}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise ValueError('INCOMPLETE_MANIFEST: required fields absent')
    if manifest['schema_version'] != 'shadow-v1' or manifest['timebase_status'] != RAW_STATUS:
        raise ValueError('NOT_UNVERIFIED_SHADOW_V1')
    start = manifest['window_start_raw_ms']
    end = manifest['window_end_raw_ms_exclusive']
    if type(start) is not int or type(end) is not int or start <= 0 or end-start != 60000 or start % 60000:
        raise ValueError('INVALID_RAW_WINDOW')
    if type(manifest['count']) is not int or manifest['count'] < 1:
        raise ValueError('INVALID_ROW_COUNT')
    if manifest['symbol'] not in SYMBOLS or not re.fullmatch(r'[0-9a-f]{20}', str(manifest['identity_tag'])):
        raise ValueError('INVALID_SOURCE_IDENTITY')
    if not isinstance(manifest['capture_batch_id'], str) or not manifest['capture_batch_id']:
        raise ValueError('INVALID_BATCH_ID')
    filename = f'raw_{start}_{end}.parquet'
    if path.name != filename or path.parent.parent.name != f'symbol={manifest["symbol"]}' or path.parent.parent.parent.name != f'identity={manifest["identity_tag"]}':
        raise ValueError('MANIFEST_PATH_MISMATCH')
    if not re.fullmatch(r'[0-9a-f]{64}', str(manifest['sha256'])):
        raise ValueError('INVALID_MANIFEST_SHA')
    if not path.is_file() or path.is_symlink():
        raise ValueError('ARCHIVE_FILE_MISSING_OR_LINK')
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('WINDOW_FILE_TOO_LARGE')
    # Shadow commits Parquet before the manifest. Require BOTH, and no adjacent
    # writer-in-progress marker, then compare bytes against SHA256.
    sidecar = path.with_suffix('.manifest.json')
    if not sidecar.is_file() or sidecar.is_symlink():
        raise ValueError('MISSING_COMMITTED_MANIFEST')
    if sha256_file(path) != manifest['sha256']:
        raise ValueError('SHA256_MISMATCH')
    return manifest


def find_latest_completed(root: Path, symbol: str, identity_tag: str | None = None) -> tuple[Path, dict]:
    """Discover completed manifests, reject ambiguous broker identities."""
    if symbol not in SYMBOLS:
        raise ValueError('UNSUPPORTED_SYMBOL')
    if identity_tag is not None and not re.fullmatch(r'[0-9a-f]{20}', identity_tag):
        raise ValueError('BAD_IDENTITY_TAG')
    base = root / 'parquet'
    candidates = sorted(base.glob(f'identity=*/symbol={symbol}/raw_date=*/raw_*_*.manifest.json'))
    if identity_tag is not None:
        candidates = [p for p in candidates if p.parent.parent.parent.name == f'identity={identity_tag}']
    if not candidates:
        raise ValueError(f'NO_COMMITTED_MANIFEST_FOR_{symbol}')
    ids = {p.parent.parent.parent.name for p in candidates}
    if len(ids) != 1:
        raise ValueError('MULTIPLE_BROKER_IDENTITIES_REQUIRE_--identity-tag')
    # Ignore in-progress Parquet without a sidecar by construction.
    def raw_start(p: Path) -> int:
        match = re.fullmatch(r'raw_(\d+)_(\d+)\.manifest\.json', p.name)
        return int(match.group(1)) if match else -1
    latest = max(candidates, key=raw_start)
    parquet = latest.with_name(latest.name.replace('.manifest.json', '.parquet'))
    manifest = json.loads(latest.read_text(encoding='utf-8'))
    verify_manifest(parquet, manifest)
    return parquet, manifest


def calculate_metrics(path: Path, manifest: dict) -> dict:
    """Perform vectorized calculations on one immutable verified minute."""
    verify_manifest(path, manifest)
    try:
        import polars as pl
    except ImportError as exc:
        raise RuntimeError('Polars is not installed in this Python environment: pip install polars') from exc

    cols = ['raw_time_msc', 'bid', 'ask', 'spread_points', 'capture_batch_id', 'timebase_status']
    frame = pl.read_parquet(path, columns=cols)
    if frame.height != manifest['count']:
        raise ValueError('ROW_COUNT_MISMATCH')
    if sum(frame.null_count().row(0)) != 0:
        raise ValueError('NULL_RAW_FIELDS')
    start, end = manifest['window_start_raw_ms'], manifest['window_end_raw_ms_exclusive']
    check = frame.select(
        ((pl.col('raw_time_msc') < start) | (pl.col('raw_time_msc') >= end)).sum().alias('out_of_bounds'),
        (pl.col('raw_time_msc').diff() < 0).sum().alias('out_of_order'),
        ((pl.col('bid') <= 0) | (pl.col('ask') <= pl.col('bid')) | (~pl.col('bid').is_finite()) | (~pl.col('ask').is_finite()) | (pl.col('spread_points') < 0) | (~pl.col('spread_points').is_finite())).sum().alias('bad_prices'),
        (pl.col('timebase_status') != RAW_STATUS).sum().alias('bad_timebase'),
        (pl.col('capture_batch_id') != manifest['capture_batch_id']).sum().alias('bad_batch'),
    ).row(0, named=True)
    if any(int(value or 0) != 0 for value in check.values()):
        raise ValueError(f'INVALID_RAW_ROWS: {check}')

    # integer floor-bucketing is equivalent to 5-second truncate on a raw
    # broker-clock coordinate; DO NOT label this coordinate UTC.
    quotes = frame.with_columns(
        ((pl.col('bid') + pl.col('ask')) / 2.0).alias('mid'),
        ((pl.col('raw_time_msc') // INTERVAL_MS) * INTERVAL_MS).alias('bucket_raw_ms'),
    )
    stats = quotes.select(
        pl.len().alias('tick_count'),
        pl.col('spread_points').quantile(0.50, interpolation='linear').alias('spread_p50'),
        pl.col('spread_points').quantile(0.95, interpolation='linear').alias('spread_p95'),
        pl.col('spread_points').min().alias('spread_min'),
        pl.col('spread_points').max().alias('spread_max'),
        (pl.col('raw_time_msc').diff().max()/1000).alias('gap_s'),
        (pl.col('mid').diff() > 0).sum().alias('up'),
        (pl.col('mid').diff() < 0).sum().alias('down'),
    ).row(0, named=True)
    # Only OBSERVED 5s bins. Missing bins stay missing (no fill / upsample).
    bars = (quotes.group_by('bucket_raw_ms')
            .agg(pl.col('mid').last().alias('mid_last'))
            .sort('bucket_raw_ms'))
    returns = (bars.with_columns(
        (pl.col('bucket_raw_ms') - pl.col('bucket_raw_ms').shift(1)).alias('step_ms'),
        (pl.col('mid_last') / pl.col('mid_last').shift(1)).log().alias('log_return'))
        .filter(pl.col('step_ms') == INTERVAL_MS)
        .select('log_return'))
    sample_count = returns.height
    # Contract requires >=3 observations for STD_LOG_RETURNS_5S.
    if sample_count >= 3:
        rv = returns.select(pl.col('log_return').std(ddof=1)).item()
        if rv is None or not math.isfinite(rv) or rv < 0:
            raise ValueError('NONFINITE_VOLATILITY')
        method, rv_n = 'STD_LOG_RETURNS_5S', sample_count
    else:
        method, rv, rv_n = 'NOT_COMPUTED', None, 0
    up, down = int(stats['up']), int(stats['down'])
    reasons = ['TIMEBASE_UNVERIFIED', 'BASELINE_UNREADY', 'HEARTBEAT_UNVERIFIED']
    if sample_count < 3:
        reasons.append('INSUFFICIENT_SAMPLED_RETURNS')
    if up+down == 0:
        reasons.append('NO_PRICE_MOVES')
    window_id = 'raw-' + hashlib.sha256(
        f'{manifest["symbol"]}|{manifest["identity_tag"]}|{start}|{end}|{manifest["sha256"]}'.encode('ascii')
    ).hexdigest()[:16]
    context = {
        'schema_version': SCHEMA,
        'window_id': window_id,
        'symbol': manifest['symbol'],
        'raw_start_ms': start,
        'raw_end_exclusive_ms': end,
        'utc_start_ms': None,
        'timebase_status': RAW_STATUS,
        'source': {'kind': 'SHADOW_RAW_PARQUET', 'sha256': manifest['sha256'], 'collector_schema': 'shadow-v1'},
        'metrics': {
            'tick_count': int(stats['tick_count']),
            'tick_velocity_per_sec': round(int(stats['tick_count']) / ((end - start)/1000), 8),
            'spread_p50_points': float(stats['spread_p50']),
            'spread_p95_points': float(stats['spread_p95']),
            'spread_constant_observed_bool': bool(float(stats['spread_max']) - float(stats['spread_min']) <= 1e-6),
            'spread_clamped_bool': None,
            'realized_volatility': float(rv) if rv is not None else None,
            'realized_volatility_method': method,
            'rv_sample_count': rv_n,
            'rolling_z_score_vol': None,
            'rolling_baseline_window_count': 0,
            'directional_tick_ratio': up/(up+down) if (up+down) else None,
            'max_gap_seconds': float(stats['gap_s']) if stats['gap_s'] is not None else None,
        },
        'quality': {'flag': 'DEGRADED', 'reasons': reasons,
                    'baseline_ready': False, 'five_sessions_certified': False,
                    'heartbeat_status': 'UNVERIFIED'},
        'permissions': {'allow_discussion': True, 'allow_trade_decisions': False},
    }
    # Validate exact semantic contract in the already installed design folder.
    contract_dir = Path(__file__).resolve().parents[1] / 'phase363_contract_v2'
    if not (contract_dir / 'strict_guard.py').is_file():
        raise RuntimeError('Missing phase363_contract_v2/strict_guard.py; install contract v2 first')
    if str(contract_dir) not in sys.path:
        sys.path.insert(0, str(contract_dir))
    from strict_guard import validate_context
    validate_context(context)
    return context


def save_derived_only(root: Path, context: dict) -> Path:
    """Atomic create-if-absent of a derived JSON only. Raw archives never edited."""
    target = root / 'runtime' / 'quant_context_derived' / 'phase363-v2' / context['symbol'] / f'{context["window_id"]}.json'
    encoded = (json.dumps(context, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != encoded:
            raise ValueError('DERIVED_FILE_CONFLICT: refusing overwrite')
        return target
    tmp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        with tmp.open('xb') as fd:
            fd.write(encoded)
            fd.flush()
            os.fsync(fd.fileno())
        os.link(tmp, target)  # atomic no-overwrite on local NTFS/normal filesystems
    except FileExistsError:
        if not target.is_file() or target.read_bytes() != encoded:
            raise ValueError('DERIVED_FILE_CONFLICT')
    finally:
        tmp.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description='Read-only Shadow -> quantitative JSON (research only)')
    parser.add_argument('--symbol', choices=SYMBOLS, required=True)
    parser.add_argument('--identity-tag', default=None, help='Required if more than one broker account exists in Shadow')
    parser.add_argument('--stdout-only', action='store_true', help='Print JSON; do not write derived JSON')
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    path, manifest = find_latest_completed(root / 'runtime' / 'shadow_tick_archive', args.symbol, args.identity_tag)
    context = calculate_metrics(path, manifest)
    if args.stdout_only:
        print(json.dumps(context, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        saved = save_derived_only(root, context)
        print(json.dumps({'status':'DERIVED_QUANT_CONTEXT_ONLY', 'research_only':True,
                          'archive_unchanged':True,'utc_dst_certified':False,
                          'symbol':args.symbol, 'window_id':context['window_id'],
                          'file':str(saved),'metrics':context['metrics']},ensure_ascii=False,indent=2,allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f'PHASE363_FAIL_CLOSED: {exc}', file=sys.stderr)
        raise SystemExit(2) from exc
