"""Offline test suite for the integration driver, no live market connections.

Run: python -m unittest discover -s tests -p test_phase363_context_guard_integration.py -v
On the user's Windows machine, the CLI subsequently tests REAL local Shadow files.
"""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def from_source(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BRIDGE = from_source(ROOT / 'scripts' / 'phase363_context_guard_integration.py',
                     'offline_phase363_bridge')
GUARD = from_source(ROOT / 'phase363_contract_v2' / 'strict_guard.py',
                    'offline_phase363_guard')


def snapshot(symbol):
    """Snapshot values are copied from console metrics supplied by user.

    They are contract examples, not re-fetched market data or verified files.
    """
    gold = symbol == 'XAUUSD'
    start = 1795436820000
    return {
        'schema_version': 'phase363-quant-context-v2',
        'window_id': ('raw-0c6b6f7e0568a5d9' if gold else 'raw-dc48cd966762e1a7'),
        'symbol': symbol,
        'raw_start_ms': start,
        'raw_end_exclusive_ms': start + 60000,
        'utc_start_ms': None,
        'timebase_status': 'UNVERIFIED_PENDING_DST',
        'source': {
            'kind': 'SHADOW_RAW_PARQUET',
            'sha256': ('418d8ef80867b2a8d3ec21405d78d1bc2343e4bf2110ba59a01c2dc0ff23b823'
                       if gold else 'a'*64),
            'collector_schema': 'shadow-v1',
        },
        'metrics': {
            'tick_count': 213 if gold else 119,
            'tick_velocity_per_sec': 3.55 if gold else 1.98333333,
            'spread_p50_points': 15.000000000009095 if gold else 2.9999999999974487,
            'spread_p95_points': 15.000000000009095 if gold else 4.000000000004,
            'spread_constant_observed_bool': gold,
            'spread_clamped_bool': None,
            'realized_volatility': 6.444687960258744e-05 if gold else 4.448195508784473e-05,
            'realized_volatility_method': 'STD_LOG_RETURNS_5S',
            'rv_sample_count': 11,
            'rolling_z_score_vol': None,
            'rolling_baseline_window_count': 0,
            'directional_tick_ratio': 0.49056603773584906 if gold else 0.6101694915254238,
            'max_gap_seconds': 2.131 if gold else 8.276,
        },
        'quality': {
            'flag': 'DEGRADED',
            'reasons': ['TIMEBASE_UNVERIFIED', 'BASELINE_UNREADY', 'HEARTBEAT_UNVERIFIED'],
            'baseline_ready': False,
            'five_sessions_certified': False,
            'heartbeat_status': 'UNVERIFIED',
        },
        'permissions': {'allow_discussion': True, 'allow_trade_decisions': False},
    }


class ContractIntegrationTests(unittest.TestCase):
    def test_real_console_gold_contract_acceptance(self):
        c = snapshot('XAUUSD')
        self.assertIs(GUARD.validate_context(c), c)
        result = BRIDGE.integrate_one(c, GUARD, 'FIXTURE_FROM_CONSOLE_NOT_RECOMPUTED')
        self.assertEqual(result['integration_status'], 'GUARD_INTEGRATION_PASS_SIMULATED_REPLIES')
        self.assertEqual(result['quality_flag'], 'DEGRADED')
        self.assertEqual(len(result['agent_replies']), 4)
        self.assertIn('SPREAD_WINDOW_RECORDED', result['agent_replies'][0]['finding_codes'])
        self.assertIn('TICK_RATE_RECORDED', result['agent_replies'][0]['finding_codes'])
        self.assertIn('QUALITY_DEGRADED', result['agent_replies'][2]['finding_codes'])
        self.assertEqual(set(result['negative_probes'].values()), {'REJECTED_AS_EXPECTED'})

    def test_real_console_euro_context_rejects_buy_from_direction_ratio(self):
        c = snapshot('EURUSD')
        result = BRIDGE.integrate_one(c, GUARD, 'FIXTURE_FROM_CONSOLE_NOT_RECOMPUTED')
        self.assertEqual(len(result['agent_replies']), 4)
        self.assertEqual(result['negative_probes']['UNAUTHORIZED_BUY_FROM_TICK_RATIO'],
                         'REJECTED_AS_EXPECTED')
        self.assertEqual(result['negative_probes']['FALSE_UTC_CERTIFIED_REPLY'],
                         'REJECTED_AS_EXPECTED')
        self.assertEqual(result['negative_probes']['CLAMPED_WHILE_NULL'],
                         'REJECTED_AS_EXPECTED')

    def test_context_cannot_claim_broker_clamp_from_constant_spread(self):
        c = snapshot('XAUUSD')
        c['metrics']['spread_clamped_bool'] = True
        with self.assertRaisesRegex(ValueError, 'clamping'):
            GUARD.validate_context(c)

    def test_context_cannot_claim_certified_utc_without_certificate(self):
        c = snapshot('EURUSD')
        c['timebase_status'] = 'CERTIFIED_UTC'
        with self.assertRaises(ValueError):
            GUARD.validate_context(c)

    def test_halted_skips_all_simulated_agents(self):
        c = snapshot('XAUUSD')
        c['timebase_status'] = 'TIMEBASE_MISMATCH'
        c['quality']['flag'] = 'HALTED'
        self.assertEqual(BRIDGE.integrate_one(c, GUARD, 'TEST')['integration_status'],
                         'HALTED_SKIP_MODEL_NO_DISCORD')

    def test_context_validation_rejects_tampered_timestamp(self):
        c = snapshot('EURUSD')
        c['utc_start_ms'] = c['raw_start_ms'] - 7200000
        with self.assertRaises(ValueError):
            GUARD.validate_context(c)

    def test_context_read_rejects_duplicate_keys(self):
        c = snapshot('EURUSD')
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'EURUSD' / f'{c["window_id"]}.json'
            p.parent.mkdir(parents=True)
            value = json.dumps(c).replace('"symbol":', '"symbol":"EURUSD","symbol":', 1)
            p.write_text(value, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'DUPLICATE_JSON_KEY'):
                BRIDGE.read_context_json(p, GUARD)

    def test_context_read_rejects_wrong_window_filename(self):
        c = snapshot('XAUUSD')
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'XAUUSD' / 'raw-ffffffffffffffff.json'
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps(c), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'FILENAME_MISMATCH'):
                BRIDGE.read_context_json(p, GUARD)

    def test_no_source_file_fail_closed(self):
        c = snapshot('XAUUSD')
        with tempfile.TemporaryDirectory() as td:
            engine = Mock()
            with self.assertRaisesRegex(ValueError, 'RAW_SOURCE_MISSING_OR_AMBIGUOUS'):
                BRIDGE._verify_equal_to_raw(Path(td), c, engine)
            engine.calculate_metrics.assert_not_called()

    def test_recomputation_mismatch_fails_closed(self):
        c = snapshot('XAUUSD')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = (root / 'runtime' / 'shadow_tick_archive' / 'parquet' /
                 'identity=1234567890abcdef1234' / 'symbol=XAUUSD' / 'raw_date=2026-11-23')
            d.mkdir(parents=True)
            name = f'raw_{c["raw_start_ms"]}_{c["raw_end_exclusive_ms"]}'
            p = d / f'{name}.manifest.json'
            p.write_text(json.dumps({'sha256': c['source']['sha256']}), encoding='utf-8')
            engine = Mock()
            different = deepcopy(c)
            different['metrics']['tick_velocity_per_sec'] += 1.0
            engine.calculate_metrics.return_value = different
            with self.assertRaisesRegex(ValueError, 'DERIVED_CONTEXT_MISMATCH'):
                BRIDGE._verify_equal_to_raw(root, c, engine)
            engine.calculate_metrics.assert_called_once()

    def test_stored_context_checked_against_recomputed_raw(self):
        c = snapshot('EURUSD')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / 'runtime' / 'quant_context_derived' / 'phase363-v2' / 'EURUSD'
            d.mkdir(parents=True)
            (d / f'{c["window_id"]}.json').write_text(json.dumps(c), encoding='utf-8')
            raw = (root / 'runtime' / 'shadow_tick_archive' / 'parquet' /
                   'identity=1234567890abcdef1234' / 'symbol=EURUSD' / 'raw_date=2026-11-23')
            raw.mkdir(parents=True)
            name = f'raw_{c["raw_start_ms"]}_{c["raw_end_exclusive_ms"]}'
            (raw / f'{name}.manifest.json').write_text(
                json.dumps({'sha256': c['source']['sha256']}), encoding='utf-8')
            engine = Mock()
            engine.calculate_metrics.return_value = deepcopy(c)
            actual, label = BRIDGE.load_context(root, 'EURUSD', 'derived', GUARD, engine)
            self.assertEqual(label, 'DERIVED_JSON_MATCHED_VERIFIED_RAW')
            self.assertEqual(actual, c)


if __name__ == '__main__':
    unittest.main()
