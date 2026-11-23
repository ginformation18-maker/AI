import copy
import json
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from strict_guard import validate_context, validate_reply, load_reply_json, render_ar


def fixture():
    c=json.loads((ROOT/'example_context.json').read_text(encoding='utf8'))
    r=json.loads((ROOT/'example_replies.json').read_text(encoding='utf8'))
    return c,r


class ContextTests(unittest.TestCase):
    def test_example_passes(self):
        c,_=fixture(); validate_context(c)
    def test_invented_utc_fails(self):
        c,_=fixture(); c['utc_start_ms']=1795197600000
        with self.assertRaises(ValueError): validate_context(c)
    def test_invented_baseline_fails(self):
        c,_=fixture(); c['quality']['baseline_ready']=True
        with self.assertRaises(ValueError): validate_context(c)
    def test_zscore_unverified_fails(self):
        c,_=fixture(); c['metrics']['rolling_z_score_vol']=3.2
        with self.assertRaises(ValueError): validate_context(c)
    def test_spread_percentile_order(self):
        c,_=fixture(); c['metrics']['spread_p95_points']=12
        with self.assertRaises(ValueError): validate_context(c)
    def test_clamp_cannot_be_claimed(self):
        c,_=fixture(); c['metrics']['spread_clamped_bool']=True
        with self.assertRaises(ValueError): validate_context(c)
    def test_finite_metrics(self):
        c,_=fixture(); c['metrics']['tick_velocity_per_sec']=float('nan')
        with self.assertRaises(ValueError): validate_context(c)
    def test_no_trade_permission(self):
        c,_=fixture(); c['permissions']['allow_trade_decisions']=True
        with self.assertRaises(ValueError): validate_context(c)
    def test_extra_context_field(self):
        c,_=fixture(); c['market_depth']={'large_buyers':88}
        with self.assertRaises(ValueError): validate_context(c)
    def test_vol_method_requires_samples(self):
        c,_=fixture(); c['metrics']['realized_volatility_method']='STD_LOG_RETURNS_1S'; c['metrics']['realized_volatility']=0.01
        with self.assertRaises(ValueError): validate_context(c)
    def test_vol_computed_ok_without_baseline(self):
        c,_=fixture(); c['metrics']['realized_volatility_method']='STD_LOG_RETURNS_1S'; c['metrics']['realized_volatility']=0.01; c['metrics']['rv_sample_count']=59
        validate_context(c)
    def test_halted_timebase_fails_open(self):
        c,_=fixture(); c['timebase_status']='TIMEBASE_MISMATCH'; c['quality']['flag']='DEGRADED'
        with self.assertRaises(ValueError): validate_context(c)


class ReplyTests(unittest.TestCase):
    def test_all_valid(self):
        c,replies=fixture(); prior=[]
        for r in replies:
            validate_reply(r,c,prior)
            result=render_ar(r,c,prior)
            self.assertTrue(len(result)>15)
            prior.append(r)
    def test_json_no_free_text(self):
        c,replies=fixture(); r=copy.deepcopy(replies[0]); r['claim']='الذهب سيرتفع 300 دولار'
        with self.assertRaises(ValueError):validate_reply(r,c)
    def test_wrong_metric_reference(self):
        c,replies=fixture(); r=copy.deepcopy(replies[0]); r['evidence_refs']=['metrics.spread_p95_points']
        with self.assertRaises(ValueError):validate_reply(r,c)
    def test_wrong_finding(self):
        c,replies=fixture(); r=copy.deepcopy(replies[0]); r['finding_codes']=['VOL_WINDOW_RECORDED']
        with self.assertRaises(ValueError):validate_reply(r,c)
    def test_false_constant_claim(self):
        c,replies=fixture(); c['metrics']['spread_constant_observed_bool']=False
        with self.assertRaises(ValueError):validate_reply(replies[0],c)
    def test_previous_role_required(self):
        c,replies=fixture()
        with self.assertRaises(ValueError):validate_reply(replies[1],c)
    def test_wrong_window(self):
        c,replies=fixture(); r=copy.deepcopy(replies[0]); r['window_id']='raw-0000000000000000'
        with self.assertRaises(ValueError):validate_reply(r,c)
    def test_reject_duplicate_json_keys(self):
        c,replies=fixture(); raw=json.dumps(replies[0],ensure_ascii=False).replace('"agent":','"agent":"MARKET_MICROSTRUCTURE","agent":')
        with self.assertRaises(ValueError):load_reply_json(raw,c)
    def test_reject_trade_fields(self):
        c,replies=fixture(); r=copy.deepcopy(replies[0]); r['order_send']={'side':'BUY'}
        with self.assertRaises(ValueError):validate_reply(r,c)
    def test_reject_nan_json(self):
        c,replies=fixture(); raw=json.dumps(replies[0]).replace('"window_id":','"prob":NaN,"window_id":')
        with self.assertRaises(ValueError):load_reply_json(raw,c)
    def test_halted_must_skip_model(self):
        c,replies=fixture(); c['timebase_status']='TIMEBASE_MISMATCH'; c['quality']['flag']='HALTED'; c['quality']['reasons'].append('TIMEBASE_MISMATCH')
        with self.assertRaises(ValueError):validate_reply(replies[0],c)
    def test_output_numbers_are_source_derived(self):
        c,replies=fixture(); x=render_ar(replies[0],c,[])
        self.assertIn('15.0',x); self.assertIn('8.28333',x)
        self.assertNotIn('شراء',x)


class SchemaTests(unittest.TestCase):
    def test_schema_syntax_and_examples_if_jsonschema_installed(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema optional; stdlib semantic tests still execute')
        c,replies=fixture()
        for name, data in [('quant_context_v2.schema.json',c),('agent_reply_v2.schema.json',replies[0])]:
            schema=json.loads((ROOT/name).read_text(encoding='utf8'))
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.validate(data,schema)

if __name__=='__main__': unittest.main()

class RequestTests(unittest.TestCase):
    def test_build_first_offline(self):
        from build_ollama_request_offline import make_request
        c,replies=fixture()
        request=make_request(c,'MARKET_MICROSTRUCTURE')
        self.assertEqual(request['model'],'qwen2.5:7b')
        self.assertFalse(request['stream'])
        self.assertIn('format',request)
        self.assertEqual(len(request['messages']),2)
    def test_block_skip_priors(self):
        from build_ollama_request_offline import make_request
        c,replies=fixture()
        with self.assertRaises(ValueError):make_request(c,'VOLATILITY_RISK')
    def test_all_roles_can_be_constructed_with_previous(self):
        from build_ollama_request_offline import make_request
        from strict_guard import ROLES
        c,replies=fixture()
        for i,role in enumerate(ROLES):
            r=make_request(c,role,replies[:i])
            self.assertEqual(r['model'],'qwen2.5:7b')
    def test_halted_request_rejected(self):
        from build_ollama_request_offline import make_request
        c,_=fixture(); c['timebase_status']='TIMEBASE_MISMATCH'; c['quality']['flag']='HALTED'; c['quality']['reasons'].append('TIMEBASE_MISMATCH')
        with self.assertRaises(ValueError):make_request(c,'MARKET_MICROSTRUCTURE')
