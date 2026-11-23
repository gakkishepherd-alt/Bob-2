import os
import tempfile
import unittest

from paper_ledger import PaperLedger
from paper_trader import PaperTrader
from risk import Limits
from signal_engine import analyze_prices, kraken_ohlc_closes


class SignalEngineTests(unittest.TestCase):
    def test_bullish_and_bearish_explainable_signals(self):
        bullish = analyze_prices("BTC/GBP", list(range(1, 25)))
        bearish = analyze_prices("BTC/GBP", list(range(25, 1, -1)), position_units=1)
        self.assertEqual(bullish.action, "BUY")
        self.assertEqual(bearish.action, "SELL")
        self.assertGreaterEqual(len(bullish.reasons), 2)
        self.assertIn("momentum", " ".join(bullish.reasons).lower())

    def test_bearish_signal_without_holding_is_hold(self):
        self.assertEqual(analyze_prices("BTC/GBP", list(range(25, 1, -1))).action, "HOLD")

    def test_invalid_or_short_series_rejected(self):
        for values in ([], [1, 2], [1, 0] + list(range(3, 24))):
            with self.assertRaises(ValueError):
                analyze_prices("BTC/GBP", values)

    def test_parses_kraken_ohlc_close_column(self):
        payload = {"result": {"XXBTZGBP": [[1, "10", "11", "9", "10.5"], [2, "11", "12", "10", "11.5"]], "last": 2}}
        self.assertEqual(kraken_ohlc_closes(payload), [10.5, 11.5])


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "paper.sqlite3")
        self.ledger = PaperLedger(self.path, 100)

    def tearDown(self):
        self.temp.cleanup()

    def test_simulated_buy_sell_fee_and_persistent_state(self):
        buy = self.ledger.execute("BTC/GBP", "BUY", 10, 5, 0.01, timestamp="2026-01-01T10:00:00+00:00")
        self.assertEqual((buy["execution"], buy["fee"]), ("PAPER_SIMULATED", 0.5))
        self.assertAlmostEqual(self.ledger.state("BTC/GBP")["cash"], 49.5)
        self.ledger.execute("BTC/GBP", "SELL", 12, 5, 0.01, timestamp="2026-01-01T11:00:00+00:00")
        report = PaperLedger(self.path, 100).daily_report("2026-01-01", market="BTC/GBP")
        self.assertEqual(report["fills"], 2)
        self.assertEqual(report["fees"], 1.1)
        self.assertAlmostEqual(report["realized_pnl_after_fees"], 8.4)
        self.assertAlmostEqual(report["sell_notional"], 60)

    def test_no_overselling_or_overspending_and_initial_balance_is_fixed(self):
        with self.assertRaises(ValueError):
            self.ledger.execute("BTC/GBP", "BUY", 10, 11, 0.01)
        with self.assertRaises(ValueError):
            self.ledger.execute("BTC/GBP", "SELL", 10, 1, 0.01)
        with self.assertRaises(ValueError):
            PaperLedger(self.path, 101)

    def test_daily_report_mark_to_market_and_unrealized(self):
        self.ledger.execute("BTC/GBP", "BUY", 10, 2, 0, timestamp="2026-01-01T10:00:00+00:00")
        report = self.ledger.daily_report("2026-01-01", market="BTC/GBP", mark_price=12)
        self.assertEqual(report["position_units"], 2)
        self.assertEqual(report["unrealized_pnl"], 4)
        self.assertEqual(report["equity"], 104)

    def test_trader_respects_daily_loss_and_emergency_halt(self):
        rising = list(range(1, 25))
        halted = PaperTrader(self.ledger, Limits(emergency_halt=True)).evaluate("BTC/GBP", rising)
        self.assertIsNone(halted["transaction"])
        self.assertEqual(halted["action"], "BUY")
        stopped = PaperTrader(self.ledger, Limits(max_daily_loss_gbp=3)).evaluate("BTC/GBP", rising, daily_pnl_gbp=-3)
        self.assertIsNone(stopped["transaction"])


if __name__ == "__main__":
    unittest.main()
