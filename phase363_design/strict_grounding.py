"""Offline strict shape/metric-reference checks for Phase 3.6.3 design.
No broker APIs, Ollama, Discord or filesystem writes. Standard library only.
This does NOT certify the truth of free-form natural-language claims.
"""
from __future__ import annotations
import json
import math
import re
from typing import Any

VERSION = 'phase363-grounding-v1'
METRIC_KEYS = (
    'spread_p95_window_points',
    'tick_velocity_per_sec',
    'rv_mid_tick_window',
    'max_gap_seconds',
    'quote_tick_count',
)
ROLES = ('MARKET', 'QUANT', 'RISK', 'COORDINATOR')
ROLE_VERDICTS = {
    'MARKET': {'OBSERVATION', 'INSUFFICIENT_DATA'},
    'QUANT': {'TESTABLE_HYPOTHESIS', 'INSUFFICIENT_DATA'},
    'RISK': {'CHALLENGE', 'INSUFFICIENT_DATA'},
    'COORDINATOR': {'SUMMARY', 'INSUFFICIENT_DATA'},
}
# Prevent literal invented numbers from entering natural-language fields.
# It does not prove qualitative causal truth, which requires separate review.
_DIGIT_RE = re.compile(r'[0-9\u0660-\u0669\u06f0-\u06f9]')
_OTHER_INDICATORS = re.compile(r'\b(?:RSI|EMA|ATR|VWAP|MACD|OBV|DOM)\b', re.I)


def _keys_exact(d: dict, expected: set[str], title: str):
    if not isinstance(d, dict) or set(d) != expected:
        raise ValueError(f'{title} keys mismatch: missing={sorted(expected - set(d)) if isinstance(d,dict) else "not object"}; extra={sorted(set(d) - expected) if isinstance(d,dict) else "not object"}')


def validate_context(data: dict[str, Any]) -> dict[str, Any]:
    _keys_exact(data, {'schema_version','snapshot_id','symbol','source','timebase_status',
                       'baseline_ready','window_scope','metrics','limitations'}, 'context')
    if data['schema_version'] != VERSION:
        raise ValueError('Invalid schema_version')
    if not isinstance(data['snapshot_id'],str) or not re.fullmatch(r'[A-Za-z0-9._:-]{6,100}',data['snapshot_id']):
        raise ValueError('Invalid snapshot_id')
    if not isinstance(data['symbol'],str) or not re.fullmatch(r'[A-Z0-9_.#-]{1,40}',data['symbol']):
        raise ValueError('Invalid symbol')
    if data['source'] != 'PHASE36_2_ARCHIVED_QUANT_REPORT' or data['timebase_status'] != 'UNVERIFIED_PENDING_DST':
        raise ValueError('Unexpected source/timebase')
    if data['baseline_ready'] is not False or data['window_scope'] != 'SINGLE_RAW_WINDOW':
        raise ValueError('Multi-session baseline/UTC not certified')
    _keys_exact(data['metrics'],set(METRIC_KEYS),'metrics')
    for k,v in data['metrics'].items():
        if v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0):
            raise ValueError(f'Invalid metric: {k}')
    if (not isinstance(data['limitations'],list) or
        data['limitations'] != ['UNCERTIFIED_TIMEBASE','NO_MULTI_SESSION_BASELINE','TICK_RV_NOISE_PRONE']):
        raise ValueError('Must preserve all limitations')
    return data


def validate_reply(data: dict[str,Any], context: dict[str,Any], prior_replies: list[dict]|None=None) -> dict[str,Any]:
    validate_context(context)
    prior_replies = prior_replies or []
    _keys_exact(data,{'schema_version','snapshot_id','agent','verdict','evidence_refs',
                     'responds_to','claim','next_check'},'reply')
    if data['schema_version'] != VERSION or data['snapshot_id'] != context['snapshot_id']:
        raise ValueError('Snapshot mismatch')
    role = data['agent']
    if role not in ROLES or data['verdict'] not in ROLE_VERDICTS[role]:
        raise ValueError('Invalid role/verdict')
    idx=ROLES.index(role)
    if len(prior_replies)!=idx or [r['agent'] for r in prior_replies]!=list(ROLES[:idx]):
        raise ValueError('Roles must be sequential and prior replies validated')
    if data['responds_to'] != (ROLES[idx-1] if idx else None):
        raise ValueError('Must respond to immediately prior agent')
    refs=data['evidence_refs']
    if not isinstance(refs,list) or len(refs)>3 or len(refs)!=len(set(refs)):
        raise ValueError('Invalid evidence_refs')
    for key in refs:
        if not isinstance(key,str) or key not in METRIC_KEYS or context['metrics'][key] is None:
            raise ValueError('Evidence references must point to present metrics')
    if data['verdict']=='INSUFFICIENT_DATA' and len(refs)==0:
        pass
    elif not refs:
        raise ValueError('Non-insufficient verdict needs evidence reference')
    for key in ('claim','next_check'):
        s=data[key]
        if not isinstance(s,str) or not (3<=len(s)<=170) or '\n' in s or _DIGIT_RE.search(s) or _OTHER_INDICATORS.search(s):
            raise ValueError(f'Invalid {key}: no numbers, unavailable indicators, or multiline text')
    return data


def load_reply_json(raw: str, context: dict, prior_replies: list[dict]|None=None):
    if len(raw)>2400:
        raise ValueError('Response exceeds length cap')
    try:
        obj=json.loads(raw,parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonstandard number')))
    except (ValueError,TypeError) as exc:
        raise ValueError('Not strict JSON') from exc
    return validate_reply(obj,context,prior_replies)


def context_from_quant_report(report: dict) -> dict:
    """Adapt an existing 3.6.2 *read-only* report, not live shadow ticks."""
    if (report.get('tool')!='Phase 3.6.2 Read-only Quant Context' or
        report.get('ready_for_baseline') is not False or
        report.get('historical_offset_certified') is not False):
        raise ValueError('Expected unverified Phase 3.6.2 quant report')
    rows=report.get('window_metrics')
    if not isinstance(rows,list) or len(rows)!=1:
        raise ValueError('Only exactly one reviewed window is supported in draft adapter')
    row=rows[0]
    if row.get('timebase')!='BROKER_RAW_NOT_CERTIFIED_UTC' or row.get('is_tradable_signal') is not False:
        raise ValueError('Unexpected report timebase or trading flag')
    file_hash=row.get('sha256')
    if not isinstance(file_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',file_hash):
        raise ValueError('Missing source hash')
    metrics={
        'spread_p95_window_points':row.get('spread_points_p95_window_only'),
        'tick_velocity_per_sec':row.get('quote_ticks_per_second_mean_over_full_window'),
        'rv_mid_tick_window':row.get('mid_quote_realized_volatility_log_returns_window'),
        'max_gap_seconds':row.get('largest_intertick_gap_seconds'),
        'quote_tick_count':row.get('quote_tick_count'),
    }
    data={
        'schema_version':VERSION, 'snapshot_id':'raw-'+file_hash[:16],
        'symbol':report.get('symbol'), 'source':'PHASE36_2_ARCHIVED_QUANT_REPORT',
        'timebase_status':'UNVERIFIED_PENDING_DST','baseline_ready':False,
        'window_scope':'SINGLE_RAW_WINDOW','metrics':metrics,
        'limitations':['UNCERTIFIED_TIMEBASE','NO_MULTI_SESSION_BASELINE','TICK_RV_NOISE_PRONE'],
    }
    return validate_context(data)
