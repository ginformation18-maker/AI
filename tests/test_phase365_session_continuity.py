"""Tests don't access MT5 and don't write any runtime archive."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import phase365_session_continuity as s


def pulse(ms, mono, *, healthy=True, once=False, clock=False, overrun=False, alerts=None, batch=None):
    from datetime import datetime, timezone
    return {
        'schema_version': 'shadow-watchdog-v1',
        'utc_iso': datetime.fromtimestamp(ms/1000, timezone.utc).isoformat(timespec='milliseconds'),
        'host_utc_msc': ms, 'monotonic_ns': mono,
        'collector_alive_bool': healthy, 'collector_pids': [321],
        'collector_root_count': 1 if healthy else 0,
        'collector_process_scan': 'COMPLETE',
        'mt5_connected_bool': None, 'mt5_connection_check': 'NOT_PROBED_NO_MT5_CALLS',
        'last_saved_batch_id': batch,
        'watchdog_interval_ms': None if once else 10000.0,
        'watchdog_interval_overrun_bool': overrun,
        'wall_clock_shift_suspected_bool': clock,
        'alerts': [] if alerts is None else alerts,
        'timebase_status': 'UNVERIFIED_PENDING_DST',
        'no_orders': True, 'original_parquet_unchanged': True,
    }


def fake_coverage(n=1):
    return {
        'assessment': 'PROVISIONAL_COVERAGE_ONLY', 'errors': [], 'warnings': [],
        'manifest_count': n, 'validated_window_count': n,
        'total_recorded_quote_ticks': n * 100,
        'groups': [{
            'identity_tag': 'abc', 'symbol': 'XAUUSD',
            'recorded_windows': n, 'recorded_quote_rows': n*100,
            'observed_raw_span_minutes': n,
            'missing_window_minutes_unclassified': 0,
            'host_observation_gaps_over_130s_count': 0,
            'candidate_session_minutes_not_certified': {'LONDON': n},
            'candidate_session_local_dates_not_certified': {'LONDON':['2026-11-23']},
        }],
    }


def fake_manifest(host=1200000, start=1600000):
    return {'identity_tag': 'abc', 'symbol':'XAUUSD',
            'raw_start_ms_not_certified_utc':start,
            'host_response_end_utc_ms':host,
            'capture_batch_id':'a-batch'}


class PulseBracketTests(unittest.TestCase):
    def test_healthy_bracket(self):
        arr = [pulse(1195000,100_000_000_000), pulse(1205000,110_000_000_000)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE')

    def test_missing_heartbeat_does_not_infer_capture_outage(self):
        self.assertEqual(s.evaluate_pulse_bracket(1200000,[])['process_evidence'],
                         'UNVERIFIED_MISSING_BRACKET')

    def test_before_too_old(self):
        arr = [pulse(1150000,100_000_000_000), pulse(1205000,155_000_000_000)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'UNVERIFIED_MISSING_BRACKET')

    def test_manual_once_cannot_certify(self):
        arr = [pulse(1195000,100_000_000_000,once=True), pulse(1205000,110_000_000_000,once=True)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'COLLECTOR_OR_WATCHDOG_UNVERIFIED_OR_ALERT')

    def test_cadence_gap_cause_unknown(self):
        arr = [pulse(1180000,100_000_000_000), pulse(1210000,130_000_000_000)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'WATCHDOG_CADENCE_GAP_CAUSE_UNKNOWN')

    def test_clock_shift(self):
        arr = [pulse(1195000,100_000_000_000), pulse(1205000,110_000_000_000,clock=True)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'CLOCK_OR_SEQUENCE_UNCERTAIN')

    def test_wall_monotonic_disagree(self):
        arr = [pulse(1195000,100_000_000_000), pulse(1205000,120_000_000_000)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'CLOCK_OR_SEQUENCE_UNCERTAIN')

    def test_collector_alert(self):
        arr = [pulse(1195000,100_000_000_000),pulse(1205000,110_000_000_000,healthy=False,alerts=['COLLECTOR_MISSING'])]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'COLLECTOR_OR_WATCHDOG_UNVERIFIED_OR_ALERT')

    def test_one_pulse_not_continuous(self):
        arr = [pulse(1200000,100_000_000_000)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'UNVERIFIED_INSUFFICIENT_CADENCE')

    def test_exact_pulse_followed_by_distinct_cadence(self):
        arr = [pulse(1200000,100_000_000_000), pulse(1210000,110_000_000_000)]
        self.assertEqual(s.evaluate_pulse_bracket(1200000,arr)['process_evidence'],
                         'WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE')


class MatrixTests(unittest.TestCase):
    def test_all_observed_but_no_utc_certification(self):
        arr = [pulse(1195000,100_000_000_000),pulse(1205000,110_000_000_000, batch='a-batch')]
        report = s.build_matrix(Path('.'), coverage_report=fake_coverage(), manifest_rows=[fake_manifest()], heartbeat_rows=arr)
        self.assertEqual(report['status'], 'PROVISIONAL_SOURCE_AND_PROCESS_EVIDENCE')
        self.assertFalse(report['utc_dst_certified'])
        self.assertFalse(report['five_sessions_certified'])
        self.assertFalse(report['baseline_ready'])
        self.assertEqual(report['heartbeat']['monitored_window_count'], 1)
        self.assertEqual(report['heartbeat']['batch_id_seen_window_count'], 1)

    def test_missing_batch_id_not_missing_source(self):
        arr = [pulse(1195000,100_000_000_000),pulse(1205000,110_000_000_000)]
        report = s.build_matrix(Path('.'), coverage_report=fake_coverage(), manifest_rows=[fake_manifest()], heartbeat_rows=arr)
        self.assertEqual(report['heartbeat']['monitored_window_count'], 1)
        self.assertEqual(report['heartbeat']['batch_id_seen_window_count'], 0)

    def test_missing_pulses_partial_not_sleep(self):
        report = s.build_matrix(Path('.'),coverage_report=fake_coverage(),manifest_rows=[fake_manifest()],heartbeat_rows=[])
        self.assertEqual(report['status'],'PROVISIONAL_SOURCE_EVIDENCE_PARTIAL_MONITORING')
        self.assertEqual(report['heartbeat']['classification_counts'], {'UNVERIFIED_MISSING_BRACKET':1})
        self.assertEqual(len(report['exception_or_window_details']),1)

    def test_audit_failure_closed(self):
        cov = fake_coverage();cov['errors'] = [{'code':'BROKEN'}]
        with self.assertRaisesRegex(ValueError,'AUDIT_FAILED'):
            s.build_matrix(Path('.'), coverage_report=cov, manifest_rows=[fake_manifest()],heartbeat_rows=[])

    def test_count_disagreement_fails(self):
        with self.assertRaisesRegex(ValueError,'ROW_COUNT_MISMATCH'):
            s.build_matrix(Path('.'),coverage_report=fake_coverage(2),manifest_rows=[fake_manifest()],heartbeat_rows=[])

    def test_short_details_truncates(self):
        cov=fake_coverage(30)
        rows=[fake_manifest(host=1200000+i*60000,start=1600000+i*60000) for i in range(30)]
        report=s.build_matrix(Path('.'),coverage_report=cov,manifest_rows=rows,heartbeat_rows=[])
        self.assertTrue(report['details_truncated'])
        self.assertEqual(len(report['exception_or_window_details']),25)

    def test_full_details_optin(self):
        cov=fake_coverage(30)
        rows=[fake_manifest(host=1200000+i*60000,start=1600000+i*60000) for i in range(30)]
        report=s.build_matrix(Path('.'),coverage_report=cov,manifest_rows=rows,heartbeat_rows=[],include_windows=True)
        self.assertFalse(report['details_truncated'])
        self.assertEqual(len(report['exception_or_window_details']),30)


class SourceReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.archive=self.root/'runtime'/'shadow_tick_archive'/'parquet'/'identity=abc'/'symbol=XAUUSD'/'raw_date=2026-11-23'
        self.archive.mkdir(parents=True)
        self.manifest=self.archive/'raw_1600000_1660000.manifest.json'
        self.d={'timebase_status':'UNVERIFIED_PENDING_DST','utc_time_certified':False,
                'host_request_start_utc_ms':1200000,'host_response_end_utc_ms':1200000,
                'window_start_raw_ms':1600000,'window_end_raw_ms_exclusive':1660000,
                'identity_tag':'abc','symbol':'XAUUSD', 'capture_batch_id':'example'}
        self.manifest.write_text(json.dumps(self.d), encoding='utf8')

    def test_valid_manifest_after_coverage_audit(self):
        out=s.load_verified_manifests(self.root,fake_coverage())
        self.assertEqual(out[0]['host_response_end_utc_ms'],1200000)

    def test_disallow_audit_manifest_count_race(self):
        with self.assertRaisesRegex(ValueError,'ARCHIVE_CHANGED'):
            s.load_verified_manifests(self.root,fake_coverage(2))

    def test_invalid_host_time(self):
        d=dict(self.d,host_response_end_utc_ms=1199000)
        self.manifest.write_text(json.dumps(d),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'HOST_RESPONSE_INVALID'):
            s.load_verified_manifests(self.root,fake_coverage())

    def test_timebase_uncertainty_required(self):
        d=dict(self.d,utc_time_certified=True)
        self.manifest.write_text(json.dumps(d),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'INCONSISTENT_TIMEBASE'):
            s.load_verified_manifests(self.root,fake_coverage())

    def test_fail_on_heartbeat_corrupt_jsonl(self):
        tel=self.root/'runtime'/'telemetry';tel.mkdir(parents=True)
        (tel/'heartbeat.jsonl').write_text('{not valid}\n',encoding='utf8')
        with self.assertRaises((ValueError,json.JSONDecodeError)):
            s.load_heartbeat_once(self.root,[fake_manifest()])

    def test_one_heartbeat_parsed_vetted(self):
        tel=self.root/'runtime'/'telemetry';tel.mkdir(parents=True)
        h=pulse(1200000,100_000_000_000)
        (tel/'heartbeat.jsonl').write_text(json.dumps(h)+'\n',encoding='utf8')
        res=s.load_heartbeat_once(self.root,[fake_manifest()])
        self.assertEqual(len(res),1)


if __name__ == '__main__':
    unittest.main()
