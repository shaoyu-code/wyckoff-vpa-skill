"""All OHLCV/chart fixtures are synthetic. No TV, trading or network acceptance."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from global_market_intelligence.opportunities.store import EventStore
from global_market_intelligence.opportunities.native.evidence_v11 import (
 audit_window, measure_span, compare_spans, reviewed_comparisons,
 freeze_research, verify_frozen_manifest, main_context_records, instant)
from global_market_intelligence.opportunities.native.layout_v11 import (
 visibility, validate_visibility, inspect_layout, arrange_labels, project, inverse_price)
from global_market_intelligence.opportunities.native.book_rules_v11 import (
 packet_rules, validate_rule_bindings)
from global_market_intelligence.opportunities.native.drawing import make_manifest, validate_manifest
from global_market_intelligence.opportunities.native.reviewer import compact_packet, validate_review
from global_market_intelligence.opportunities.native.tradingview import NativeError
from test_native_monitor import dataset, engine_result, review_value, packet, NOW
from test_native_delivery import MemoryChart


def wave_data():
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    bars=[]
    for i,(price,volume) in enumerate(zip([100,98,96,97,96,95],[90,100,400,120,90,100])):
        t=start+timedelta(hours=i*4)
        bars.append({'opened_at':t.isoformat(),'closed_at':(t+timedelta(hours=4)).isoformat(),
                     'open':price+1,'high':price+2,'low':price-1,'close':price,'volume':volume})
    return bars


def minimal_dataset():
    bars=wave_data()
    return {'bars':bars,'captured_at':'2026-01-02T01:00:00Z','timeframe':'4H',
      'native_by_open':{b['opened_at']:{'time':int(instant(b['opened_at']).timestamp()),
           **{k:b[k] for k in ('open','high','low','close','volume')}} for b in bars}}


def span(bars, start=0, end=2, **kwargs):
    return measure_span(bars,start=start,end=end,cutoff='2026-02-01T00:00:00Z',**kwargs)


class WindowAndWaveTests(unittest.TestCase):
    def test_audit_checks_all_rows(self):
        self.assertEqual(audit_window(minimal_dataset())['rows'],6)

    def test_corrupt_middle_volume_fails_even_when_endpoints_match(self):
        ds=minimal_dataset();ds['bars'][2]['volume']=100
        with self.assertRaisesRegex(NativeError,'VALUE_MISMATCH'):audit_window(ds)

    def test_deleted_middle_bar_fails(self):
        ds=minimal_dataset();ds['bars'].pop(2)
        with self.assertRaisesRegex(NativeError,'COVERAGE'):audit_window(ds)

    def test_duplicate_bar_fails(self):
        ds=minimal_dataset();ds['bars'].insert(3,deepcopy(ds['bars'][2]))
        with self.assertRaisesRegex(NativeError,'OVERLAP'):audit_window(ds)

    def test_reordered_rows_fail(self):
        ds=minimal_dataset();ds['bars'][2:4]=reversed(ds['bars'][2:4])
        with self.assertRaisesRegex(NativeError,'ORDER'):audit_window(ds)

    def test_actual_original_capture_is_rechecked(self):
        ds=dataset();ds['bars'][10]['volume']=1;ds['native_by_open'][ds['bars'][10]['opened_at']]['volume']=1
        with self.assertRaisesRegex(NativeError,'BOUND_TO_CAPTURE'):audit_window(ds)

    def test_missing_volume_is_not_zero(self):
        ds=minimal_dataset();ds['bars'][2]['volume']=None
        with self.assertRaisesRegex(NativeError,'NONFINITE'):audit_window(ds)

    def test_nan_and_boolean_are_not_numbers(self):
        for value in [float('nan'),True,float('inf')]:
            with self.subTest(value=value):
                bars=wave_data();bars[2]['volume']=value
                with self.assertRaises(NativeError):span(bars)

    def test_negative_volume_rejected(self):
        bars=wave_data();bars[1]['volume']=-1
        with self.assertRaises(NativeError):span(bars)

    def test_impossible_ohlc_rejected(self):
        bars=wave_data();bars[1]['low']=bars[1]['high']+1
        with self.assertRaises(NativeError):span(bars)

    def test_full_span_volume_not_endpoint_sum(self):
        self.assertEqual(span(wave_data(),0,5)['volume_total'],900)

    def test_volume_comparison_changes_with_middle_bar(self):
        bars=wave_data();before=span(bars,0,5);bars[2]['volume']=100;after=span(bars,0,5)
        self.assertEqual((before['volume_total'],after['volume_total']),(900,600))

    def test_time_basis_and_unequal_duration_preserved(self):
        bars=wave_data();r=compare_spans(span(bars,0,2),span(bars,3,4))
        self.assertIn('UNEQUAL_TRADING_DURATION_COMPARE_RATE_ALSO',r['flags'])
        self.assertNotEqual(r['volume_ratio'],r['volume_rate_ratio'])

    def test_zero_denominator_is_none_not_zero(self):
        bars=wave_data()
        for b in bars[:3]:b['volume']=0;b['close']=100;b['high']=110
        r=compare_spans(span(bars,0,2),span(bars,3,5))
        self.assertIsNone(r['volume_ratio']);self.assertIsNone(r['progress_ratio'])
        self.assertIsNone(r['probability'])

    def test_unfinished_span_does_not_claim_final_contraction(self):
        bars=wave_data();r=compare_spans(span(bars,0,2),span(bars,3,5,ongoing=True))
        self.assertIn('INCOMPLETE_SPAN_NO_FINAL_CONTRACTION_CLAIM',r['flags'])

    def test_endpoint_basis_must_match(self):
        bars=wave_data()
        with self.assertRaisesRegex(NativeError,'ENDPOINT_BASIS'):
            compare_spans(span(bars),span(bars,3,5,start_field='high'))

    def test_no_backfilled_endpoint_from_later_bar(self):
        bars=wave_data()
        with self.assertRaisesRegex(NativeError,'FUTURE'):
            measure_span(bars,start=0,end=5,cutoff=bars[2]['closed_at'])

    def test_late_observation_is_not_known_at_close(self):
        bars=wave_data();bars[1]['observed_at']='2026-02-02T00:00:00Z'
        with self.assertRaisesRegex(NativeError,'FUTURE'):span(bars)

    def test_future_suffix_does_not_change_frozen_span_measurement(self):
        bars=wave_data();old=span(bars,0,2);suffix=deepcopy(bars[-1])
        suffix.update(opened_at='2026-01-03T00:00:00Z',closed_at='2026-01-03T04:00:00Z')
        self.assertEqual(old,span(bars+[suffix],0,2))

    def test_timezone_comparison_uses_instants(self):
        self.assertEqual(instant('2026-01-01T08:00:00+08:00'),instant('2026-01-01T00:00:00Z'))

    def test_naive_timestamps_rejected(self):
        with self.assertRaisesRegex(NativeError,'TIMEZONE'):instant('2026-01-01T00:00:00')

    def test_negative_price_linear_measurement_allowed(self):
        bars=wave_data()
        for b in bars:
            for field in ('open','high','low','close'):b[field]-=110
        value=span(bars);self.assertIsNone(value['return_fraction'])
        self.assertEqual(value['net_change'],-4)

    def test_live_packet_keeps_middle_history(self):
        ds=dataset()
        p=compact_packet(ds,None,engine_result(ds),None)
        self.assertEqual(len(p['daily']['bars']),len(ds['bars']))
        self.assertEqual([b['bar_index'] for b in p['daily']['bars']],list(range(len(ds['bars']))))
        self.assertEqual(p['daily']['window_audit']['rows'],len(ds['bars']))

    def test_comparison_requests_compute_not_trust_model_numbers(self):
        b=wave_data()
        def select(i,j):return dict(start_index=i,end_index=j,start_price_field='close',end_price_field='close',ongoing=False)
        ds={'bars':b,'captured_at':'2026-02-01T00:00:00Z'}
        result=reviewed_comparisons({'wave_comparisons':[{'id':'cmp','reference':select(0,2),'current':select(3,5),'basis':'synthetic'}]},ds)
        self.assertEqual(result[0]['reference']['volume_total'],590)
        self.assertEqual(result[0]['current']['volume_total'],310)


def label(key, time, price):
    return {'marker':key,'shape':'text','text':'测试 '+key,'points':[{'time':time,'price':price}],
            'overrides':{'intervalsVisibilities':visibility('4H')},
            'display_priority':1}


def view():
    return {'id':'SYNTH_VIEW','product_id':'SYNTH','timeframe':'4H','data_cutoff':'2026-01-01',
       'time_x':{str(t):t*35+30 for t in range(12)},'price_low':80,'price_high':120,
       'price_y_top':20,'price_y_bottom':320,'work_rect':[10,10,490,340],
       'obstacles':[],'scale':'linear','bbox_basis':'TOP_LEFT_NATIVE_MEASURED',
       'text_sizes':{'[GMI:a]':[70,24],'[GMI:b]':[70,24]}}


class LayoutTests(unittest.TestCase):
    def test_timeframe_isolation_all_declared_display_periods(self):
        for tf in ('1M','1W','1D','12H','4H'):
            v=visibility(tf)
            enabled=[k for k in ('seconds','minutes','hours','days','weeks','months','ticks','ranges') if v[k]]
            self.assertEqual(len(enabled),1)
            if tf=='4H':self.assertEqual((v['hoursFrom'],v['hoursTo']),(4,4))
            if tf=='12H':self.assertEqual((v['hoursFrom'],v['hoursTo']),(12,12))

    def test_history_hidden_but_not_deleted(self):
        v=visibility(None)
        self.assertFalse(any(v[k] for k in ('seconds','minutes','hours','days','weeks','months','ticks','ranges')))

    def test_wrong_period_is_rejected(self):
        obj=label('[GMI:a]',3,100);obj['overrides']['intervalsVisibilities']=visibility('1D')
        with self.assertRaisesRegex(NativeError,'LEAK'):validate_visibility(obj,'4H')

    def test_coincident_labels_detected(self):
        r=inspect_layout([label('[GMI:a]',3,100),label('[GMI:b]',3,100)],view())
        self.assertFalse(r['passed'])

    def test_planner_moves_text_but_keeps_real_anchor(self):
        m={'product_id':'SYNTH','timeframe':'4H','data_cutoff':'2026-01-01',
           'items':[label('[GMI:a]',3,100),label('[GMI:b]',3,100)]}
        result=arrange_labels(m,view())
        self.assertTrue(result['layout_geometry']['passed'])
        self.assertEqual(m['items'][0]['points'],[{'time':3,'price':100}])
        for item in result['items'][:2]:
            self.assertEqual(item['semantic_anchor'],{'time':3,'price':100})
        leaders=[x for x in result['items'] if x['shape']=='trend_line']
        self.assertTrue(leaders)
        self.assertEqual(leaders[0]['points'][0],{'time':3,'price':100})

    def test_unsatisfiable_does_not_hide_labels(self):
        v=view();v['obstacles']=[[0,0,1000,1000]]
        m={'product_id':'SYNTH','timeframe':'4H','data_cutoff':'2026-01-01','items':[label('[GMI:a]',3,100)]}
        original=deepcopy(m)
        with self.assertRaisesRegex(NativeError,'UNSATISFIABLE'):arrange_labels(m,v)
        self.assertEqual(m,original)

    def test_clipping_detected(self):
        r=inspect_layout([label('[GMI:a]',11,78)],view());self.assertFalse(r['passed'])

    def test_native_measured_dimensions_required(self):
        v=view();v['bbox_basis']='GUESSED'
        with self.assertRaisesRegex(NativeError,'METRICS'):arrange_labels(
            {'product_id':'SYNTH','timeframe':'4H','data_cutoff':'2026-01-01','items':[]},v)

    def test_product_or_cutoff_switch_rejects_old_view(self):
        v=view()
        with self.assertRaisesRegex(NativeError,'IDENTITY'):arrange_labels(
            {'product_id':'OTHER','timeframe':'4H','data_cutoff':'2026-01-01','items':[]},v)

    def test_log_projection_roundtrip(self):
        v=view();v['scale']='log'
        _,y=project({'time':3,'price':100},v)
        self.assertAlmostEqual(inverse_price(y,v),100)

    def test_cannot_use_log_for_negative_prices(self):
        v=view();v['scale']='log';v['price_low']=-10
        with self.assertRaisesRegex(NativeError,'NONPOSITIVE'):project({'time':3,'price':5},v)

    def test_missing_time_is_not_interpolated_across_sessions(self):
        with self.assertRaisesRegex(NativeError,'TIME_OUTSIDE'):project({'time':100,'price':100},view())


class FrozenDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.store=EventStore(self.path/'archive')
        self.ds=dataset();p=packet();self.review=validate_review(review_value(p),p)
        self.manifest=make_manifest(self.ds,self.review,symbol='SYNTH:UNIT',layout_id='SYNTH_LAYOUT',
                                   episode_id='SYNTH',price_tolerance=.001)

    def freeze(self):
        return freeze_research(self.store,self.ds,self.review,self.manifest,at=NOW)

    def test_immutable_exact_freeze_and_readback(self):
        m=self.freeze();verify_frozen_manifest(self.store,m)
        frozen=self.store.read_snapshot(m['frozen_research_sha256'])
        self.assertEqual(frozen['dataset'],self.ds)

    def test_freeze_idempotent_without_backdating(self):
        first=self.freeze()
        second=freeze_research(self.store,self.ds,self.review,self.manifest,at='2026-09-08T00:00:00Z')
        self.assertEqual(first,second);self.assertEqual(len(self.store.events()),1)

    def test_changed_plan_rejected_before_tv_action(self):
        m=self.freeze();m['items'][0]['points'][0]['price']+=1
        tv=MemoryChart()
        with self.assertRaisesRegex(NativeError,'FROZEN_MANIFEST_CHANGED'):
            tv.deliver(m,store=self.store,at=NOW,evidence_dir=self.path/'tv')
        self.assertEqual(tv.seq,0)

    def test_missing_freeze_ledger_rejected(self):
        m=self.freeze()
        with patch.object(self.store,'events',return_value=[]):
            with self.assertRaisesRegex(NativeError,'LEDGER'):verify_frozen_manifest(self.store,m)

    def test_failed_disk_freeze_no_market_write(self):
        tv=MemoryChart()
        with patch.object(self.store,'snapshot',side_effect=OSError('synthetic full disk')):
            with self.assertRaises(OSError):self.freeze()
        self.assertEqual(tv.seq,0)

    def test_native_readback_does_not_certify_visual(self):
        m=self.freeze();tv=MemoryChart()
        receipt=tv.deliver(m,store=self.store,at=NOW,evidence_dir=self.path/'tv')
        self.assertTrue(receipt['native_readback_complete'])
        self.assertFalse(receipt['v11_complete'])
        self.assertTrue(receipt['visual_acceptance'].startswith('NOT_RUN'))
        self.assertEqual(tv.items['human']['properties']['text'],'Do not alter human annotations')

    def test_repeated_delivery_no_duplicate(self):
        m=self.freeze();tv=MemoryChart()
        tv.deliver(m,store=self.store,at=NOW,evidence_dir=self.path/'tv')
        seq=tv.seq
        receipt=tv.deliver(m,store=self.store,at=NOW,evidence_dir=self.path/'tv')
        self.assertEqual(tv.seq,seq);self.assertEqual(receipt['writes'],0)

    def test_removed_historical_objects_have_no_visible_period(self):
        tv=MemoryChart();m=self.freeze()
        tv.deliver(m,store=self.store,at=NOW,evidence_dir=self.path/'tv')
        changed=deepcopy(self.manifest);removed=changed['items'].pop(0)
        changed=freeze_research(self.store,self.ds,self.review,changed,at='2026-09-08T00:00:00Z')
        receipt=tv.deliver(changed,store=self.store,at='2026-09-08T00:00:00Z',evidence_dir=self.path/'tv2')
        old=next(x for x in receipt['verified_objects'] if x['marker']==removed['marker'])
        self.assertTrue(old['retired']);validate_visibility(old,'1D')


class BookAndIsolationTests(unittest.TestCase):
    def test_all_three_books_equal_no_claim_of_full_coverage(self):
        p=packet_rules();self.assertEqual(set(p['books']),{'B1','B2','B3'})
        self.assertEqual(p['priority'],'EQUAL_NO_PRIMARY_BOOK')
        self.assertIn('NOT_FULL',p['coverage'])
        self.assertEqual({r['book'] for c in p['cards'] for r in c['source']},{'B1','B2','B3'})

    def test_unknown_rule_or_bar_rejected(self):
        for binding in [
            {'rule_id':'MADE_UP','bar_indices':[0],'application':'x','counterevidence':'y'},
            {'rule_id':'READ_ORDER','bar_indices':[100],'application':'x','counterevidence':'y'}]:
            with self.assertRaises(NativeError):validate_rule_bindings([binding],[{'bar_index':0}])

    def test_model_live_rule_basis_cannot_be_empty(self):
        with self.assertRaisesRegex(NativeError,'REQUIRED'):validate_rule_bindings([],[],required=True)

    def test_point_figure_and_unknown_records_not_in_main_context(self):
        records=[
          {'record_type':'MAIN_RESEARCH','lineage_scope':'MAIN_ONLY','contains_sidecar_targets':False,'value':1},
          {'record_type':'PNF','lineage_scope':'MAIN_ONLY','contains_sidecar_targets':False,'target':100},
          {'record_type':'MAIN_REVIEW','lineage_scope':'MIXED','contains_sidecar_targets':True},
          {'record_type':'MAIN_RESEARCH'}]
        self.assertEqual(main_context_records(records),[records[0]])

    def test_changing_sidecar_targets_keeps_main_input_equal(self):
        records=[{'record_type':'MAIN_RESEARCH','lineage_scope':'MAIN_ONLY','contains_sidecar_targets':False},
                 {'record_type':'PNF','target':100}]
        before=main_context_records(records);records[1]['target']=999999
        self.assertEqual(before,main_context_records(records))


class ProcessBoundaries(unittest.TestCase):
    def run_command(self,code,timeout=2,**kwargs):
        import sys
        from global_market_intelligence.opportunities.native.engine import run_process
        with tempfile.TemporaryDirectory() as tmp:
            return run_process([sys.executable,'-S','-c',code],cwd=Path(tmp),timeout=timeout,**kwargs)

    def test_returncode_is_preserved(self):
        self.assertEqual(self.run_command('raise SystemExit(3)')['returncode'],3)

    def test_large_stdin_does_not_block_writing_output(self):
        result=self.run_command('import sys; data=sys.stdin.read(); print(len(data));print("ok",file=sys.stderr)',
                                input_text='测'*100000)
        self.assertEqual(result['stdout'].strip(),'100000')
        self.assertEqual(result['stderr'].strip(),'ok')

    def test_timeout_is_bounded(self):
        import time
        t=time.monotonic()
        with self.assertRaisesRegex(NativeError,'TIMEOUT'):
            self.run_command('import time; time.sleep(10)',timeout=.1)
        self.assertLess(time.monotonic()-t,2)

    def test_inherited_output_handle_does_not_block_return(self):
        import time
        t=time.monotonic()
        code='import subprocess,sys; subprocess.Popen([sys.executable,"-S","-c","import time;time.sleep(10)"]);print("parent-done")'
        self.assertIn('parent-done',self.run_command(code)['stdout'])
        self.assertLess(time.monotonic()-t,2)

    def test_output_limit_is_enforced(self):
        with patch('global_market_intelligence.opportunities.native.engine.MAX_PROCESS_OUTPUT',1000):
            with self.assertRaisesRegex(NativeError,'OUTPUT_LIMIT'):
                self.run_command('print("x"*10000)')


if __name__=='__main__':unittest.main()
