"""Explicit, read-only MT5 broker-clock observation policy.

IMPORTANT: PROVISIONAL_BROKER_OFFSET is NOT a canonical UTC archival fix.
It only permits cautious observation with a broker-clock label shift that must
be re-validated every quote/candle. It DOES NOT enable tick archival or orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ObservationTimebase:
    mode: str = 'STRICT_UTC'
    offset_seconds: int = 0

    @property
    def provisional(self) -> bool:
        return self.mode == 'PROVISIONAL_BROKER_OFFSET'

    @property
    def archive_allowed(self) -> bool:
        return not self.provisional

    @classmethod
    def from_env(cls, cfg, environ=None) -> 'ObservationTimebase':
        e = os.environ if environ is None else environ
        mode = str(e.get('MT5_OBSERVER_TIMEBASE_MODE', 'STRICT_UTC')).strip().upper()
        if mode == 'STRICT_UTC':
            # Explicit offset settings with strict mode are suspicious; do not
            # silently ignore configuration that looks like a partial rollout.
            if (str(e.get('MT5_OBSERVER_OFFSET_SECONDS', '')).strip() or
                    str(e.get('MT5_OBSERVER_TIMEBASE_ACK', '')).strip()):
                raise RuntimeError('Offset configuration present but timebase mode is STRICT_UTC')
            return cls()
        if mode != 'PROVISIONAL_BROKER_OFFSET':
            raise ValueError('Unsupported MT5 observer timebase mode')
        if getattr(cfg, 'mode', None) != 'DRY_RUN':
            raise RuntimeError('Provisional broker-clock observation requires DRY_RUN')
        server = str(e.get('MT5_OBSERVER_TIMEBASE_SERVER', '')).strip()
        if not server or server != getattr(cfg, 'server', None):
            raise RuntimeError('Timebase authorization must match the configured broker server exactly')
        ack = str(e.get('MT5_OBSERVER_TIMEBASE_ACK', '')).strip()
        if ack != 'I_ACCEPT_OBSERVATION_ONLY_NO_ARCHIVE':
            raise RuntimeError('Explicit observation-only/no-archive authorization missing')
        try:
            seconds = int(str(e.get('MT5_OBSERVER_OFFSET_SECONDS', '')))
        except (ValueError, TypeError):
            raise ValueError('Explicit integer MT5_OBSERVER_OFFSET_SECONDS required') from None
        if not (900 <= abs(seconds) <= 14 * 3600) or seconds % 900:
            raise ValueError('Broker timebase offset must be a nonzero 15-minute multiple within 14h')
        return cls(mode=mode, offset_seconds=seconds)

    def to_observed_utc_s(self, raw_broker_time_seconds: float) -> float:
        return raw_broker_time_seconds - self.offset_seconds

    def status(self) -> str:
        if self.provisional:
            return (f'PROVISIONAL_BROKER_OFFSET({self.offset_seconds:+d}s); '
                    'NO ARCHIVE, NO ORDERS, timestamps not yet certified')
        return 'STRICT_UTC'


def require_unshifted_archive_timebase(environ=None) -> None:
    """Prevent CLI tick archival while the broker-clock mapping is provisional."""
    e = os.environ if environ is None else environ
    if str(e.get('MT5_OBSERVER_TIMEBASE_MODE', 'STRICT_UTC')).strip().upper() != 'STRICT_UTC':
        raise RuntimeError('Tick archival is blocked: broker-clock timebase is provisional; '
                           'run read-only observation until historical provenance is audited')
