"""Bounded, local-only Ollama client for READ-ONLY Discord discussions.

No cloud provider, no trading authority. All transport is loopback HTTP and
explicitly ignores system HTTP proxies; redirects are rejected.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError


class LocalModelUnavailable(RuntimeError):
    pass


def validate_loopback_url(url: str) -> str:
    """Reject all non-loopback, redirects, user-info, fragments, and paths."""
    try:
        parsed = urlsplit(str(url).strip())
        if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1')
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or parsed.port is None or not (1 <= parsed.port <= 65535)):
            raise ValueError('OLLAMA_URL must be a local-only http://127.0.0.1:11434 URL')
    except (ValueError, TypeError) as exc:
        raise ValueError('OLLAMA_URL must be a local-only http://127.0.0.1:11434 URL') from exc
    return str(url).strip().rstrip('/')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaLocalClient:
    def __init__(self, url: str = 'http://127.0.0.1:11434'):
        self.base = validate_loopback_url(url)
        # Never send local market observations through environment HTTP proxies.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _json_request(self, path: str, data: dict | None = None, timeout: float = 10.0) -> dict:
        body = None if data is None else json.dumps(data, ensure_ascii=False).encode('utf-8')
        headers = {'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = Request(self.base + path, data=body, headers=headers,
                      method='POST' if body is not None else 'GET')
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    raise LocalModelUnavailable('Ollama response exceeds safety size cap')
        except (URLError, HTTPError, TimeoutError, OSError) as exc:
            # Never print raw responses, environment variables or model prompts.
            raise LocalModelUnavailable(f'Ollama local request failed: {type(exc).__name__}') from exc
        try:
            result = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalModelUnavailable('Ollama response is not valid JSON') from exc
        if not isinstance(result, dict):
            raise LocalModelUnavailable('Ollama returned an unexpected payload')
        return result

    def installed_models(self) -> list[str]:
        result = self._json_request('/api/tags', timeout=5.0)
        models = result.get('models')
        if not isinstance(models, list):
            raise LocalModelUnavailable('Ollama model list is invalid')
        return [m.get('name') for m in models if isinstance(m, dict) and isinstance(m.get('name'),str)]

    def chat(self, *, model: str, instructions: str, data: dict,
             timeout: float = 120.0, max_predict: int = 220) -> str:
        if not isinstance(data, dict):
            raise ValueError('AI data must be a dict')
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': instructions},
                {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}
            ],
            'stream': False,
            'think': False,
            'keep_alive': '5m',
            'options': {'num_predict': max_predict, 'num_ctx': 3072,
                        'temperature': 0.2}
        }
        result = self._json_request('/api/chat', payload, timeout=timeout)
        if result.get('done') is not True:
            raise LocalModelUnavailable('Ollama generation did not complete')
        message = result.get('message', {})
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LocalModelUnavailable('Ollama returned an empty response')
        return content.strip()
