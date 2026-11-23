"""Separate named Discord webhook identities for READ-ONLY market discussions.

A single channel Webhook can override its visible sender name for each message.
This does NOT create four Discord bot accounts or grant trading permissions.
Only named AI discussion messages use the webhook; the command/observer bot
keeps its existing token and permissions.
"""
from __future__ import annotations

import asyncio
import re

ROLE_NAMES_AR = {
    'MARKET': '📊 وكيل مراقبة السوق',
    'QUANT': '📈 وكيل التحليل الكمي',
    'RISK': '🛡️ وكيل إدارة المخاطر',
    'COORDINATOR': '🧠 وكيل تنسيق النقاش',
}

# Only Discord's HTTPS API; no arbitrary URLs, redirects, query strings, etc.
_WEBHOOK_RE = re.compile(
    r'https://discord\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+/?\Z'
)


def validate_agent_webhook(url: str) -> str:
    value = str(url or '').strip()
    if not _WEBHOOK_RE.fullmatch(value):
        raise ValueError('DISCORD_AGENT_WEBHOOK_URL must be an official Discord HTTPS webhook URL')
    return value.rstrip('/')


class NamedAgentPoster:
    """Async-only sender; NEVER called on the market/tick/Parquet thread."""
    def __init__(self, webhook_url: str, discord, *, expected_channel: int | None = None,
                 expected_guild: int | None = None):
        self.url = validate_agent_webhook(webhook_url)
        self.discord = discord
        self.expected_channel = expected_channel
        self.expected_guild = expected_guild
        self._verified = False

    async def send(self, role: str, message: str) -> None:
        if role not in ROLE_NAMES_AR:
            raise ValueError('Unknown AI role')
        # Fresh short-lived session limits resource retention after Discord outages.
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            hook = self.discord.Webhook.from_url(self.url, session=session)
            if not self._verified and self.expected_channel is not None:
                info = await asyncio.wait_for(hook.fetch(), timeout=10.0)
                if (getattr(info, 'channel_id', None) != self.expected_channel or
                    (self.expected_guild is not None and
                     getattr(info, 'guild_id', None) not in (None, self.expected_guild))):
                    raise RuntimeError('Agent webhook targets the wrong Discord channel/server')
                self._verified = True
            # No role can @everyone or mention individual users via its generated text.
            await asyncio.wait_for(
                hook.send(str(message)[:1800],
                          username=ROLE_NAMES_AR[role],
                          allowed_mentions=self.discord.AllowedMentions.none(),
                          wait=True),
                timeout=15.0,
            )


def main() -> int:
    """Manual network check: post one clearly-labeled test per named role."""
    import argparse
    parser=argparse.ArgumentParser(description='Send four non-trading named agent test messages')
    parser.add_argument('--test', action='store_true', required=True)
    parser.parse_args()
    from .settings import Settings
    from .discord_observer import check_safe_mode
    from .discord_hub_config import validate_hub_config
    cfg=Settings.load()
    check_safe_mode(cfg)
    conf=validate_hub_config()
    if not conf['named_agents']:
        raise SystemExit('Set AGENT_DISTINCT_NAMES_ENABLED=true and DISCORD_AGENT_WEBHOOK_URL in .env')
    import discord

    async def run():
        sender=NamedAgentPoster(conf['agent_webhook'], discord,
                                expected_channel=conf['channel'], expected_guild=conf['guild'])
        for role in ROLE_NAMES_AR:
            await sender.send(role, '✅ تجربة الاسم المستقل في Discord — وكيل للمناقشة فقط؛ لا توجد أسعار حية أو أوامر تداول.')
    try:
        asyncio.run(run())
    except Exception as exc:
        # Avoid printing webhook URL/token through a third-party exception body.
        print(f'FAIL: Discord webhook test failed ({type(exc).__name__}); no broker orders')
        return 2
    print('PASS: Four separate Arabic webhook sender names tested in configured Discord channel')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
