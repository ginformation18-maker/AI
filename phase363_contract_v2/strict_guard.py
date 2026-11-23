"""Strict, offline, research-only Phase 3.6.3 contract guard.

Stdlib only. No MetaTrader5/Ollama/Discord imports, no market API, no orders,
no file writes. Schema + semantic checks are needed; neither proves
truth of economic interpretation or statistical validity of inputs.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
VERSION = 'phase363-quant-context-v2'
REPLY_VERSION = 'phase363-agent-reply-v2'
ROLES = ('MARKET_MICROSTRUCTURE','VOLATILITY_RISK','AUDIT_INTEGRITY','COORDINATOR')
ROLE_CODES = {
    ROLES[0]: {'SPREAD_WINDOW_RECORDED','SPREAD_CONSTANT_OBSERVED','TICK_RATE_RECORDED'},
    ROLES[1]: {'VOL_UNAVAILABLE','VOL_WINDOW_RECORDED','VOL_BASELINE_UNAVAILABLE','DIRECTION_RATIO_UNAVAILABLE'},
    ROLES[2]: {'TIMEBASE_UNVERIFIED','BASELINE_UNREADY','HEARTBEAT_UNVERIFIED','QUALITY_DEGRADED','QUALITY_HALTED'},
    ROLES[3]: {'RESEARCH_ONLY','CERTIFICATION_REQUIRED','NO_TRADE_APPROVAL'},
}
EXPECTED_VERDICTS = {
    ROLES[0]: {'OBSERVATION','INSUFFICIENT_DATA'},
    ROLES[1]: {'REVIEW_ONLY','INSUFFICIENT_DATA'},
    ROLES[2]: {'REVIEW_ONLY','HALTED'},
    ROLES[3]: {'SUMMARY'},
}
FINDING_REFS = {
    'SPREAD_WINDOW_RECORDED': {'metrics.spread_p50_points','metrics.spread_p95_points'},
    'SPREAD_CONSTANT_OBSERVED': {'metrics.spread_constant_observed_bool'},
    'TICK_RATE_RECORDED': {'metrics.tick_velocity_per_sec'},
    'VOL_UNAVAILABLE': {'metrics.realized_volatility'},
    'VOL_WINDOW_RECORDED': {'metrics.realized_volatility'},
    'VOL_BASELINE_UNAVAILABLE': {'quality.baseline_ready'},
    'DIRECTION_RATIO_UNAVAILABLE': {'metrics.directional_tick_ratio'},
    'TIMEBASE_UNVERIFIED': {'timebase_status'},
    'BASELINE_UNREADY': {'quality.baseline_ready'},
    'HEARTBEAT_UNVERIFIED': {'quality.heartbeat_status'},
    'QUALITY_DEGRADED': {'quality.flag'},
    'QUALITY_HALTED': {'quality.flag'},
    'RESEARCH_ONLY': {'quality.flag'},
    'CERTIFICATION_REQUIRED': {'timebase_status'},
    'NO_TRADE_APPROVAL': {'quality.flag'},
}
CHECK_CODES = {'CERTIFY_UTC_DST','COLLECT_SESSIONS','VERIFY_HEARTBEAT','CALCULATE_TIME_BAR_VOL','REVIEW_SPREAD_REGIME','NONE'}


def _fail(msg: str):
    raise ValueError(msg)


def _exact_dict(obj: Any, keys: set[str], label: str) -> dict:
    if not isinstance(obj, dict) or set(obj) != keys:
        _fail(f'{label}: required keys exactly {sorted(keys)}')
    return obj


def _enum(value, options, name):
    if value not in options:
        _fail(f'{name}: invalid choice')


def _bool(value, name):
    if type(value) is not bool:
        _fail(f'{name}: boolean required')


def _num(value, name, minimum: float | None = 0, maximum: float | None = None, nullable: bool = False):
    if value is None and nullable:
        return
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail(f'{name}: finite number required')
    if minimum is not None and value < minimum:
        _fail(f'{name}: below range')
    if maximum is not None and value > maximum:
        _fail(f'{name}: above range')


def _int(value, name, minimum: int = 0):
    if type(value) is not int or value < minimum:
        _fail(f'{name}: nonnegative integer required')


def _get(context: dict, path: str):
    value = context
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            _fail('Bad evidence path')
        value = value[part]
    return value


def validate_context(c: dict[str, Any]) -> dict[str, Any]:
    _exact_dict(c, {'schema_version','window_id','symbol','raw_start_ms','raw_end_exclusive_ms',
                    'utc_start_ms','timebase_status','source','metrics','quality','permissions'}, 'context')
    if c['schema_version'] != VERSION:
        _fail('context: version')
    if not isinstance(c['window_id'], str) or not re.fullmatch(r'raw-[0-9a-f]{16}',c['window_id']):
        _fail('context: window id')
    _enum(c['symbol'],('XAUUSD','EURUSD'),'symbol')
    _int(c['raw_start_ms'],'raw_start_ms')
    _int(c['raw_end_exclusive_ms'],'raw_end_exclusive_ms')
    if c['raw_start_ms'] >= c['raw_end_exclusive_ms'] or c['raw_end_exclusive_ms']-c['raw_start_ms']>24*60*60*1000:
        _fail('Bad raw time window')
    _enum(c['timebase_status'],('UNVERIFIED_PENDING_DST','CERTIFIED_UTC','TIMEBASE_MISMATCH'),'timebase_status')
    if c['utc_start_ms'] is not None:
        _int(c['utc_start_ms'],'utc_start_ms')
    if c['timebase_status'] != 'CERTIFIED_UTC' and c['utc_start_ms'] is not None:
        _fail('Unverified UTC cannot be populated')
    if c['timebase_status'] == 'CERTIFIED_UTC' and c['utc_start_ms'] is None:
        _fail('Certified UTC needs evidence-backed timestamp')
    s=_exact_dict(c['source'],{'kind','sha256','collector_schema'},'source')
    _enum(s['kind'],('SHADOW_RAW_PARQUET','LEGACY_READONLY_PARQUET','CERTIFIED_DERIVED'),'source kind')
    if not isinstance(s['sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',s['sha256']):
        _fail('Invalid source digest')
    if not isinstance(s['collector_schema'],str) or not 1<=len(s['collector_schema'])<=60:
        _fail('Invalid collector schema')
    if c['timebase_status']=='CERTIFIED_UTC' and s['kind']!='CERTIFIED_DERIVED':
        _fail('Certified UTC requires independently certified derived source')
    m=_exact_dict(c['metrics'],{'tick_count','tick_velocity_per_sec','spread_p50_points','spread_p95_points',
          'spread_constant_observed_bool','spread_clamped_bool','realized_volatility',
          'realized_volatility_method','rv_sample_count','rolling_z_score_vol',
          'rolling_baseline_window_count','directional_tick_ratio','max_gap_seconds'},'metrics')
    _int(m['tick_count'],'tick_count',1)
    for name in ('tick_velocity_per_sec','spread_p50_points','spread_p95_points'):
        _num(m[name],name)
    if m['spread_p95_points'] < m['spread_p50_points']:
        _fail('P95 cannot be below P50')
    _bool(m['spread_constant_observed_bool'],'spread_constant_observed_bool')
    if m['spread_clamped_bool'] is not None:
        _bool(m['spread_clamped_bool'],'spread_clamped_bool')
    if m['spread_clamped_bool'] is True:
        _fail('Broker clamping cannot be certified by quote-window statistics')
    _enum(m['realized_volatility_method'],('NOT_COMPUTED','STD_LOG_RETURNS_1S','STD_LOG_RETURNS_5S'),'realized_volatility_method')
    _int(m['rv_sample_count'],'rv_sample_count')
    _num(m['realized_volatility'],'realized_volatility',nullable=True)
    if m['realized_volatility_method']=='NOT_COMPUTED' and (m['realized_volatility'] is not None or m['rv_sample_count'] != 0):
        _fail('Not-computed volatility must be null with zero sample count')
    if m['realized_volatility_method']!='NOT_COMPUTED' and (m['realized_volatility'] is None or m['rv_sample_count']<3):
        _fail('Time-bar return volatility requires at least three valid returns')
    _num(m['rolling_z_score_vol'],'rolling_z_score_vol',minimum=None,nullable=True)
    _int(m['rolling_baseline_window_count'],'rolling_baseline_window_count')
    _num(m['directional_tick_ratio'],'directional_tick_ratio',maximum=1,nullable=True)
    _num(m['max_gap_seconds'],'max_gap_seconds',nullable=True)
    q=_exact_dict(c['quality'],{'flag','reasons','baseline_ready','five_sessions_certified','heartbeat_status'},'quality')
    _enum(q['flag'],('PASSED','DEGRADED','HALTED'),'quality.flag')
    if not isinstance(q['reasons'],list) or len(q['reasons'])>12 or len(q['reasons'])!=len(set(map(str,q['reasons']))) or any(not isinstance(x,str) or x not in {'TIMEBASE_UNVERIFIED','TIMEBASE_MISMATCH','BASELINE_UNREADY','HEARTBEAT_UNVERIFIED','SOURCE_UNVERIFIED','INCOMPLETE_WINDOW','NO_PRICE_MOVES','INSUFFICIENT_SAMPLED_RETURNS','OTHER_CHECK_REQUIRED'} for x in q['reasons']):
        _fail('Invalid quality reasons')
    _bool(q['baseline_ready'],'baseline_ready')
    _bool(q['five_sessions_certified'],'five_sessions_certified')
    _enum(q['heartbeat_status'],('UNVERIFIED','OK','GAP_DETECTED'),'heartbeat_status')
    if c['timebase_status'] != 'CERTIFIED_UTC' and (q['flag']=='PASSED' or q['baseline_ready'] or q['five_sessions_certified'] or m['rolling_z_score_vol'] is not None):
        _fail('Unverified timebase cannot establish a certified baseline')
    if c['timebase_status'] == 'TIMEBASE_MISMATCH' and q['flag'] != 'HALTED':
        _fail('Timebase mismatch must halt')
    if m['rolling_z_score_vol'] is not None and (not q['baseline_ready'] or not q['five_sessions_certified'] or m['rolling_baseline_window_count']<30 or m['realized_volatility'] is None):
        _fail('Z-score requires certified comparable baseline')
    if not q['baseline_ready'] and m['rolling_z_score_vol'] is not None:
        _fail('No baseline, no Z-score')
    p=_exact_dict(c['permissions'],{'allow_discussion','allow_trade_decisions'},'permissions')
    if p['allow_discussion'] is not True or p['allow_trade_decisions'] is not False:
        _fail('Research-only permissions required')
    return c


def _finding_is_grounded(code, c):
    m=c['metrics']; q=c['quality']
    return {
      'SPREAD_WINDOW_RECORDED': True,
      'SPREAD_CONSTANT_OBSERVED': m['spread_constant_observed_bool'] is True,
      'TICK_RATE_RECORDED': True,
      'VOL_UNAVAILABLE': m['realized_volatility'] is None,
      'VOL_WINDOW_RECORDED': m['realized_volatility'] is not None,
      'VOL_BASELINE_UNAVAILABLE': q['baseline_ready'] is False,
      'DIRECTION_RATIO_UNAVAILABLE': m['directional_tick_ratio'] is None,
      'TIMEBASE_UNVERIFIED': c['timebase_status'] != 'CERTIFIED_UTC',
      'BASELINE_UNREADY': q['baseline_ready'] is False,
      'HEARTBEAT_UNVERIFIED': q['heartbeat_status'] != 'OK',
      'QUALITY_DEGRADED': q['flag'] == 'DEGRADED',
      'QUALITY_HALTED': q['flag'] == 'HALTED',
      'RESEARCH_ONLY': True,
      'CERTIFICATION_REQUIRED': c['timebase_status'] != 'CERTIFIED_UTC',
      'NO_TRADE_APPROVAL': True,
    }[code]


def validate_reply(reply: dict[str, Any], context: dict[str, Any], prior_replies: list[dict] | None = None) -> dict:
    validate_context(context)
    if context['quality']['flag']=='HALTED':
        _fail('HALTED data: skip Ollama; produce deterministic local alert')
    r=_exact_dict(reply, {'schema_version','window_id','agent','verdict','finding_codes',
                         'evidence_refs','responds_to','next_check_code'}, 'agent reply')
    if r['schema_version'] != REPLY_VERSION or r['window_id'] != context['window_id']:
        _fail('Agent version/window mismatch')
    _enum(r['agent'], ROLES, 'agent')
    if r['verdict'] not in EXPECTED_VERDICTS[r['agent']]:
        _fail('Invalid verdict for role')
    prior_replies=prior_replies or []
    idx=ROLES.index(r['agent'])
    if len(prior_replies)!=idx or any(x.get('agent')!=ROLES[i] for i,x in enumerate(prior_replies)):
        _fail('Agent sequence mismatch')
    if r['responds_to'] != (ROLES[idx-1] if idx else None):
        _fail('reply_to mismatch')
    codes=r['finding_codes']; refs=r['evidence_refs']
    if not isinstance(codes,list) or not 1<=len(codes)<=3 or len(codes)!=len(set(map(str,codes))) or any(type(code) is not str or code not in ROLE_CODES[r['agent']] for code in codes):
        _fail('Invalid finding codes for role')
    if not isinstance(refs,list) or not 1<=len(refs)<=8 or len(refs)!=len(set(map(str,refs))) or any(type(x) is not str or x not in {
        'timebase_status','quality.flag','quality.baseline_ready','quality.heartbeat_status',
        'metrics.tick_count','metrics.tick_velocity_per_sec','metrics.spread_p50_points','metrics.spread_p95_points',
        'metrics.spread_constant_observed_bool','metrics.spread_clamped_bool','metrics.realized_volatility',
        'metrics.rolling_z_score_vol','metrics.directional_tick_ratio','metrics.max_gap_seconds'} for x in refs):
        _fail('Invalid evidence references')
    needed=set().union(*(FINDING_REFS[x] for x in codes))
    if not needed.issubset(set(refs)):
        _fail('Missing references for findings')
    if not all(_finding_is_grounded(x,context) for x in codes):
        _fail('Claim contradicts source context')
    # Null-valued evidence is permitted ONLY for explicit absence claims.
    null_ok={'metrics.realized_volatility','metrics.directional_tick_ratio'}
    if any(_get(context,x) is None and x not in null_ok for x in refs):
        _fail('Evidence reference is null')
    _enum(r['next_check_code'],CHECK_CODES,'next_check_code')
    return r


def load_reply_json(raw: str, context: dict, prior_replies: list[dict] | None = None) -> dict:
    if not isinstance(raw,str) or len(raw)>2000:
        _fail('Too long or invalid model output')
    def no_duplicates(pairs):
        d={}
        for k,v in pairs:
            if k in d: _fail('Duplicate JSON key')
            d[k]=v
        return d
    try:
        data=json.loads(raw,object_pairs_hook=no_duplicates,
            parse_constant=lambda x: _fail('Non-finite JSON numbers'))
    except (json.JSONDecodeError,TypeError) as e:
        raise ValueError('Invalid JSON') from e
    return validate_reply(data, context,prior_replies)


LABELS={
 'SPREAD_WINDOW_RECORDED': lambda c:f"سبريد النافذة: الوسيط {c['metrics']['spread_p50_points']} نقطة، والمئين الأعلى P95 {c['metrics']['spread_p95_points']} نقطة؛ هذه ليست مرجعية متعددة الجلسات.",
 'SPREAD_CONSTANT_OBSERVED':lambda c:'السبريد ثابت في هذه النافذة المرصودة؛ لا يمكن إثبات تثبيت اصطناعي من الوسيط بهذا وحده.',
 'TICK_RATE_RECORDED':lambda c:f"متوسط تحديث الأسعار المسجل {c['metrics']['tick_velocity_per_sec']} في الثانية؛ وليس حجم صفقات متداولة.",
 'VOL_UNAVAILABLE':lambda c:'التقلب المعاد حسابه من شموع زمنية متساوية غير متاح في هذه النافذة.',
 'VOL_WINDOW_RECORDED':lambda c:f"التقلب الوصفي وفق {c['metrics']['realized_volatility_method']}: {c['metrics']['realized_volatility']}، بلا توصية تداول.",
 'VOL_BASELINE_UNAVAILABLE':lambda c:'مرجع التقلب متعدد الجلسات غير معتمد؛ لا يجوز تصنيف التقلب شاذًا.',
 'DIRECTION_RATIO_UNAVAILABLE':lambda c:'نسبة التكات الصاعدة والهابطة غير محسوبة؛ لا نستنتج اتجاه السوق.',
 'TIMEBASE_UNVERIFIED':lambda c:'مرجع توقيت الوسيط غير معتمد UTC/DST، فلا تُنسب له جلسة سيولة مؤكدة.',
 'BASELINE_UNREADY':lambda c:'المرجع الإحصائي لم يستوف شروط الاعتماد.',
 'HEARTBEAT_UNVERIFIED':lambda c:'حالة استمرار المراقبة غير مؤكدة لهذه النافذة.',
 'QUALITY_DEGRADED':lambda c:'تقرير الجودة DEGRADED: القراءة وصفية للتدقيق فقط.',
 'QUALITY_HALTED':lambda c:'تقرير الجودة HALTED: البيانات لا تصلح للتحليل.',
 'RESEARCH_ONLY':lambda c:'هذا نقاش بحثي قائم على بيانات نافذة محددة.',
 'CERTIFICATION_REQUIRED':lambda c:'اعتماد UTC/DST مطلوب قبل تصنيف الجلسات تاريخيًا.',
 'NO_TRADE_APPROVAL':lambda c:'لا توجد صلاحية شراء أو بيع أو تنفيذ صفقات ضمن هذا المسار.',
}
NEXT={
 'CERTIFY_UTC_DST':'التحقق من قاعدة UTC/DST بدليل مستقل.',
 'COLLECT_SESSIONS':'جمع عينات من جلسات مستقلة وقياس تغطيتها.',
 'VERIFY_HEARTBEAT':'مطابقة سجلات النبض مع نوافذ الأرشفة.',
 'CALCULATE_TIME_BAR_VOL':'حساب تقلب عينات زمنية ثابتة مع بيان عدد العوائد.',
 'REVIEW_SPREAD_REGIME':'مقارنة السبريد بين نوافذ وجلسات مختلفة.',
 'NONE':'لا يوجد إجراء إضافي مقترح.'}


def render_ar(reply: dict, context: dict, prior_replies: list[dict] | None = None) -> str:
    validate_reply(reply,context,prior_replies)
    body=' '.join(LABELS[code](context) for code in reply['finding_codes'])
    return f"[{reply['agent']}] {body} الخطوة التالية: {NEXT[reply['next_check_code']]}"


if __name__=='__main__':
    c=json.loads((ROOT/'example_context.json').read_text(encoding='utf-8'))
    replies=json.loads((ROOT/'example_replies.json').read_text(encoding='utf-8'))
    validate_context(c)
    prior=[]
    for r in replies:
        validate_reply(r,c,prior)
        print(render_ar(r,c,prior))
        prior.append(r)
    print('OFFLINE_GROUNDING_PASS')
