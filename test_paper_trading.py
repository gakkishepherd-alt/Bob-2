import unittest
from decimal import Decimal

try:
    from bob2.paper_ledger import PaperLedger
    from bob2.paper_trader import PaperTrader
    from bob2.risk import Limits, allowed_buy_gbp
except ModuleNotFoundError:
    from paper_ledger import PaperLedger
    from paper_trader import PaperTrader
    from risk import Limits, allowed_buy_gbp


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/ledger.sqlite3"
        self.ledger = PaperLedger(self.path, 100)

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_fee_and_realized_report(self):
        buy = self.ledger.execute("BTC/GBP", "BUY", 10, 5, 0.01, timestamp="2026-01-01T10:00:00+00:00")
        sell = self.ledger.execute("BTC/GBP", "SELL", 12, 5, 0.01, timestamp="2026-01-01T11:00:00+00:00")
        report = self.ledger.daily_report("2026-01-01", market="BTC/GBP")
        self.assertEqual((buy["fee"], sell["fee"]), (Decimal("0.50"), Decimal("0.60")))
        self.assertEqual(report["fills"], 2)
        self.assertEqual(report["fees"], Decimal("1.10"))
        # P&L is gross price gain 10 less both fees, each exactly once.
        self.assertEqual(report["realized_pnl_after_fees"], Decimal("8.90"))
        self.assertEqual(report["sell_notional"], Decimal("60"))
        self.assertEqual(self.ledger.balances()["GBP"], Decimal("108.90"))
        self.assertEqual(self.ledger.state("BTC/GBP")["units"], Decimal("0"))

    def test_daily_report_mark_to_market_and_unrealized(self):
        self.ledger.execute("BTC/GBP", "BUY", 10, 2, 0, timestamp="2026-01-01T10:00:00+00:00")
        report = self.ledger.daily_report("2026-01-01", market="BTC/GBP", mark_price=12)
        self.assertEqual(report["position_units"], Decimal("2"))
        self.assertEqual(report["unrealized_pnl"], Decimal("4"))
        self.assertEqual(report["equity"], Decimal("104"))

    def test_risk_helpers_and_paper_trader(self):
        self.assertGreater(allowed_buy_gbp(100, 0, 0, Limits()), 0)
        rising = list(range(1,25))
        result = PaperTrader(self.ledger, Limits(emergency_halt=True)).evaluate("BTC/GBP", rising)
        self.assertIsNone(result["transaction"])
        self.assertEqual(result["action"], "BUY")


if __name__ == "__main__":
    unittest.main()
