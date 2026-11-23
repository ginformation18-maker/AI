"""Read-only Discord notification transport. Webhook is a local secret.

This module NEVER fetches commands from Discord and has no broker order APIs.
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

LOG = logging.getLogger(__name__)


def validate_webhook(url: str) -> str:
    url = url.strip()
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname not in ('discord.com', 'discordapp.com')
            or p.username is not None or p.password is not None or p.port is not None
            or not p.path.startswith('/api/webhooks/') or len(p.path.split('/')) != 5
            or not p.path.split('/')[3].isdigit() or not p.path.split('/')[4]
            or p.query or p.fragment):
        raise ValueError('Invalid Discord webhook URL. Create an incoming webhook in your channel.')
    return url


class DiscordNotifier:
    def __init__(self, webhook_url: str, opener=None):
        self._url = validate_webhook(webhook_url)
        self._open = opener or urlopen

    def send(self, message: str) -> None:
        # Do not include credentials, login IDs or webhook URI in notifications.
        content = str(message).replace('@everyone', '@ everyone').replace('@here', '@ here')[:1800]
        body = json.dumps({'content': content, 'allowed_mentions': {'parse': []}}, ensure_ascii=False).encode('utf-8')
        req = Request(self._url, data=body, headers={'Content-Type':'application/json',
                                                     'User-Agent':'MT5-Observer/3.3'}, method='POST')
        for attempt in range(2):
            try:
                with self._open(req, timeout=12) as resp:
                    if int(resp.status) not in (200, 204):
                        raise RuntimeError(f'Discord HTTP {resp.status}')
                return
            except HTTPError as exc:
                if exc.code == 429 and attempt == 0:
                    retry_after = 2.0
                    try:
                        body = json.loads(exc.read().decode('utf-8'))
                        retry_after = max(0.0, min(float(body.get('retry_after', 2)), 10.0))
                    except (ValueError, OSError, KeyError):
                        pass
                    time.sleep(retry_after)
                    continue
                raise RuntimeError(f'Discord HTTP error {exc.code} (URL suppressed)') from None
            except (URLError, TimeoutError, OSError) as exc:
                raise RuntimeError(f'Discord network error: {type(exc).__name__}') from None
        raise RuntimeError('Discord rate limit, message not delivered')
