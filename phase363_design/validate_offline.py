"""Offline demonstration. No MT5, Discord or Ollama connection."""
import json
from pathlib import Path
from strict_grounding import validate_context, validate_reply
ROOT=Path(__file__).resolve().parent
ctx=validate_context(json.loads((ROOT/'example_context.json').read_text(encoding='utf-8')))
replies=json.loads((ROOT/'example_agent_replies.json').read_text(encoding='utf-8'))
prior=[]
for r in replies:
    validate_reply(r,ctx,prior)
    prior.append(r)
print('PASS: context and 4 sequential role replies validated (OFFLINE only).')
print('NO BROKER ACCESS / NO ORDERS / UTC-DST UNVERIFIED')
