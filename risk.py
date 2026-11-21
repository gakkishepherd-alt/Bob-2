"""Conservative cash-only risk limits, in GBP."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Limits:
    initial_budget_gbp: float = 100.0
    max_order_gbp: float = 10.0
    max_exposure_gbp: float = 25.0
    min_cash_reserve_gbp: float = 75.0
    max_daily_loss_gbp: float = 3.0

def allowed_buy_gbp(cash_gbp: float, exposure_gbp: float, daily_pnl_gbp: float, limits: Limits = Limits()) -> float:
    if min(cash_gbp, exposure_gbp) < 0:
        raise ValueError("Balances cannot be negative")
    if daily_pnl_gbp <= -limits.max_daily_loss_gbp:
        return 0.0
    return round(max(0.0, min(limits.max_order_gbp, limits.max_exposure_gbp - exposure_gbp, cash_gbp - limits.min_cash_reserve_gbp)), 2)
