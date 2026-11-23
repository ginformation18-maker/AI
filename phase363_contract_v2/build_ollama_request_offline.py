"""Construct (but NEVER send) one Qwen2.5:7b /api/chat JSON request.

Demo tool only. No Ollama/MT5/Discord/network calls.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from strict_guard import ROOT, ROLES, validate_context, validate_reply

ROLE_FILES={
    'MARKET_MICROSTRUCTURE':'01_MARKET_MICROSTRUCTURE_AR.txt',
    'VOLATILITY_RISK':'02_VOLATILITY_RISK_AR.txt',
    'AUDIT_INTEGRITY':'03_AUDIT_INTEGRITY_AR.txt',
    'COORDINATOR':'04_COORDINATOR_AR.txt',
}


def make_request(context: dict, role: str, prior_replies: list[dict] | None = None) -> dict:
    validate_context(context)
    if context['quality']['flag']=='HALTED':
        raise ValueError('HALTED context: skip Ollama')
    if role not in ROLES: raise ValueError('unknown role')
    prior_replies=prior_replies or []
    i=ROLES.index(role)
    if len(prior_replies)!=i:
        raise ValueError('Expected validated preceding replies in sequence')
    verified=[]
    for r in prior_replies:
        validate_reply(r,context,verified)
        verified.append(r)
    common=(ROOT/'prompts'/'00_COMMON_SYSTEM_AR.txt').read_text(encoding='utf8')
    specialized=(ROOT/'prompts'/ROLE_FILES[role]).read_text(encoding='utf8')
    reply_schema=json.loads((ROOT/'agent_reply_v2.schema.json').read_text(encoding='utf8'))
    user_payload={'validated_quant_context': context,'prior_validated_replies': verified}
    return {
        'model':'qwen2.5:7b',
        'stream':False,
        'format':reply_schema,
        'options':{'temperature':0,'num_predict':256},
        'messages':[
            {'role':'system','content':common+'\n'+specialized},
            {'role':'user','content':json.dumps(user_payload,ensure_ascii=False,separators=(',',':'))},
        ],
    }


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Print request JSON, no API calls')
    parser.add_argument('--role',default=ROLES[0],choices=ROLES)
    args=parser.parse_args()
    c=json.loads((ROOT/'example_context.json').read_text(encoding='utf8'))
    if args.role!=ROLES[0]:
        raise SystemExit('For other roles call make_request(...) with prior validated replies in Python; no unvalidated skips')
    print(json.dumps(make_request(c,args.role),ensure_ascii=False,indent=2))
