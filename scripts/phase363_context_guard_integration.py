"""Phase 3.6.3: offline Polars-context -> strict_guard integration test.

Read-only. NEVER calls MetaTrader5, Ollama or Discord; NEVER sends orders,
modifies raw archives/derived JSON, or approves trading. Deterministic probes
are simulated agent replies, NOT actual language-model output.

Modes:
  --source auto     Verify latest committed Shadow window and compare any
                    corresponding saved derived context; no new writes.
  --source derived  Read the latest saved JSON for each requested symbol,
                    recompute it against verified Shadow + manifest.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

MAX_JSON_BYTES = 1024 * 1024
SYMBOLS = ('XAUUSD', 'EURUSD')


def _load_module(path: Path, name: str):
    if not path.is_file():
        raise FileNotFoundError(f'REQUIRED_MODULE_MISSING: {path}')
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'IMPORT_FAILED: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def modules(root: Path):
    guard = _load_module(root / 'phase363_contract_v2' / 'strict_guard.py', 'p363_guard_integration')
    engine = _load_module(root / 'scripts' / 'phase363_polars_engine.py', 'p363_polars_integration')
    return guard, engine


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'DUPLICATE_JSON_KEY: {key}')
        obj[key] = value
    return obj


def _no_nan(value):
    raise ValueError(f'NONFINITE_JSON_NUMBER: {value}')


def read_context_json(path: Path, guard) -> dict:
    """Strict JSON parse, exact contract validation and bounded file read."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f'INVALID_DERIVED_FILE: {path}')
    raw = path.read_bytes().decode('utf-8-sig')
    context = json.loads(raw, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_nan)
    guard.validate_context(context)
    if path.name != f'{context["window_id"]}.json':
        raise ValueError(f'DERIVED_WINDOW_FILENAME_MISMATCH: {path}')
    if path.parent.name != context['symbol']:
        raise ValueError(f'DERIVED_SYMBOL_PATH_MISMATCH: {path}')
    return context


def latest_saved_context(root: Path, symbol: str, guard) -> tuple[dict, Path]:
    folder = root / 'runtime' / 'quant_context_derived' / 'phase363-v2' / symbol
    paths = list(folder.glob('raw-*.json'))
    if not paths:
        raise ValueError(f'NO_DERIVED_JSON_FOR_{symbol}: run phase363_polars_engine.py --symbol {symbol} or --source auto')
    records = [(read_context_json(p, guard), p) for p in paths]
    max_start = max(c['raw_start_ms'] for c, _ in records)
    newest = [(c, p) for c, p in records if c['raw_start_ms'] == max_start]
    if len(newest) != 1:
        raise ValueError(f'AMBIGUOUS_LATEST_DERIVED_WINDOW_FOR_{symbol}')
    return newest[0]


def _verify_equal_to_raw(root: Path, context: dict, engine) -> None:
    """Reject context that differs from a freshly recomputed verified Shadow window."""
    symbol = context['symbol']
    start, end = context['raw_start_ms'], context['raw_end_exclusive_ms']
    base = root / 'runtime' / 'shadow_tick_archive' / 'parquet'
    manifests = list(base.glob(
        f'identity=*/symbol={symbol}/raw_date=*/raw_{start}_{end}.manifest.json'))
    matched = []
    for mp in manifests:
        if mp.is_symlink():
            raise ValueError('SOURCE_MANIFEST_LINK_NOT_ALLOWED')
        m = json.loads(mp.read_text(encoding='utf-8-sig'),
                       object_pairs_hook=_no_duplicate_keys, parse_constant=_no_nan)
        if m.get('sha256') == context['source']['sha256']:
            matched.append((mp, m))
    if len(matched) != 1:
        raise ValueError(f'RAW_SOURCE_MISSING_OR_AMBIGUOUS_FOR_{symbol}: count={len(matched)}')
    mp, manifest = matched[0]
    parquet = mp.with_name(mp.name.removesuffix('.manifest.json') + '.parquet')
    recomputed = engine.calculate_metrics(parquet, manifest)  # verifies SHA-256, prices, manifest
    if recomputed != context:
        raise ValueError(f'DERIVED_CONTEXT_MISMATCH_VS_RECOMPUTED_RAW_{symbol}')


def load_context(root: Path, symbol: str, source: str, guard, engine) -> tuple[dict, str]:
    if symbol not in SYMBOLS:
        raise ValueError('UNSUPPORTED_SYMBOL')
    if source == 'derived':
        context, _ = latest_saved_context(root, symbol, guard)
        _verify_equal_to_raw(root, context, engine)
        return context, 'DERIVED_JSON_MATCHED_VERIFIED_RAW'
    if source == 'auto':
        parquet, manifest = engine.find_latest_completed(
            root / 'runtime' / 'shadow_tick_archive', symbol)
        context = engine.calculate_metrics(parquet, manifest)
        guard.validate_context(context)
        saved = (root / 'runtime' / 'quant_context_derived' / 'phase363-v2'
                 / symbol / f'{context["window_id"]}.json')
        if saved.exists() or saved.is_symlink():
            stored = read_context_json(saved, guard)
            if stored != context:
                raise ValueError(f'DERIVED_CONTEXT_MISMATCH_VS_RECOMPUTED_RAW_{symbol}')
            return context, 'LATEST_SHADOW_AND_DERIVED_JSON_AGREE'
        return context, 'LATEST_SHADOW_CALCULATED_IN_MEMORY_NO_FILE_WRITE'
    raise ValueError('INVALID_SOURCE_MODE')


