"""Small bounded collector windows, identity, gap handling, and Parquet manifest."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace, ModuleType
import sys

import numpy as np
import pytest

from mt5_agents.tick_collector import TickArchive, last_closed_boundary
from mt5_agents.tick_data import TickQualityError

BASE = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class Broker:
    COPY_TICKS_INFO = 1

    def __init__(self, mode="quotes"):
        self.mode = mode
        self.calls = []

    def account_info(self): return SimpleNamespace(login=123, server="DemoServer")
    def symbol_info(self, symbol): return SimpleNamespace(point=0.01)

    def copy_ticks_range(self, symbol, start, end, flags):
        self.calls.append((symbol, start, end, flags))
        if self.mode == "none": return None
        dtype = [('time_msc','i8'),('bid','f8'),('ask','f8'),('flags','i4')]
        if self.mode == "empty": return np.array([], dtype=dtype)
        start_ms = int(start.timestamp() * 1000)
        # Exactly on the right boundary should be excluded from this partition.
        return np.array([(start_ms,3000.,3000.2,6),
                         (start_ms+1000,3000.1,3000.3,6),
                         (int(end.timestamp()*1000),3000.2,3000.4,6)], dtype=dtype)


def test_invalid_and_too_long_window_never_hits_mt5(tmp_path):
    broker = Broker()
    archive = TickArchive(tmp_path, broker, 123, "DemoServer")
    with pytest.raises(ValueError, match="120"):
        archive.collect("XAUUSD", BASE, BASE + timedelta(hours=3))
    with pytest.raises(ValueError, match="filesystem"):
        archive.collect("../secret", BASE, BASE+timedelta(seconds=60))
    assert broker.calls == []


def test_missing_broker_response_does_not_checkpoint(tmp_path):
    broker = Broker(mode="none")
    archive = TickArchive(tmp_path, broker, 123, "DemoServer")
    with pytest.raises(TickQualityError, match="None"):
        archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))
    assert archive.latest_end("XAUUSD") is None


def test_empty_is_unverified_gap_not_a_fabricated_file(tmp_path):
    broker = Broker(mode="empty")
    archive = TickArchive(tmp_path, broker, 123, "DemoServer")
    result = archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))
    assert result["state"] == "NO_TICKS_UNVERIFIED"
    assert archive.latest_end("XAUUSD") == int((BASE+timedelta(seconds=60)).timestamp()*1000)
    assert not list(tmp_path.rglob("*.parquet"))


def test_identity_mismatch_never_archives(tmp_path):
    broker = Broker()
    archive = TickArchive(tmp_path, broker, 123, "DifferentServer")
    with pytest.raises(RuntimeError, match="identity"):
        archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))
    assert broker.calls == []


def test_alignment_boundary():
    assert last_closed_boundary(BASE+timedelta(seconds=79), 60, delay_seconds=10) == BASE+timedelta(seconds=60)


def test_mocked_manifest_control_flow_half_open_dedupe_and_tampering(tmp_path, monkeypatch):
    # Control-flow unit test only; the REQUIRED real Parquet test is separate.
    import mt5_agents.tick_collector as collector
    def fake_writer(frame, path):
        assert len(frame) == 2
        Path(path).write_bytes(b"NOT-A-PARQUET-FILE")
    monkeypatch.setattr(collector, "write_verified_parquet", fake_writer)
    monkeypatch.setattr(collector, "verify_parquet", lambda path: {"schema_ok":False})
    broker = Broker()
    archive = TickArchive(tmp_path, broker, 123, "DemoServer")
    result = archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))
    assert result["state"] == "ARCHIVED" and result["count"] == 2
    p = Path(result["path"])
    assert p.exists() and "date=2026-11-20" in str(p)
    again = archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))
    assert again["already_indexed"] and len(broker.calls) == 1
    p.write_bytes(b"TAMPERED")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))


def test_real_parquet_always_required(tmp_path):
    import pyarrow.parquet as pq  # no skipping this required gate
    broker = Broker()
    archive = TickArchive(tmp_path, broker, 123, "DemoServer")
    result = archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))
    table = pq.read_table(result["path"])
    assert table.num_rows == 2
    assert table.column("bid").to_pylist() == [3000., 3000.1]


def test_overlapping_windows_are_rejected(tmp_path):
    archive = TickArchive(tmp_path, Broker(mode="empty"), 123, "DemoServer")
    archive.collect("XAUUSD", BASE, BASE+timedelta(seconds=60))
    with pytest.raises(RuntimeError, match="Overlapping"):
        archive.collect("XAUUSD", BASE+timedelta(seconds=30), BASE+timedelta(seconds=90))
