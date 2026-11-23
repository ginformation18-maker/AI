import unittest
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from strict_grounding import validate_context, validate_reply, context_from_quant_report
import json
from copy import deepcopy

CTX=json.loads((ROOT/'example_context.json').read_text(encoding='utf-8'))
RESP=json.loads((ROOT/'example_agent_replies.json').read_text(encoding='utf-8'))
class TestStrictGrounding(unittest.TestCase):
    def test_good_context(self): self.assertEqual(validate_context(CTX)['symbol'],'XAUUSD')
    def test_good_chain(self):
        seen=[]
        for r in RESP:
            validate_reply(r,CTX,seen)
            seen.append(r)
        self.assertEqual(len(seen),4)
    def test_bad_metric(self):
        x=deepcopy(CTX); x['metrics']['spread_p95_window_points']=-1
        with self.assertRaises(ValueError): validate_context(x)
    def test_baseline_claim(self):
        x=deepcopy(CTX); x['baseline_ready']=True
        with self.assertRaises(ValueError): validate_context(x)
    def test_unknown_metric_ref(self):
        x=deepcopy(RESP[0]); x['evidence_refs']=['new_indicator']
        with self.assertRaises(ValueError): validate_reply(x,CTX)
    def test_null_metric_ref(self):
        x=deepcopy(CTX); x['metrics']['quote_tick_count']=None
        with self.assertRaises(ValueError): validate_reply(RESP[0],x)
    def test_forged_number(self):
        x=deepcopy(RESP[0]); x['claim']='ارتفع المؤشر بنسبة 80% رغم عدم وجود دليل.'
        with self.assertRaises(ValueError): validate_reply(x,CTX)
    def test_unknown_indicator(self):
        x=deepcopy(RESP[0]); x['claim']='مؤشر RSI يدل على شراء.'
        with self.assertRaises(ValueError): validate_reply(x,CTX)
    def test_trade_field_forbidden(self):
        x=deepcopy(RESP[0]); x['order_side']='BUY'
        with self.assertRaises(ValueError): validate_reply(x,CTX)
    def test_invalid_sequence(self):
        with self.assertRaises(ValueError): validate_reply(RESP[2],CTX,RESP[:1])
    def test_unverified_mapping_from_report(self):
        report={'tool':'Phase 3.6.2 Read-only Quant Context','ready_for_baseline':False,
                'historical_offset_certified':False,'symbol':'XAUUSD','window_metrics':[{
                    'timebase':'BROKER_RAW_NOT_CERTIFIED_UTC', 'is_tradable_signal':False,
                    'sha256':'5f4983db7af217686f63f8862784dea185117a9ef45fd826b910606344109a2f',
                    'spread_points_p95_window_only':15,
                    'quote_ticks_per_second_mean_over_full_window':8.28333,
                    'mid_quote_realized_volatility_log_returns_window':0.0006623035,
                    'largest_intertick_gap_seconds':0.403,'quote_tick_count':497
                }]}
        self.assertEqual(context_from_quant_report(report),CTX)
    def test_no_multiple_windows_yet(self):
        report={'tool':'Phase 3.6.2 Read-only Quant Context','ready_for_baseline':False,
                'historical_offset_certified':False,'symbol':'XAUUSD','window_metrics':[]}
        with self.assertRaises(ValueError): context_from_quant_report(report)
if __name__=='__main__': unittest.main()