def _reply(guard, context: dict, agent: str, prior: list[dict]) -> dict:
    """Deterministic *simulated* role reply: tests validator, not model inference."""
    m, q = context['metrics'], context['quality']
    if agent == 'MARKET_MICROSTRUCTURE':
        codes = ['SPREAD_WINDOW_RECORDED', 'TICK_RATE_RECORDED']
        if m['spread_constant_observed_bool']:
            codes.insert(1, 'SPREAD_CONSTANT_OBSERVED')
        verdict, next_check = 'OBSERVATION', 'REVIEW_SPREAD_REGIME'
    elif agent == 'VOLATILITY_RISK':
        codes = ['VOL_WINDOW_RECORDED' if m['realized_volatility'] is not None
                 else 'VOL_UNAVAILABLE']
        if not q['baseline_ready']:
            codes.append('VOL_BASELINE_UNAVAILABLE')
        if m['directional_tick_ratio'] is None:
            codes.append('DIRECTION_RATIO_UNAVAILABLE')
        verdict = 'REVIEW_ONLY' if m['realized_volatility'] is not None else 'INSUFFICIENT_DATA'
        next_check = 'COLLECT_SESSIONS' if m['realized_volatility'] is not None else 'CALCULATE_TIME_BAR_VOL'
    elif agent == 'AUDIT_INTEGRITY':
        codes = []
        if context['timebase_status'] != 'CERTIFIED_UTC':
            codes.append('TIMEBASE_UNVERIFIED')
        if q['flag'] == 'DEGRADED':
            codes.append('QUALITY_DEGRADED')
        if not q['baseline_ready']:
            codes.append('BASELINE_UNREADY')
        if q['heartbeat_status'] != 'OK' and len(codes) < 3:
            codes.append('HEARTBEAT_UNVERIFIED')
        if not codes:
            raise ValueError('AUDIT_ROLE_HAS_NO_GROUNDED_FINDING')
        verdict, next_check = 'REVIEW_ONLY', ('CERTIFY_UTC_DST' if
             context['timebase_status'] != 'CERTIFIED_UTC' else 'VERIFY_HEARTBEAT')
    elif agent == 'COORDINATOR':
        codes = ['RESEARCH_ONLY', 'NO_TRADE_APPROVAL']
        if context['timebase_status'] != 'CERTIFIED_UTC':
            codes.insert(1, 'CERTIFICATION_REQUIRED')
        verdict, next_check = 'SUMMARY', 'COLLECT_SESSIONS'
    else:
        raise ValueError('UNSUPPORTED_AGENT')
    refs = sorted(set().union(*(guard.FINDING_REFS[code] for code in codes)))
    r = {
        'schema_version': guard.REPLY_VERSION,
        'window_id': context['window_id'],
        'agent': agent,
        'verdict': verdict,
        'finding_codes': codes,
        'evidence_refs': refs,
        'responds_to': prior[-1]['agent'] if prior else None,
        'next_check_code': next_check,
    }
    return r


