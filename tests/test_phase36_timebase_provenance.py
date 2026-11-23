"""Pure tests do not need MT5 or PyArrow; they never connect to a broker."""
from pathlib import Path
import importlib.util

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'phase36_timebase_provenance.py'
spec = importlib.util.spec_from_file_location('phase36_timebase_provenance', SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_offset_is_candidate_only():
    a = audit.candidate_offset([7198.0, 7199.2, 7199.7])
    assert a['status'] == 'CURRENT_SESSION_CANDIDATE_ONLY'
    assert a['candidate_offset_seconds'] == 7200
    assert a['historical_offset_certified'] is False


def test_mixed_offsets_fail_closed():
    a = audit.candidate_offset([7199.0, 7200.0, 10800.0])
    assert a['status'] == 'UNKNOWN_OR_INCONSISTENT'
    assert a['candidate_offset_seconds'] is None


def test_timestamps_are_internal_consistency_only():
    x = audit.archive_integrity([1000, 1100], [1000, 1100], [1.0, 1.1], [1.1, 1.2])
    assert x['timestamp_columns_equal'] is True
    assert x['out_of_order_count'] == 0
    assert x['invalid_bid_ask_count'] == 0


def test_archive_detects_out_of_order_and_bad_quotes():
    x = audit.archive_integrity([1100, 1000, 1000], [1100, 1000, 900],
                                [1.0, 1.0, 1.4], [1.1, 0.9, 1.1])
    assert x['timestamp_columns_equal'] is False
    assert x['out_of_order_count'] == 1
    assert x['duplicate_adjacent_milliseconds'] == 1
    assert x['invalid_bid_ask_count'] == 2


def test_empty_archive_is_rejected():
    assert audit.archive_integrity([], [], [], [])['valid'] is False
