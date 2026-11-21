import unittest
from bob2.manager import evaluate
from bob2.risk import allowed_buy_gbp

class ManagerTests(unittest.TestCase):
    def test_default_hold(self):
        d = evaluate("BTC/GBP", 50000, 100, 0)
        self.assertEqual((d.action, d.order_gbp, d.execution), ("HOLD", 0, "DISABLED"))

    def test_max_order(self):
        self.assertEqual(allowed_buy_gbp(100, 0, 0), 10)

    def test_daily_loss_stop(self):
        self.assertEqual(allowed_buy_gbp(100, 0, -3), 0)

    def test_exposure_limit(self):
        self.assertEqual(allowed_buy_gbp(80, 25, 0), 0)

    def test_invalid_price(self):
        with self.assertRaises(ValueError):
            evaluate("BTC/GBP", 0, 100, 0)

    def test_budget_reconciliation(self):
        with self.assertRaises(ValueError):
            evaluate("BTC/GBP", 50000, 100, 10)

if __name__ == "__main__":
    unittest.main()