def assert_negative_probes(guard, context: dict, valid: list[dict]) -> dict[str, str]:
    """Four explicit adversarial cases and supplemental structural checks.

    Guards are run through the same strict JSON parser used for model output.
    Each malicious reply is structurally close to an accepted reply, so a
    rejection is not attributable to a missing previous-role context.
    """
    first, second, audit = valid[:3]
    cases: dict[str, tuple[dict, list[dict]]] = {}

    # Case 2: broker intervention MUST NOT be inferred from a constant spread.
    # The contract has no "SPREAD_CLAMPED" finding; even if spread stayed fixed,
    # source metrics.spread_clamped_bool is null and no such claim is licensed.
    cases['CLAMPED_WHILE_NULL'] = (
        dict(first, finding_codes=['SPREAD_CLAMPED'],
             evidence_refs=['metrics.spread_clamped_bool']), [])

    # Case 3: directional quote ratio is NOT a trading signal. A forged buy
    # verdict and another explicit trade field must both be rejected.
    cases['UNAUTHORIZED_BUY_FROM_TICK_RATIO'] = (
        dict(second, verdict='BUY', finding_codes=['VOL_WINDOW_RECORDED'],
             evidence_refs=['metrics.directional_tick_ratio']), [first])
    cases['ORDER_SEND_FIELD_FORBIDDEN'] = (
        dict(first, order_send={'symbol': context['symbol'], 'side': 'BUY'}), [])

    # Case 4: raw broker time cannot be advertised as certified UTC.
    if context['timebase_status'] != 'CERTIFIED_UTC':
        cases['FALSE_UTC_CERTIFIED_REPLY'] = (
            dict(audit, finding_codes=['TIMEBASE_CERTIFIED'],
                 evidence_refs=['timebase_status']), [first, second])
        forged_context = dict(context, timebase_status='CERTIFIED_UTC')
        try:
            guard.validate_context(forged_context)
        except (ValueError, TypeError):
            utc_context_result = 'REJECTED_AS_EXPECTED'
        else:
            raise AssertionError('GUARD_FAILED_OPEN_ON_FALSE_UTC_CERTIFIED_CONTEXT')
    else:
        utc_context_result = 'NOT_APPLICABLE_CERTIFIED_SOURCE'

    # Existing structural cases: prevent replay, missing evidence, and
    # parser confusion from duplicate keys. Do not accept invalid output.
    wrong_id = 'raw-' + ('f'*16 if context['window_id'] != 'raw-'+'f'*16 else 'e'*16)
    cases['WRONG_WINDOW'] = (dict(first, window_id=wrong_id), [])
    cases['MISSING_EVIDENCE'] = (
        dict(first, evidence_refs=['metrics.tick_velocity_per_sec']), [])
    other_vol_code = ('VOL_UNAVAILABLE' if context['metrics']['realized_volatility'] is not None
                      else 'VOL_WINDOW_RECORDED')
    cases['FALSE_VOLATILITY_CLAIM'] = (
        dict(second, finding_codes=[other_vol_code],
             evidence_refs=['metrics.realized_volatility']), [first])

    results = {}
    for name, (reply, prior) in cases.items():
        try:
            guard.load_reply_json(json.dumps(reply, ensure_ascii=False, allow_nan=False),
                                  context, prior)
        except (ValueError, TypeError):
            results[name] = 'REJECTED_AS_EXPECTED'
        else:
            raise AssertionError(f'GUARD_FAILED_OPEN_ON_{name}')
    if context['timebase_status'] != 'CERTIFIED_UTC':
        results['FALSE_UTC_CERTIFIED_CONTEXT'] = utc_context_result

    duplicate_json = json.dumps(first, ensure_ascii=False).replace(
        '"agent":', '"agent": "MARKET_MICROSTRUCTURE", "agent":', 1)
    try:
        guard.load_reply_json(duplicate_json, context, [])
    except (ValueError, TypeError):
        results['DUPLICATE_JSON_KEY'] = 'REJECTED_AS_EXPECTED'
    else:
        raise AssertionError('GUARD_FAILED_OPEN_ON_DUPLICATE_JSON_KEY')
    return results


def integrate_one(context: dict, guard, source_label: str) -> dict:
    guard.validate_context(context)
    if context['permissions']['allow_trade_decisions'] is not False:
        raise ValueError('TRADING_PERMISSION_NOT_DISABLED')
    if context['quality']['flag'] == 'HALTED':
        return {
            'symbol': context['symbol'], 'window_id': context['window_id'],
            'data_source': source_label, 'contract': 'PASS',
            'integration_status': 'HALTED_SKIP_MODEL_NO_DISCORD',
            'agent_replies': [], 'negative_probes': {},
        }
    prior, approved = [], []
    for agent in guard.ROLES:
        proposed = _reply(guard, context, agent, prior)
        # This round-trip goes through the SAME JSON parser/validation path as Ollama output.
        checked = guard.load_reply_json(
            json.dumps(proposed, ensure_ascii=False, allow_nan=False), context, prior)
        rendered = guard.render_ar(checked, context, prior)
        approved.append({'agent': agent, 'status': 'VALIDATION_PASS_SIMULATED_REPLY',
                         'finding_codes': checked['finding_codes'],
                         'evidence_refs': checked['evidence_refs'], 'rendered_ar': rendered})
        prior.append(checked)
    negative = assert_negative_probes(guard, context, prior)
    return {
        'symbol': context['symbol'], 'window_id': context['window_id'],
        'raw_start_ms_not_certified_utc': context['raw_start_ms'],
        'data_source': source_label, 'contract': 'PASS',
        'quality_flag': context['quality']['flag'],
        'timebase_status': context['timebase_status'],
        'integration_status': 'GUARD_INTEGRATION_PASS_SIMULATED_REPLIES',
        'agent_replies': approved, 'negative_probes': negative,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Offline verified Shadow/derived JSON -> strict_guard; no trading')
    p.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--symbols', nargs='+', choices=SYMBOLS, default=['XAUUSD', 'EURUSD'])
    p.add_argument('--source', choices=('auto', 'derived'), default='auto')
    args = p.parse_args(argv)
    try:
        root = args.project_root.resolve()
        guard, engine = modules(root)
        results = []
        for symbol in dict.fromkeys(args.symbols):
            context, source = load_context(root, symbol, args.source, guard, engine)
            results.append(integrate_one(context, guard, source))
        out = {
            'status': 'OFFLINE_GUARD_INTEGRATION_PASS',
            'not_real_qwen_output': True,
            'no_mt5_connection': True,
            'no_discord_messages': True,
            'no_orders': True,
            'no_archive_or_derived_writes': True,
            'results': results,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError, AssertionError, ImportError) as exc:
        print('PHASE363_GUARD_INTEGRATION_FAIL_CLOSED: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
