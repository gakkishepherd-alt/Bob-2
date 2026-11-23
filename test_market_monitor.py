import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from market_monitor import MonitorConfig, ObservationStore, ReadOnlyMarketMonitor

NOW = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
PAIR = 'XXBTZGBP'

class Universe:
    def __init__(self):
        self.fresh = True
        self.allowed = True
        self.fail = False
        self.refresh_count = 0
    def is_fresh(self, *, now=None): return self.fresh
    def eligible_markets(self, *, now=None):
        return (SimpleNamespace(pair_id=PAIR, wsname='XBT/GBP'),) if self.fresh and self.allowed else ()
    def refresh(self):
        self.refresh_count += 1
        if self.fail:
            self.fresh = False
            raise OSError('offline')
        self.fresh = True
        return self.eligible_markets()

class Client:
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.future = False
        self.unsorted = False
        self.old = False
    def ticker(self, pair):
        self.calls.append('ticker')
        if self.fail_once:
            self.fail_once = False
            raise OSError('temporary')
        return {pair: {'b':['100'], 'a':['101'], 'c':['103'], 'v':['100','200']}}
    def ohlc(self, pair, *, interval):
        self.calls.append('ohlc')
        start = int(NOW.timestamp()) - 24 * 300 - (14400 if self.old else 0)
        candles = [[start+i*300, '100','100','100',str(80+i),'100','2',1] for i in range(24)]
        candles.append([start + 24 * 300 + (300 if self.future else 0),'100','100','100','104','100','2',1])
        return {pair: candles, 'last': 0}
    def order_book(self, pair, *, count):
        self.calls.append('book')
        bids = [['99','10'],['100','10']] if self.unsorted else [['100','10'],['99','10']]
        return {pair: {'bids':bids, 'asks':[['101','10'],['102','10']]}}

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ObservationStore(Path(self.tmp.name)/'observations.db')
        self.client, self.universe = Client(), Universe()
        self.cfg = MonitorConfig(spacing_seconds=0, backoff_seconds=0, retries=1, refresh_attempts=1,
                                 max_spread_bps=Decimal('200'))
        self.monitor = ReadOnlyMarketMonitor(self.client,self.universe,self.store,self.cfg,clock=lambda:NOW,sleeper=lambda _:None)
        self.market = SimpleNamespace(pair_id=PAIR)
    def tearDown(self): self.tmp.cleanup()
    def test_valid_completed_only_and_persistence(self):
        r = self.monitor.observe_market(self.market)
        self.assertTrue(r['valid'])
        self.assertEqual(r['signal'],'BUY')
        self.assertEqual(r['completed_candles'],24)
        self.assertTrue(self.store.integrity_check())
        self.assertEqual(self.store.history(PAIR)[0]['last_price'],'103')
    def test_refresh_recovers(self):
        self.universe.fresh=False
        self.assertEqual(len(self.monitor.poll_once()),1)
        self.assertEqual(self.universe.refresh_count,1)
    def test_refresh_failure_fails_closed(self):
        self.universe.fresh=False; self.universe.fail=True
        self.assertEqual(self.monitor.poll_once(),[])
        self.assertEqual(self.client.calls,[])
    def test_direct_unauthorized_fails_closed(self):
        self.universe.allowed=False
        r=self.monitor.observe_market(self.market)
        self.assertEqual(r['signal'],'INVALID')
        self.assertEqual(self.client.calls,[])
    def test_retries_network_failure(self):
        self.client.fail_once=True
        self.assertTrue(self.monitor.observe_market(self.market)['valid'])
        self.assertEqual(self.client.calls.count('ticker'),2)
    def test_future_candle_invalid(self):
        self.client.future=True
        self.assertEqual(self.monitor.observe_market(self.market)['signal'],'INVALID')
    def test_stale_candle_invalid(self):
        self.client.old=True
        self.assertEqual(self.monitor.observe_market(self.market)['signal'],'INVALID')
    def test_unsorted_book_invalid(self):
        self.client.unsorted=True
        self.assertEqual(self.monitor.observe_market(self.market)['signal'],'INVALID')
    def test_report_historical_nonactionable_and_stale(self):
        self.monitor.observe_market(self.market)
        fresh=self.monitor.report()['markets'][0]
        self.assertFalse(fresh['actionable'])
        self.assertEqual(fresh['signal'],'HOLD')
        later=ReadOnlyMarketMonitor(self.client,self.universe,self.store,self.cfg,clock=lambda:NOW+timedelta(minutes=10),sleeper=lambda _:None)
        self.assertEqual(later.report()['markets'][0]['signal'],'INVALID')
    def test_stop_and_invalid_clock(self):
        e=threading.Event();e.set();self.monitor.run(e)
        self.assertEqual(self.client.calls,[])
        bad=ReadOnlyMarketMonitor(self.client,self.universe,self.store,self.cfg,clock=lambda:datetime(2026,1,1))
        with self.assertRaises(ValueError): bad.poll_once()

if __name__=='__main__': unittest.main()
