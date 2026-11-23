"""Stdlib-only tests. Real Windows capture archive is not bundled or accessed."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

PROJECT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AT = load(PROJECT / 'scripts' / 'phase364_window_attestation.py', 'test_phase364_attest')
GUARD = load(PROJECT / 'phase363_contract_v2' / 'strict_guard.py', 'test_phase364_guard')
ENGINE = load(PROJECT / 'scripts' / 'phase363_polars_engine.py', 'test_phase364_polars')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for rel in ('scripts/phase363_polars_engine.py', 'phase363_contract_v2/strict_guard.py'):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / rel, target)
        self.symbol = 'EURUSD'
        self.start = 1795436820000
        self.end = self.start + 60_000
        self.host_ms = self.start - 7_200_000 + 15_000
        self.batch_id = str(uuid.uuid4())
        self.identity = 'a'*20
        d = (self.root / 'runtime' / 'shadow_tick_archive' / 'parquet' /
             f'identity={self.identity}' / f'symbol={self.symbol}' / 'raw_date=2026-11-23')
        d.mkdir(parents=True)
        name = f'raw_{self.start}_{self.end}'
        self.parquet = d / f'{name}.parquet'
        self.parquet.write_bytes(b'SYNTHETIC_TEST_BYTES_NOT_REAL_PARQUET')
        self.manifest_file = d / f'{name}.manifest.json'
        self.manifest = {
            'schema_version':'shadow-v1', 'capture_batch_id':self.batch_id,
            'timebase_status':'UNVERIFIED_PENDING_DST', 'utc_time_certified':False,
            'dst_certified':False, 'symbol':self.symbol, 'identity_tag':self.identity,
            'broker_offset_candidate_sec':7200,
            'window_start_raw_ms':self.start,'window_end_raw_ms_exclusive':self.end,
            'host_request_start_utc_ms':self.host_ms - 13000,
            'host_response_end_utc_ms':self.host_ms,
            'count':119, 'sha256':AT._sha256(self.parquet),
            'archive_eligible_for_training':False, 'archive_eligible_for_execution':False,
        }
        self.write_manifest()
        self.context = {
            'schema_version':'phase363-quant-context-v2',
            'window_id':AT._window_id(self.symbol,self.identity,self.start,self.end,self.manifest['sha256']),
            'symbol':self.symbol,'raw_start_ms':self.start,'raw_end_exclusive_ms':self.end,
            'utc_start_ms':None,'timebase_status':'UNVERIFIED_PENDING_DST',
            'source':{'kind':'SHADOW_RAW_PARQUET','sha256':self.manifest['sha256'],'collector_schema':'shadow-v1'},
            'metrics':{'tick_count':119,'tick_velocity_per_sec':1.98333333,
                       'spread_p50_points':3.0,'spread_p95_points':4.0,
                       'spread_constant_observed_bool':False,'spread_clamped_bool':None,
                       'realized_volatility':4.448195508784473e-05,
                       'realized_volatility_method':'STD_LOG_RETURNS_5S','rv_sample_count':11,
                       'rolling_z_score_vol':None,'rolling_baseline_window_count':0,
                       'directional_tick_ratio':0.6101694915254238,'max_gap_seconds':8.276},
            'quality':{'flag':'DEGRADED',
                       'reasons':['TIMEBASE_UNVERIFIED','BASELINE_UNREADY','HEARTBEAT_UNVERIFIED'],
                       'baseline_ready':False,'five_sessions_certified':False,'heartbeat_status':'UNVERIFIED'},
            'permissions':{'allow_discussion':True,'allow_trade_decisions':False},
        }
        GUARD.validate_context(self.context)
        self.derived = (self.root / 'runtime' / 'quant_context_derived' / 'phase363-v2' /
                        self.symbol / f'{self.context["window_id"]}.json')
        self.derived.parent.mkdir(parents=True)
        self.write_context()

    def write_manifest(self):
        self.manifest_file.write_text(json.dumps(self.manifest), encoding='utf-8')

    def write_context(self):
        self.derived.write_text(json.dumps(self.context, indent=2), encoding='utf-8')

    def pulse(self, delta_ms, *, batch=False, alert=None, interval_ms=10000, mono_offset=0):
        ms = self.host_ms + delta_ms
        return {
            'schema_version':'shadow-watchdog-v1',
            'utc_iso':datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(timespec='milliseconds'),
            'host_utc_msc':ms,'monotonic_ns':10000000000000 + delta_ms*1_000_000 + mono_offset,
            'collector_alive_bool':True,'collector_pids':[2345,6789],
            'collector_root_count':1,'collector_process_scan':'COMPLETE',
            'mt5_connected_bool':None,'mt5_connection_check':'NOT_PROBED_NO_MT5_CALLS',
            'last_saved_batch_id':self.batch_id if batch else str(uuid.uuid4()),
            'last_saved_utc_msc':ms-20000,'lag_ms':20000,
            'watchdog_interval_ms':interval_ms,'watchdog_interval_overrun_bool':False,
            'wall_vs_monotonic_shift_ms':0.0,'wall_clock_shift_suspected_bool':False,
            'alerts':alert or [], 'timebase_status':'UNVERIFIED_PENDING_DST',
            'no_orders':True,'original_parquet_unchanged':True,
        }

    def save_pulses(self, pulses):
        path = self.root / 'runtime' / 'telemetry' / 'heartbeat.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(p)+'\n' for p in pulses),encoding='utf-8')
        return path

    def full_pulses(self):
        self.save_pulses([self.pulse(-9000),self.pulse(1000),self.pulse(11000,batch=True)])

    def attest(self, mode='deep', write=False, correlate=True):
        with patch.object(ENGINE,'calculate_metrics',return_value=deepcopy(self.context)) as calc:
            answer = AT.attest_one(self.root,self.context,self.derived,GUARD,ENGINE,
                                   mode=mode,write_deep_certificate=write,correlate=correlate)
        return answer, calc


class ProvenanceTests(Fixture):
    def test_source_sha_identity_window_batch_pass(self):
        m,parquet,mp,proof=AT.verify_provenance(self.root,self.context,ENGINE)
        self.assertEqual(proof['capture_batch_id'],self.batch_id)
        self.assertEqual(proof['provenance_status'],'SHA_IDENTITY_BATCH_WINDOW_VERIFIED')

    def test_reject_modified_source_bytes(self):
        self.parquet.write_bytes(b'MODIFIED')
        with self.assertRaisesRegex(ValueError,'SHA256_MISMATCH'):
            AT.verify_provenance(self.root,self.context,ENGINE)

    def test_reject_wrong_window_id(self):
        c=deepcopy(self.context);c['window_id']='raw-'+'f'*16
        with self.assertRaisesRegex(ValueError,'WINDOW_ID_DOES_NOT_MATCH'):
            AT.verify_provenance(self.root,c,ENGINE)

    def test_reject_wrong_batch_uuid(self):
        self.manifest['capture_batch_id']='not-a-uuid';self.write_manifest()
        with self.assertRaisesRegex(ValueError,'INVALID_CAPTURE_BATCH'):
            AT.verify_provenance(self.root,self.context,ENGINE)

    def test_reject_host_response_before_request(self):
        self.manifest['host_response_end_utc_ms']=self.manifest['host_request_start_utc_ms']-1
        self.write_manifest()
        with self.assertRaisesRegex(ValueError,'INVALID_HOST_QUERY'):
            AT.verify_provenance(self.root,self.context,ENGINE)

    def test_reject_false_certification_in_manifest(self):
        self.manifest['dst_certified']=True;self.write_manifest()
        with self.assertRaisesRegex(ValueError,'UNCERTIFIED_AND_RESEARCH_ONLY'):
            AT.verify_provenance(self.root,self.context,ENGINE)

    def test_duplicate_keys_rejected(self):
        self.derived.write_text('{"x":1,"x":2}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'DUPLICATE_JSON_KEY'):
            AT.contexts_to_check(self.root,self.symbol,1,GUARD)

    def test_saved_derived_context_selection(self):
        selected=AT.contexts_to_check(self.root,self.symbol,1,GUARD)
        self.assertEqual(selected[0][0],self.context)

    def test_reject_price_change_without_source_change_in_deep(self):
        self.context['metrics']['spread_p50_points']=3.5
        self.write_context()
        proof=AT.verify_provenance(self.root,self.context,ENGINE)[3]
        with patch.object(ENGINE,'calculate_metrics',return_value={'different':True}):
            with self.assertRaisesRegex(ValueError,'MISMATCH_RECOMPUTED'):
                AT.verify_metric_evidence(self.root,self.context,self.derived,proof,self.manifest,
                                          self.parquet,ENGINE,'deep',False)


class CertificateTests(Fixture):
    def test_fast_without_deep_certificate_not_verified(self):
        result,calc=self.attest(mode='fast')
        self.assertEqual(result['metrics_verification'],'NOT_RECOMPUTED_NO_PREVIOUS_DEEP_CERTIFICATE')
        self.assertFalse(result['evidence_complete_for_this_phase'])
        calc.assert_not_called()

    def test_deep_and_fast_round_trip(self):
        deep,calc=self.attest(mode='deep',write=True)
        self.assertEqual(deep['metrics_verification'],'RECOMPUTED_EXACT_MATCH')
        calc.assert_called_once()
        fast,calc=self.attest(mode='fast')
        self.assertEqual(fast['metrics_verification'],'PRIOR_DEEP_MATCH_SUPPORTED_BY_LOCAL_CACHE')
        calc.assert_not_called()

    def test_local_certificate_idempotent(self):
        self.attest(write=True)
        self.attest(write=True)
        folder=self.root/'runtime'/'window_attestations'/'phase364-v1'/self.symbol
        self.assertEqual(len(list(folder.glob('*.json'))),1)

    def test_fast_rejects_context_file_changed_after_certificate(self):
        self.attest(write=True)
        self.context['metrics']['spread_p50_points']=3.25
        self.write_context()
        with self.assertRaisesRegex(ValueError,'DEEP_CERTIFICATE_STALE'):
            self.attest(mode='fast')

    def test_fast_rejects_modified_engine_code(self):
        self.attest(write=True)
        with (self.root/'scripts'/'phase363_polars_engine.py').open('a',encoding='utf-8') as f:
            f.write('\n# TEST MODIFIED ENGINE\n')
        with self.assertRaisesRegex(ValueError,'DEEP_CERTIFICATE_STALE'):
            self.attest(mode='fast')

    def test_deep_default_no_files_modified(self):
        self.full_pulses()
        selected=[self.parquet,self.manifest_file,self.derived,
                  self.root/'runtime'/'telemetry'/'heartbeat.jsonl']
        before={str(p):AT._sha256(p) for p in selected}
        result,_=self.attest(mode='deep')
        self.assertTrue(result['evidence_complete_for_this_phase'])
        self.assertEqual(before,{str(p):AT._sha256(p) for p in selected})
        self.assertFalse((self.root/'runtime'/'window_attestations').exists())
        self.assertEqual(result['source_context_heartbeat_unchanged'],'UNVERIFIED')
        self.assertFalse(result['ready_for_model_publication'])


class HeartbeatTests(Fixture):
    def test_observed_process_around_host_response(self):
        self.full_pulses()
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE')
        self.assertTrue(hb['batch_id_seen_by_watchdog'])
        self.assertEqual(hb['max_inter_pulse_seconds'],10)

    def test_missing_heartbeat_unverified_not_confirmed_gap(self):
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'UNVERIFIED_MISSING_BRACKETING_HEARTBEATS')
        self.assertFalse(hb['batch_id_seen_by_watchdog'])

    def test_no_post_heartbeat_is_inconclusive(self):
        self.save_pulses([self.pulse(-9000)])
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'UNVERIFIED_MISSING_BRACKETING_HEARTBEATS')

    def test_detect_observed_cadence_gap_without_attributing_cause(self):
        self.save_pulses([self.pulse(-19000),self.pulse(18000)])
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'WATCHDOG_CADENCE_GAP_DETECTED_CAUSE_UNCLASSIFIED')

    def test_process_alert_blocks_health_observation(self):
        self.save_pulses([self.pulse(-9000),self.pulse(1000,alert=['COLLECTOR_PROCESS_NOT_FOUND'])])
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'COLLECTOR_OR_WATCHDOG_HEALTH_UNVERIFIED_OR_ALERT')

    def test_clock_step_suspected(self):
        self.save_pulses([self.pulse(-9000),self.pulse(1000,mono_offset=-5000*1_000_000)])
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'CLOCK_OR_MONOTONIC_SEQUENCE_UNCERTAIN')

    def test_manual_once_pulses_cannot_certify_loop(self):
        self.save_pulses([self.pulse(-9000,interval_ms=None),self.pulse(1000,interval_ms=None)])
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'COLLECTOR_OR_WATCHDOG_HEALTH_UNVERIFIED_OR_ALERT')

    def test_malformed_jsonl_fails_closed(self):
        path=self.save_pulses([self.pulse(-9000)])
        with path.open('ab') as f:f.write(b'{"incomplete":')
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_HEARTBEAT_TAIL'):
            AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)

    def test_corrupt_telemetry_does_not_write_deep_certificate(self):
        path=self.save_pulses([self.pulse(-9000)])
        with path.open('ab') as f:
            f.write(b'{not-complete')
        with self.assertRaisesRegex(ValueError, 'INCOMPLETE_HEARTBEAT_TAIL'):
            self.attest(mode='deep',write=True)
        self.assertFalse((self.root/'runtime'/'window_attestations').exists())

    def test_process_observed_even_if_specific_batch_not_scanned(self):
        # Watchdog scans manifests only periodically; no matching batch ID
        # does not negate independent process visibility at query-response time.
        self.save_pulses([self.pulse(-9000),self.pulse(1000)])
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE')
        self.assertFalse(hb['batch_id_seen_by_watchdog'])

    def test_corrupted_heartbeat_field_fails_closed(self):
        p=self.pulse(-9000);p['mt5_connected_bool']=True
        self.save_pulses([p])
        with self.assertRaisesRegex(ValueError,'UNEXPECTED_MT5_PROBE'):
            AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)

    def test_rotated_daily_file_detected(self):
        logs=self.root/'runtime'/'telemetry'
        logs.mkdir(parents=True)
        date=datetime.fromtimestamp(self.host_ms/1000,timezone.utc).date().isoformat()
        (logs/f'heartbeat-{date}.jsonl').write_text(
            json.dumps(self.pulse(-9000))+'\n'+json.dumps(self.pulse(1000))+'\n', encoding='utf-8')
        hb=AT.correlate_heartbeat(self.root,self.host_ms,self.batch_id)
        self.assertEqual(hb['status'],'WATCHDOG_PROCESS_OBSERVED_AROUND_QUERY_RESPONSE')


if __name__=='__main__':
    unittest.main()
