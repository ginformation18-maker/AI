"""Single-channel Discord AI discussion hub and MT5 observer — READ ONLY.

One Discord bot, one allowlisted server + channel, slash-only (no message-content intent).
Broker API use is restricted to read-only Observer; no order_send/execution imports.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from queue import Empty, Full, Queue
import threading
import time
from pathlib import Path

from .settings import Settings, ROOT
from .agent_discussion import DiscussionUnavailable, request_roles, request_roles_ollama, validated_snapshot
from .discord_observer import (Observer, AIRegimeObserver, check_safe_mode,
                               single_instance)
from .tick_collector import TickArchive
from .parquet_preflight import run_preflight
from .collector_resume_safety import verify_safe_resume
from .observation_timebase import ObservationTimebase

LOG = logging.getLogger(__name__)


from .discord_hub_config import validate_hub_config, Budget
from .discord_agent_webhook import ROLE_NAMES_AR, NamedAgentPoster


class Runtime:
    def __init__(self):
        self.observer=None
        self.state='STARTING'
        self.reason=''
        self.stop=threading.Event()
        self.worker=None

    def snapshot(self, symbol:str):
        obs=self.observer
        return (obs.snapshots.get(symbol.upper()) or {}).copy() if obs else {}


class DiscordChannelNotifier:
    """Bounded, non-blocking producer used by the dedicated MT5 observer thread.

    Network calls are performed ONLY by the Discord asyncio consumer, never by
    the MT5 observation/Parquet thread. A full queue drops notifications with
    an explicit log event; it NEVER blocks market collection or trading safety.
    """
    def __init__(self, client, event_loop):
        self.client=client
        self.event_loop=event_loop

    def send(self, message):
        if self.event_loop.is_closed():
            raise RuntimeError('Discord event loop unavailable; notification dropped')
        try:
            self.client.notification_queue.put_nowait(str(message)[:1800])
        except Full:
            raise RuntimeError('Discord notification queue full; alert dropped (check logs)') from None
        try:
            self.event_loop.call_soon_threadsafe(self.client.notification_signal.set)
        except RuntimeError:
            # Event loop became unavailable after the queue put. Disallow an
            # unbounded retry loop and surface the issue in local logs.
            raise RuntimeError('Discord event loop unavailable; notification not delivered') from None


class Hub:
    def __init__(self, config, cfg, *, discord):
        self.config,self.cfg,self.discord = config,cfg,discord
        self.runtime=Runtime()
        self.budget=Budget(config['max_daily'], ROOT/'runtime'/'discord_ai_budget.json')
        self.last_auto={}
        self.last_manual={}
        self.named_poster = NamedAgentPoster(config["agent_webhook"], discord, expected_channel=config["channel"], expected_guild=config["guild"]) if config["named_agents"] else None
        self.loop=None
        self.synced=False
        self.tasks_started=False
        self.channel=None
        self.notification_queue=Queue(maxsize=128)
        # Prevent overlapping local LLM jobs from exhausting Windows RAM/CPU.
        self.discussion_lock=asyncio.Lock()
        self.notification_signal=None
        self.dropped_notifications=0
        self.client = discord.Client(intents=discord.Intents.default())
        self.tree = discord.app_commands.CommandTree(self.client)
        self._register()

        @self.client.event
        async def on_ready():
            if not self.synced:
                guild=discord.Object(id=self.config['guild'])
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                self.synced=True
            if not self.tasks_started:
                self.loop=asyncio.get_running_loop()
                channel=self.client.get_channel(self.config['channel'])
                if channel is None:
                    channel=await self.client.fetch_channel(self.config['channel'])
                if getattr(channel,'guild',None) is None or channel.guild.id!=self.config['guild']:
                    raise RuntimeError('Configured Discord channel is not in configured guild')
                self.channel=channel
                self.notification_signal=asyncio.Event()
                self.tasks_started=True
                self.loop.create_task(self._notification_worker())
                self.runtime.worker=threading.Thread(target=self._worker, name='mt5-observer',daemon=True)
                self.runtime.worker.start()
                self.loop.create_task(self._auto_loop())
                await self.post('✅ Discord hub connected | MT5 observer STARTING; use /agents for status | read-only | no orders')

    def _register(self):
        @self.tree.command(name='agents', description='عرض حالة وكلاء MT5 ووضع التشغيل')
        async def agents(interaction):
            if not await self._check_channel(interaction):
                return
            state=self.runtime.state
            reason=self.runtime.reason[:220]
            tb = (self.runtime.observer.timebase.status()
                  if self.runtime.observer else 'NOT_VERIFIED')
            syms=', '.join(self.cfg.symbols)
            await interaction.response.send_message(
                f'🤖 Agents: {state} | symbols: {syms} | '+
                f'AI: {"ON" if self.config["ai_enabled"] else "OFF"} ({self.config["provider"]}) | '+
                f'Auto-discuss: {"ON" if self.config["auto"] else "OFF"} | '+
                f'أسماء مستقلة: {"نعم" if self.named_poster else "لا"} | حد يومي: {self.config["max_daily"]} | '+
                f'MODE=DRY_RUN | DEMO only | NO ORDERS | Timebase: {tb}'+(f'\nIssue: {reason}' if reason else ''),
                allowed_mentions=self.discord.AllowedMentions.none())

        @self.tree.command(name='discuss', description='طلب نقاش 4 وكلاء حول بيانات السوق (دون أوامر)')
        async def discuss(interaction, symbol:str, question:str='ما المخاطر والقيود في هذه البيانات؟'):
            if not await self._check_channel(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            symbol=symbol.upper().strip()
            if symbol not in self.cfg.symbols:
                await interaction.followup.send('الرمز غير مفعّل في SYMBOLS.',ephemeral=True)
                return
            if not self.config['ai_enabled']:
                await interaction.followup.send('الذكاء غير مفعّل. اضبط AGENT_AI_ENABLED=true ومزود Ollama المحلي.',ephemeral=True)
                return
            now=time.monotonic()
            if now-self.last_manual.get(interaction.user.id,-1e20)<self.config['manual_cooldown']:
                await interaction.followup.send(f'مهلة النقاش اليدوي {self.config["manual_cooldown"]} ثانية.',ephemeral=True)
                return
            self.last_manual[interaction.user.id]=now
            try:
                await self._debate(symbol,question[:300],trigger=f'by {interaction.user.display_name}')
                await interaction.followup.send('تم نشر نقاش الوكلاء في هذه القناة.',ephemeral=True)
            except DiscussionUnavailable as e:
                await interaction.followup.send(f'لم يبدأ النقاش: {e}',ephemeral=True)
            except Exception as e:
                LOG.exception('Discuss failed')
                await interaction.followup.send(f'نقاش غير متاح: {type(e).__name__}',ephemeral=True)


        @self.tree.command(name='roundtable', description='نقاش بحثي بالعربية دون الاعتماد على أسعار مباشرة')
        async def roundtable(interaction, question: str):
            if not await self._check_channel(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not self.config['ai_enabled'] or self.config['provider']!='ollama':
                await interaction.followup.send('النقاش البحثي يحتاج تفعيل Ollama المحلي.',ephemeral=True)
                return
            now=time.monotonic()
            if now-self.last_manual.get(interaction.user.id,-1e20)<self.config['manual_cooldown']:
                await interaction.followup.send('لم تنتهِ مهلة نقاشك اليدوي.',ephemeral=True)
                return
            self.last_manual[interaction.user.id]=now
            try:
                await self._offline_debate(question[:300], trigger='يدوي دون بيانات لحظية')
                await interaction.followup.send('تم نشر النقاش التعليمي في قناة الوكلاء.',ephemeral=True)
            except DiscussionUnavailable as exc:
                await interaction.followup.send(f'لم يبدأ النقاش: {exc}',ephemeral=True)
            except Exception as exc:
                LOG.exception('Offline discussion failed')
                await interaction.followup.send(f'فشل النقاش: {type(exc).__name__}',ephemeral=True)

    async def _check_channel(self, interaction):
        if (interaction.guild_id != self.config['guild'] or
                interaction.channel_id != self.config['channel']):
            await interaction.response.send_message('الأوامر مخصصة لقناة الوكلاء المحددة فقط.',ephemeral=True)
            return False
        return True

    async def post(self, content):
        if self.channel is None:
            raise RuntimeError('Discord channel not ready')
        # Avoid bot-triggered mass mentions and hide broker account IDs.
        await self.channel.send(str(content)[:1800],
                                allowed_mentions=self.discord.AllowedMentions.none())

    async def _notification_worker(self):
        """Discord sends are asynchronous and bounded; never delay MT5 polling.

        Messages are best-effort; any lost message is logged and NOT silently
        converted to a successful delivery. Check observer state and local logs.
        """
        while not self.runtime.stop.is_set():
            await self.notification_signal.wait()
            self.notification_signal.clear()
            while True:
                try:
                    message=self.notification_queue.get_nowait()
                except Empty:
                    break
                try:
                    await asyncio.wait_for(self.post(message), timeout=10.0)
                except Exception as exc:
                    LOG.error('DISCORD NOTIFICATION DELIVERY FAILED (%s); message dropped',
                              type(exc).__name__)
                finally:
                    self.notification_queue.task_done()

    def _role_publisher_from_worker(self):
        """Send each finished Ollama role without waiting for the remaining three.

        Called from the AI worker thread; Discord API calls always execute on
        the asyncio event loop. The critical MT5 observer thread is untouched.
        """
        loop = asyncio.get_running_loop()
        def publish(role, message):
            task = asyncio.run_coroutine_threadsafe(self._publish_roles([(role, message)]), loop)
            try:
                task.result(timeout=35.0)
            except Exception as exc:
                task.cancel()
                raise DiscussionUnavailable(f'Agent publish failure: {type(exc).__name__}') from exc
        return publish

    async def _debate(self, symbol, question, trigger):
        if self.discussion_lock.locked():
            raise DiscussionUnavailable('Another AI discussion is running; skipped to protect resources')
        async with self.discussion_lock:
            if self.runtime.state != 'ONLINE':
                raise DiscussionUnavailable('Observer is not ONLINE')
            snap=validated_snapshot(self.runtime.snapshot(symbol),time.time(), max_age=600)
            if not self.budget.acquire():
                raise DiscussionUnavailable('Daily AI discussion quota reached')
            qualifier = (' | ⚠️ توقيت وسيط مبدئي غير معتمد للأرشفة أو التداول'
                         if snap.get('timebase_status') == 'PROVISIONAL_BROKER_OFFSET_NO_ARCHIVE' else '')
            await self.post(f'🗣️ **نقاش وكلاء AI حول {symbol}** | {trigger} | بيانات UTC {snap["observed_utc"]} | مراقبة فقط{qualifier}')
            try:
                if self.config['provider'] == 'ollama':
                    roles=await asyncio.to_thread(
                        request_roles_ollama, snapshot=snap,
                        model=self.config['ollama_model'], url=self.config['ollama_url'],
                        timeout=self.config['ollama_timeout'],
                        max_predict=self.config['ollama_predict'],question=question,
                        on_role=self._role_publisher_from_worker())
                else:
                    roles=await asyncio.to_thread(
                        request_roles, snapshot=snap,model=self.config['model'],
                        api_key=self.config['ai_key'],question=question)
            except Exception as e:
                await self.post(f'⚠️ AI unavailable, debate incomplete: {type(e).__name__}; no trade decisions')
                raise
            if self.config['provider'] != 'ollama':
                await self._publish_roles(roles)
            await self.post('🔒 الخلاصة: المخرجات نقاش افتراضي وليست إشارات تنفيذ أو ضمانًا للأرباح.')

    async def _publish_roles(self, roles):
        for role, response in roles:
            try:
                if self.named_poster:
                    await self.named_poster.send(role, response)
                else:
                    await self.post(f'**{ROLE_NAMES_AR[role]}**\n{response}')
            except Exception as exc:
                # Never misreport a partial debate as a fully delivered one.
                LOG.error('AGENT MESSAGE DELIVERY FAILED role=%s error=%s', role, type(exc).__name__)
                raise DiscussionUnavailable(f'فشل إرسال مداخلة {ROLE_NAMES_AR.get(role,role)}') from exc

    async def _offline_debate(self, question: str, trigger: str):
        """Educational discussion even when market is idle or observer halted.

        Absolutely no live quotes are represented. The local-only model runs
        off-thread; this path has no broker API access or order permissions.
        """
        if self.discussion_lock.locked():
            raise DiscussionUnavailable('Another AI discussion is running')
        async with self.discussion_lock:
            if not self.budget.acquire():
                raise DiscussionUnavailable('Daily discussion quota reached')
            await self.post(f'🧠 نقاش بحثي بالعربية | {trigger} | لا توجد بيانات سوق مباشرة | مراقبة فقط')
            try:
                roles = await asyncio.to_thread(
                    request_roles_ollama, snapshot=None, offline=True,
                    model=self.config['ollama_model'], url=self.config['ollama_url'],
                    timeout=self.config['ollama_timeout'],
                    max_predict=self.config['ollama_predict'], question=question,
                    on_role=self._role_publisher_from_worker())
            except Exception as exc:
                await self.post(f'⚠️ تعذر تشغيل النقاش المحلي: {type(exc).__name__}')
                raise
            # Ollama roles already delivered progressively by AI worker callback.
            await self.post('🔒 نقاش تعليمي فقط؛ لا يعتمد على أسعار حديثة ولا يوجه صفقات.')

    async def _auto_loop(self):
        while not self.runtime.stop.is_set():
            await asyncio.sleep(60)
            if not (self.config['auto'] and self.config['ai_enabled'] and
                    self.runtime.state=='ONLINE'):
                continue
            for symbol in self.cfg.symbols:
                snap=self.runtime.snapshot(symbol)
                if not snap:
                    continue
                key=(symbol,snap.get('bar_open_utc'))
                now=time.monotonic()
                if self.last_auto.get(symbol) and now-self.last_auto[symbol]<self.config['auto_interval']:
                    continue
                if getattr(self,'_last_auto_bar',{}).get(symbol)==key:
                    continue
                # Gate on a fresh snapshot BEFORE consuming API quota.
                try:
                    validated_snapshot(snap,time.time(),max_age=600)
                    self.last_auto[symbol]=now
                    if not hasattr(self,'_last_auto_bar'):
                        self._last_auto_bar={}
                    self._last_auto_bar[symbol]=key
                    await self._debate(symbol,'مراجعة دورية للأرصاد والمخاطر والبيانات الناقصة',trigger='automated')
                except DiscussionUnavailable as e:
                    LOG.info('Auto debate skipped %s: %s',symbol,e)
                except Exception:
                    LOG.exception('Auto debate failed')

    def _worker(self):
        from .market import connect_mt5
        import MetaTrader5 as mt5
        connected=False
        try:
            check_safe_mode(self.cfg)
            with single_instance(ROOT/'runtime'/'discord_hub.lock'):
                preflight=run_preflight(ROOT/'runtime'/'tick_archive')
                LOG.info('Parquet preflight: %s',preflight['status'])
                verify_safe_resume(ROOT/'runtime'/'tick_archive')
                account=connect_mt5(self.cfg)
                connected=True
                if account.trade_mode!=mt5.ACCOUNT_TRADE_MODE_DEMO or account.server!=self.cfg.server:
                    raise RuntimeError('Not the expected DEMO broker account')
                timebase=ObservationTimebase.from_env(self.cfg)
                archive=TickArchive(ROOT/'runtime'/'tick_archive',mt5,self.cfg.login,account.server)
                if timebase.provisional:
                    LOG.warning('TIMEBASE PROVISIONAL: offset %+d seconds, NO tick archive writes, NO orders',
                                timebase.offset_seconds)
                notifier=DiscordChannelNotifier(self,self.loop)
                observer=Observer(self.cfg,mt5,notifier,archive,
                                  ai=AIRegimeObserver(False,self.config['model']),
                                  timebase=timebase,
                                  interval=max(30,int(os.getenv('AGENT_POLL_SECONDS','60'))),
                                  report_interval=max(300,int(os.getenv('AGENT_REPORT_SECONDS','1800'))))
                self.runtime.observer=observer
                self.runtime.state='ONLINE'
                while not self.runtime.stop.is_set():
                    observer.cycle()
                    self.runtime.stop.wait(observer.interval)
        except Exception as e:
            LOG.exception('Observer fatal: %s',type(e).__name__)
            self.runtime.state='HALTED'
            self.runtime.reason=f'{type(e).__name__}: {str(e)[:160]}'
            if self.loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.post(f'🚨 MT5 observer HALTED: {self.runtime.reason} | NO auto risk bypass'),
                        self.loop).result(timeout=15)
                except Exception:
                    pass
        finally:
            self.runtime.stop.set()
            if connected:
                mt5.shutdown()
            # Bot remains online for diagnostics /agents, but never resumes unsafe monitoring.

    def run(self):
        self.client.run(self.config['token'],log_handler=None)


def main():
    parser=argparse.ArgumentParser(description='Read-only MT5 Demo agents in one Discord channel')
    parser.add_argument('--check-config',action='store_true',help='Offline config validation; no Discord connect')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    cfg=Settings.load()
    check_safe_mode(cfg)
    config=validate_hub_config()
    timebase=ObservationTimebase.from_env(cfg)
    if args.check_config:
        print(json.dumps({'status':'PASS','mode':cfg.mode,'broker_demo_must_be_verified_at_runtime':True,
                          'symbols':cfg.symbols,'ai_enabled':config['ai_enabled'],
                          'auto_discussion':config['auto'],'no_orders':True,
                          'distinct_names':config['named_agents'],
                          'max_daily_discussions':config['max_daily'],
                          'timebase_status':timebase.status(),
                          'tick_archiving_allowed':timebase.archive_allowed}))
        return
    try:
        import discord
    except ImportError as e:
        raise SystemExit('Install discord.py with: .venv\\Scripts\\python.exe -m pip install -r requirements.txt') from e
    Hub(config,cfg,discord=discord).run()


if __name__=='__main__':
    main()
