"""Run with: python -m unittest discover -s tests -p test_phase363_polars_engine.py -v"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from phase363_polars_engine import verify_manifest, find_latest_completed, save_derived_only, calculate_metrics


def example(root, symbol='XAUUSD', start=1795433820000, n=5):
    ident='a'*20; end=start+60000
    folder=root/'runtime'/'shadow_tick_archive'/'parquet'/f'identity={ident}'/f'symbol={symbol}'/'raw_date=2026-11-23'
    folder.mkdir(parents=True,exist_ok=True)
    file=folder/f'raw_{start}_{end}.parquet'
    file.write_bytes(b'placeholder parquet' * n)
    payload={
        'sha256':hashlib.sha256(file.read_bytes()).hexdigest(), 'count':n,
        'schema_version':'shadow-v1','symbol':symbol,'identity_tag':ident,
        'window_start_raw_ms':start,'window_end_raw_ms_exclusive':end,
        'timebase_status':'UNVERIFIED_PENDING_DST','capture_batch_id':'batch-id',
    }
    file.with_suffix('.manifest.json').write_text(json.dumps(payload),encoding='utf8')
    return file,payload


class MetadataSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.file,self.meta=example(self.root)
    def tearDown(self):self.temp.cleanup()
    def test_manifest_matches(self):self.assertIs(verify_manifest(self.file,self.meta),self.meta)
    def test_manifest_sha_fails_closed(self):
        self.file.write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError,'SHA256'):verify_manifest(self.file,self.meta)
    def test_manifest_missing_required(self):
        m=dict(self.meta);m.pop('count')
        with self.assertRaises(ValueError):verify_manifest(self.file,m)
    def test_manifest_bad_status(self):
        m=dict(self.meta);m['timebase_status']='CERTIFIED_UTC'
        with self.assertRaises(ValueError):verify_manifest(self.file,m)
    def test_manifest_bad_time_span(self):
        m=dict(self.meta);m['window_end_raw_ms_exclusive']+=1
        with self.assertRaises(ValueError):verify_manifest(self.file,m)
    def test_manifest_bad_identity(self):
        m=dict(self.meta);m['identity_tag']='b'*20
        with self.assertRaises(ValueError):verify_manifest(self.file,m)
    def test_latest_completed(self):
        p,m=find_latest_completed(self.root/'runtime'/'shadow_tick_archive','XAUUSD')
        self.assertEqual(p,self.file)
    def test_reject_multiple_identities(self):
        p,m=example(self.root, symbol='XAUUSD', start=1795433880000)
        path=p.parent.parent.parent
        dest=path.with_name('identity='+'b'*20)
        import shutil
        shutil.copytree(path,dest)
        for manifest in dest.rglob('*.manifest.json'):
            d=json.loads(manifest.read_text());d['identity_tag']='b'*20;manifest.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'MULTIPLE_BROKER'):find_latest_completed(self.root/'runtime'/'shadow_tick_archive','XAUUSD')
    def test_derived_no_overwrite(self):
        c={'window_id':'raw-'+'a'*16,'symbol':'XAUUSD','v':1}
        p=save_derived_only(self.root,c)
        self.assertEqual(save_derived_only(self.root,c),p)
        c['v']=2
        with self.assertRaisesRegex(ValueError,'CONFLICT'):save_derived_only(self.root,c)


class PolarsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try: import polars
        except ImportError: raise unittest.SkipTest('Polars unavailable locally; required on Windows for integration')
        cls.pl=polars
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.file,self.meta=example(self.root,n=12)
    def tearDown(self):self.temp.cleanup()
    def write(self,times, mids, spreads=None):
        pl=self.pl
        spreads=spreads or [0.15]*len(mids)
        bids=[v-s/2 for v,s in zip(mids,spreads)]
        asks=[v+s/2 for v,s in zip(mids,spreads)]
        pl.DataFrame({'raw_time_msc':times, 'bid':bids, 'ask':asks,
                      'spread_points':[s*100 for s in spreads],
                      'capture_batch_id':['batch-id']*len(times),
                      'timebase_status':['UNVERIFIED_PENDING_DST']*len(times)}).write_parquet(self.file)
        self.meta['count']=len(times);self.meta['sha256']=hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.file.with_suffix('.manifest.json').write_text(json.dumps(self.meta))
    def test_regular_bars_and_contract(self):
        start=self.meta['window_start_raw_ms']
        ts=[start+i*5000+100 for i in range(10)]
        prices=[3500+(-1)**i*i*.01 for i in range(10)]
        self.write(ts,prices)
        # calculate_metrics uses the already installed sibling contract directory.
        if not (ROOT/'phase363_contract_v2').exists():
            raise RuntimeError('Test requires v2 contract installed in project')
        c=calculate_metrics(self.file,self.meta)
        self.assertEqual(c['metrics']['rv_sample_count'],9)
        self.assertEqual(c['metrics']['realized_volatility_method'],'STD_LOG_RETURNS_5S')
        self.assertIsNone(c['utc_start_ms'])
        self.assertIsNone(c['metrics']['rolling_z_score_vol'])
        self.assertAlmostEqual(c['metrics']['tick_velocity_per_sec'],10/60,places=7)
    def test_skips_nonconsecutive_buckets(self):
        start=self.meta['window_start_raw_ms']
        ts=[start+100,start+5100,start+10100,start+30100,start+35100,start+40100]
        self.write(ts,[3500,3501,3502,3503,3504,3505])
        c=calculate_metrics(self.file,self.meta)
        self.assertEqual(c['metrics']['rv_sample_count'],4)
    def test_empty_synthetic_returns_when_no_consecutive_bins(self):
        start=self.meta['window_start_raw_ms']
        ts=[start+100,start+10100,start+20100,start+30100,start+40100]
        self.write(ts,[3500,3501,3502,3503,3504])
        c=calculate_metrics(self.file,self.meta)
        self.assertEqual(c['metrics']['rv_sample_count'],0)
        self.assertIsNone(c['metrics']['realized_volatility'])
        self.assertIn('INSUFFICIENT_SAMPLED_RETURNS',c['quality']['reasons'])
    def test_frozen_spread_does_not_claim_clamp(self):
        start=self.meta['window_start_raw_ms']
        ts=[start+i*5000+100 for i in range(6)]
        self.write(ts,[3500+i*.01 for i in range(6)])
        c=calculate_metrics(self.file,self.meta)
        self.assertTrue(c['metrics']['spread_constant_observed_bool'])
        self.assertIsNone(c['metrics']['spread_clamped_bool'])
    def test_reject_invalid_price(self):
        start=self.meta['window_start_raw_ms']
        self.write([start+100,start+5100],[3500,3501],spreads=[0.15,-1.0])
        with self.assertRaisesRegex(ValueError,'INVALID_RAW_ROWS'):calculate_metrics(self.file,self.meta)

if __name__=='__main__': unittest.main()
