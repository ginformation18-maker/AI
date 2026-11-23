"""Phase 3.3 Demo-only automated observation agents + Discord alerts.

READ-ONLY by construction: intentionally never imports ExecutionAgent or calls
order_send; never calls mt5_agents.main. No command intake from Discord.

Agents: MarketData, QuantitativeAnalysis, AIRegimeObserver (optional API),
ArchiveIntegrity, BrokerConnection, DiscordNotifier. Every archival gap is
recorded explicitly *before* any current-window write. Nothing is backfilled.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .settings import Settings, ROOT
from .discord_bridge import DiscordNotifier
from .analysis import TechnicalAnalysisAgent
from .tick_collector import TickArchive, last_closed_boundary, _write_health, _utc_ms
from .parquet_preflight import run_preflight
from .collector_resume_safety import verify_safe_resume
from .observation_timebase import ObservationTimebase

LOG = logging.getLogger(__name__)
ARCHIVE_ROOT = ROOT / 'runtime' / 'tick_archive'


def check_safe_mode(cfg: Settings) -> None:
    if cfg.mode != 'DRY_RUN':
        raise RuntimeError('Observer requires MODE=DRY_RUN. It NEVER trades, including Demo orders.')
    if (ROOT / 'STOP').exists():
        raise RuntimeError('STOP file present. Resolve original safety issue before observer startup.')


def quote_health(tick, now_s: float, max_age: int, *, offset_seconds: int = 0) -> tuple[bool, str]:
    if tick is None:
        return False, 'NO_QUOTE'
    stamp = int(getattr(tick, 'time_msc', 0)) / 1000 or int(getattr(tick, 'time', 0))
    bid = float(getattr(tick, 'bid', 0))
    ask = float(getattr(tick, 'ask', 0))
    if stamp <= 0 or bid <= 0 or ask <= 0 or ask < bid:
        return False, 'INVALID_QUOTE'
    # Both broker timestamps should describe the same price event. If not,
    # refuse to interpret either as an authoritative observation.
    raw_sec = float(getattr(tick, 'time', 0) or 0)
    raw_msc = float(getattr(tick, 'time_msc', 0) or 0)
    if raw_sec > 0 and raw_msc > 0 and abs(raw_sec - raw_msc / 1000) > 2:
        return False, 'INCONSISTENT_QUOTE_TIMESTAMPS'
    # Provisional broker-clock correction applies ONLY to observation, never
    # directly to parquet, order placement, labels or reconciliation.
    age = now_s - (stamp - offset_seconds)
    if age < -5 or age > max_age:
        return False, f'STALE_QUOTE age={int(age)}s'
    return True, 'FRESH'


def record_unverified_gap(archive: TickArchive, symbol: str, start_ms: int, end_ms: int) -> bool:
    """Commit incomplete coverage metadata before collecting any newer window.

    Returns True for a newly recorded gap. This is NOT a declaration that the
    source market was closed. The training gate must independently audit gaps.
    """
    if end_ms <= start_ms:
        raise ValueError('Invalid gap bounds')
    with archive._db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS observer_gaps (
            login INTEGER NOT NULL, server TEXT NOT NULL, symbol TEXT NOT NULL,
            start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state='UNVERIFIED'),
            created_utc TEXT NOT NULL,
            PRIMARY KEY(login,server,symbol,start_ms,end_ms))''')
        cur = db.execute('''INSERT OR IGNORE INTO observer_gaps
            (login,server,symbol,start_ms,end_ms,state,created_utc)
            VALUES (?,?,?,?,?,'UNVERIFIED',?)''',
            (archive.login, archive.server, symbol, start_ms, end_ms,
             datetime.now(timezone.utc).isoformat()))
        return cur.rowcount == 1


@contextmanager
def single_instance(lock: Path):
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError('Observer lock exists: another process or crash; inspect before manual cleanup') from exc
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            fp.write(f'pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}\n')
            fp.flush()
            os.fsync(fp.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


class AIRegimeObserver:
    """Optional LLM descriptive market regime, no order authorization."""
    def __init__(self, enabled: bool, model: str):
        self.enabled = enabled
        self.model = model
        self.client = None
        if enabled:
            if not os.getenv('OPENAI_API_KEY', '').strip():
                raise ValueError('AGENT_AI_ENABLED=true requires OPENAI_API_KEY in local .env')
            from openai import OpenAI
            self.client = OpenAI(timeout=10.0, max_retries=0)

    def describe(self, symbol: str, signal, quote) -> str:
        if not self.enabled:
            return 'AI OFF: set AGENT_AI_ENABLED=true + OPENAI_API_KEY to enable model-based observations.'
        payload = {'symbol': symbol, 'last_technical_signal': signal.side,
                   'atr': round(float(signal.atr), 5), 'rsi': round(float(signal.rsi), 2),
                   'spread': round(float(quote.ask - quote.bid), 5),
                   'signal_reason': signal.reason}
        try:
            response = self.client.responses.create(
                model=self.model, store=False,
                instructions=('You are an OBSERVATION-ONLY market regime analyst. '
                              'Describe limitations and possible market conditions from limited indicators. '
                              'Do not recommend trades, predict returns, claim live news knowledge, '
                              'send orders, alter risk limits or request credentials. '
                              'Output concise JSON.'),
                input=json.dumps(payload, ensure_ascii=False),
                max_output_tokens=330,
                text={'format':{'type':'json_schema','name':'regime_observation','strict':True,
                                'schema':{'type':'object','properties':{
                                    'regime':{'type':'string'}, 'risk_notes':{'type':'string'}},
                                    'required':['regime','risk_notes'], 'additionalProperties':False}}})
            info = json.loads(response.output_text)
            return f"AI OBSERVATION: {str(info['regime'])[:170]} | Risk: {str(info['risk_notes'])[:240]}"
        except Exception as exc:
            LOG.error('AI OBSERVER UNAVAILABLE: %s', type(exc).__name__)
            return 'AI UNAVAILABLE: model call failed; no AI conclusion has been produced.'


class Observer:
    def __init__(self, cfg, mt5, notifier: DiscordNotifier, archive: TickArchive, *,
                 ai=None, interval=30, report_interval=1800, ai_interval=3600, now=None,
                 timebase: ObservationTimebase | None = None):
        from .market import MarketDataAgent
        self.cfg, self.mt5, self.notifier, self.archive = cfg, mt5, notifier, archive
        self.timebase = timebase or ObservationTimebase()
        if self.timebase.provisional and cfg.mode != 'DRY_RUN':
            raise RuntimeError('Provisional timebase forbids any trading mode')
        self.market = MarketDataAgent(cfg.timeframe)
        self.quant = TechnicalAnalysisAgent()
        self.ai = ai or AIRegimeObserver(False, cfg.ai_model)
        self.interval = interval
        self.report_interval = report_interval
        self.ai_interval = ai_interval
        self.now = now or time.time
        self.last_report = {}
        self.last_bar = {}
        self.last_ai = {}
        self.last_issue = {}
        # Exposes only de-identified, market-only snapshots to the Discord hub.
        self.snapshots = {}

    def notify(self, kind: str, symbol: str, body: str, *, cooldown=0) -> None:
        key = (kind, symbol)
        now = self.now()
        if cooldown and now - self.last_issue.get(key, -1e20) < cooldown:
            return
        self.last_issue[key] = now
        message = f'[{kind}] {symbol} | {body} | DRY_RUN / DEMO / NO ORDERS'
        LOG.info(message)
        try:
            self.notifier.send(message)
        except RuntimeError as exc:
            LOG.error('DISCORD DELIVERY FAILED (%s)', exc)

    def cycle(self) -> None:
        if (ROOT / 'STOP').exists():
            raise RuntimeError('STOP file appeared: observer halted')
        now = datetime.fromtimestamp(self.now(), timezone.utc)
        for symbol in self.cfg.symbols:
            self._symbol_cycle(symbol, now)

    def _symbol_cycle(self, symbol: str, now: datetime) -> None:
        if not self.mt5.symbol_select(symbol, True):
            self.notify('ALERT', symbol, 'Symbol unavailable; check name on the broker', cooldown=1800)
            return
        tick = self.mt5.symbol_info_tick(symbol)
        fresh, reason = quote_health(tick, now.timestamp(), self.cfg.max_quote_age_seconds,
                                     offset_seconds=self.timebase.offset_seconds)
        if not fresh:
            self.notify('MARKET_IDLE', symbol, f'{reason}; no live tick archival or analysis', cooldown=3600)
            return
        if self.timebase.provisional:
            # The candidate +2h mapping has NOT been validated for historical
            # archive provenance. Do not write ANY tick/manifest/health file.
            self.notify('TIMEBASE_PROVISIONAL', symbol,
                        f'offset={self.timebase.offset_seconds:+d}s; observing quotes and candles '
                        'ONLY; historical/Parquet archival suspended pending provenance audit',
                        cooldown=3600)
        else:
            # Only finished, finality-delayed 60-second windows. No long historical fetch.
            end = last_closed_boundary(now, 60)
            start = end - timedelta(seconds=60)
            previous = self.archive.latest_end(symbol)
            window_start_ms, window_end_ms = _utc_ms(start), _utc_ms(end)
            if previous is not None and previous < window_start_ms:
                created = record_unverified_gap(self.archive, symbol, previous, window_start_ms)
                if created:
                    self.notify('UNVERIFIED_GAP', symbol,
                                f'{datetime.fromtimestamp(previous/1000,timezone.utc).isoformat()} '
                                f'→ {start.isoformat()}; NOT BACKFILLED or marked complete')
            if previous is not None and previous > window_start_ms and previous != window_end_ms:
                raise RuntimeError(f'Manifest timeline overlaps current window: {symbol}')
            if previous is None or previous <= window_start_ms:
                # For previously indexed windows, TickArchive will verify checksum/readback.
                result = self.archive.collect(symbol, start, end)
                _write_health(self.archive.root, result, window_end_ms)
                if result['state'] != 'ARCHIVED':
                    self.notify('NO_TICKS_UNVERIFIED', symbol,
                                f'{start.isoformat()} got 0 ticks; not counted as valid coverage', cooldown=1200)
                else:
                    LOG.info('ARCHIVED symbol=%s count=%s start=%s', symbol, result['count'], start.isoformat())
        # Analysis on CLOSED candles only, once per completed candle.
        try:
            bars = self.market.candles(symbol)
        except (RuntimeError, ValueError) as exc:
            self.notify('CANDLES_UNAVAILABLE', symbol, type(exc).__name__ + ': ' + str(exc)[:160], cooldown=1800)
            return
        raw_closed = int(bars.iloc[-1]['time'])
        closed = int(self.timebase.to_observed_utc_s(raw_closed))
        # Provisional mapping must yield candle boundaries aligned to the
        # configured timeframe; otherwise refuse technical observations.
        if closed % self.market.seconds != 0:
            self.notify('INVALID_BAR_TIMEBASE', symbol, 'Unaligned adjusted candle; no analysis', cooldown=1800)
            return
        if closed not in self.last_bar.get(symbol, set()):
            self.last_bar[symbol] = {closed}
            try:
                signal = self.quant.decide(symbol, bars)
            except (ValueError, KeyError) as exc:
                self.notify('ANALYSIS_BLOCKED', symbol, str(exc)[:160], cooldown=1800)
                return
            age = self.now() - (closed + self.market.seconds)
            if age < -5 or age > self.market.seconds + self.cfg.max_bar_age_seconds:
                self.notify('STALE_CANDLES', symbol, 'closed candle too old for meaningful analysis', cooldown=1800)
                return
            self.snapshots[symbol] = {
                'symbol': symbol, 'observed_utc': now.isoformat(),
                'bar_open_utc': datetime.fromtimestamp(closed,timezone.utc).isoformat(),
                'bid': round(float(tick.bid), 6), 'ask': round(float(tick.ask), 6),
                'spread': round(float(tick.ask-tick.bid), 6),
                'rsi': round(float(signal.rsi), 3), 'atr': round(float(signal.atr), 6),
                'technical_side': signal.side, 'technical_reason': signal.reason,
                'timebase_status': ('PROVISIONAL_BROKER_OFFSET_NO_ARCHIVE'
                                    if self.timebase.provisional else 'STRICT_UTC'),
            }
            if signal.side != 'HOLD':
                self.notify('TECHNICAL_SIGNAL', symbol,
                            f'{signal.side} EMA/RSI signal. OBSERVATION ONLY; not an order or recommendation. '
                            f'RSI={signal.rsi:.1f} ATR={signal.atr:.4f}')
            if self.ai.enabled and self.now() - self.last_ai.get(symbol, -1e20) >= self.ai_interval:
                self.last_ai[symbol] = self.now()
                self.notify('AI_REGIME', symbol, self.ai.describe(symbol, signal, tick))
        if self.now() - self.last_report.get(symbol, -1e20) >= self.report_interval:
            self.last_report[symbol] = self.now()
            archive_note = ('tick archiving PAUSED (provisional timebase); provenance audit required'
                            if self.timebase.provisional
                            else f'last window {start.isoformat()}; historical gaps require audit')
            self.notify('HEARTBEAT', symbol,
                        f'feed fresh; monitor active; {archive_note}')


def main():
    parser = argparse.ArgumentParser(description='Demo-only observation agents + Discord, no trading orders')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--test-discord', action='store_true', help='Send only Discord test; no MT5 connect')
    mode.add_argument('--once', action='store_true', help='One observation pass only')
    mode.add_argument('--forever', action='store_true', help='Keep monitoring automatically')
    args = parser.parse_args()
    from .settings import ROOT
    from .market import connect_mt5
    import MetaTrader5 as mt5
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    webhook = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook:
        raise SystemExit('Missing DISCORD_WEBHOOK_URL in .env (do not share this secret)')
    sender = DiscordNotifier(webhook)
    if args.test_discord:
        sender.send('✅ Discord webhook connected to MT5 observation agents. NO TRADING ORDERS.')
        print('DISCORD TEST SENT (check target channel).')
        return
    cfg = Settings.load()
    check_safe_mode(cfg)
    root = ROOT / 'runtime' / 'tick_archive'
    # A preflight failure must prevent observation startup, not be swallowed.
    preflight = run_preflight(root)
    print(json.dumps(preflight, ensure_ascii=False), flush=True)
    verify_safe_resume(root)
    connected = False
    with single_instance(ROOT / 'runtime' / 'discord_observer.lock'):
        try:
            account = connect_mt5(cfg)
            connected = True
            if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
                raise RuntimeError('Account not DEMO: monitoring disabled')
            if str(account.server) != cfg.server:
                raise RuntimeError('Broker server mismatch')
            archive = TickArchive(root, mt5, cfg.login, str(account.server))
            ai = AIRegimeObserver(os.getenv('AGENT_AI_ENABLED','false').lower()=='true',
                                  os.getenv('AGENT_AI_MODEL', cfg.ai_model))
            observer = Observer(cfg, mt5, sender, archive, ai=ai,
                                interval=max(30, int(os.getenv('AGENT_POLL_SECONDS','60'))),
                                report_interval=max(300,int(os.getenv('AGENT_REPORT_SECONDS','1800'))),
                                ai_interval=max(1800,int(os.getenv('AGENT_AI_INTERVAL_SECONDS','3600'))))
            sender.send(f'✅ MT5 observer ONLINE | DEMO verified | symbols={",".join(cfg.symbols)} | '
                        f'AI={"ON" if ai.enabled else "OFF"} | DRY_RUN | NO ORDERS')
            while True:
                observer.cycle()
                if args.once:
                    break
                time.sleep(observer.interval)
        except Exception as exc:
            LOG.exception('OBSERVER HALTED: %s', type(exc).__name__)
            try:
                sender.send(f'🚨 MT5 observer HALTED | {type(exc).__name__}: {str(exc)[:260]} | no auto risk override')
            except Exception:
                pass
            raise
        finally:
            if connected:
                mt5.shutdown()


if __name__ == '__main__':
    main()
