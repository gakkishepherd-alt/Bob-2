import unittest

try:
    from bob2.manager import evaluate
    from bob2.risk import Limits, allowed_buy_gbp
except ModuleNotFoundError:
    from manager import evaluate
    from risk import Limits, allowed_buy_gbp


class ManagerTests(unittest.TestCase):
    def test_default_hold_and_execution_disabled(self):
        decision = evaluate("BTC/GBP", 50000, 100, 0)
        self.assertEqual((decision.action, decision.order_gbp, decision.execution), ("HOLD", 0, "DISABLED"))

    def test_full_unreserved_capital_is_available_including_fee_budget(self):
        limits = Limits(estimated_fee_rate=0.01)
        order = allowed_buy_gbp(100, 0, 0, limits)
        fee = __import__("math").ceil(order * limits.estimated_fee_rate * 100 - 1e-12) / 100
        self.assertGreater(order, 98)
        self.assertLessEqual(order + fee, 100)
        self.assertEqual(allowed_buy_gbp(100, 0, 0, Limits(estimated_fee_rate=0)), 100)

    def test_open_orders_and_reservations_reduce_available_cash(self):
        self.assertEqual(allowed_buy_gbp(100, 40, 0, Limits(estimated_fee_rate=0), reserved_cash_gbp=30), 70)

    def test_insufficient_unreserved_funds_and_minimum_order(self):
        limits = Limits(estimated_fee_rate=0.01, minimum_order_gbp=1)
        self.assertEqual(allowed_buy_gbp(10, 0, 0, limits, reserved_cash_gbp=9.50), 0)
        self.assertEqual(allowed_buy_gbp(10, 0, 0, limits, reserved_cash_gbp=10), 0)

    def test_daily_loss_stop_is_preserved(self):
        self.assertEqual(allowed_buy_gbp(100, 0, -3), 0)

    def test_emergency_halt_is_independent(self):
        self.assertEqual(allowed_buy_gbp(100, 0, 0, Limits(emergency_halt=True)), 0)

    def test_invalid_price(self):
        with self.assertRaises(ValueError):
            evaluate("BTC/GBP", 0, 100, 0)

    def test_reserved_cash_cannot_exceed_cash(self):
        with self.assertRaises(ValueError):
            allowed_buy_gbp(10, 0, 0, reserved_cash_gbp=11)

    def test_no_fixed_initial_budget_cap(self):
        # Realised profits may increase cash above the original seed budget.
        decision = evaluate("BTC/GBP", 50000, 150, 0)
        self.assertEqual(decision.execution, "DISABLED")


if __name__ == "__main__":
    unittest.main()
