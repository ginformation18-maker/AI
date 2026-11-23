"""Local Ollama availability and generation test, independent of MT5 and Discord."""
from __future__ import annotations
import json
import os
import sys
from .settings import ROOT  # loads .env from the project root
from .ollama_local import OllamaLocalClient, LocalModelUnavailable, validate_loopback_url


def main():
    if str(os.getenv('AGENT_AI_PROVIDER','openai')).strip().lower() != 'ollama' or str(os.getenv('AGENT_AI_ENABLED','false')).lower() != 'true':
        print(json.dumps({'status':'SKIPPED','reason':'Ollama AI is not enabled'},ensure_ascii=False))
        return 0
    model = os.getenv('OLLAMA_MODEL','qwen3:4b').strip()
    url = validate_loopback_url(os.getenv('OLLAMA_URL','http://127.0.0.1:11434'))
    timeout = float(os.getenv('OLLAMA_TIMEOUT_SECONDS','120'))
    if not 10 <= timeout <= 180:
        raise ValueError('OLLAMA_TIMEOUT_SECONDS must be 10..180')
    client = OllamaLocalClient(url)
    names = client.installed_models()
    if model not in names:
        raise LocalModelUnavailable(f'Local model {model!r} is not installed; run: ollama pull {model}')
    # Actual small inference test, not just /api/tags; no broker data involved.
    response = client.chat(model=model,
                           instructions='Reply in Arabic with one short phrase only. No tools.',
                           data={'test':'هل تعمل محليًا؟'},timeout=timeout,max_predict=64)
    if not response:
        raise LocalModelUnavailable('Ollama returned an empty model generation')
    print(json.dumps({'status':'PASS','provider':'ollama','model':model,
                      'localhost_only':True,'inference_test':True,
                      'response_chars':len(response),'no_orders':True},ensure_ascii=False))
    return 0


if __name__=='__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status':'FAIL','type':type(exc).__name__,
                          'reason':str(exc)[:240],'no_orders':True},ensure_ascii=False))
        sys.exit(2)
