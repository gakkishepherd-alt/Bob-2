"""Explainable decision manager: HOLD until a validated signal exists."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from .risk import Limits, allowed_buy_gbp

@dataclass(frozen=True)
class Decision:
    timestamp_utc: str
    market: str
    action: str
    order_gbp: float
    reason: str
    execution: str = "DISABLED"

    def as_dict(self):
        return asdict(self)

def evaluate(market: str, price_gbp: float, cash_gbp: float, exposure_gbp: float, daily_pnl_gbp: float = 0.0, limits: Limits = Limits()) -> Decision:
    if price_gbp <= 0 or cash_gbp < 0 or exposure_gbp < 0:
        raise ValueError("Price must be positive and balances nonnegative")
    if cash_gbp + exposure_gbp > limits.initial_budget_gbp + 1e-8:
        raise ValueError("Total marked value exceeds configured budget; reconcile before trading")
    capacity = allowed_buy_gbp(cash_gbp, exposure_gbp, daily_pnl_gbp, limits)
    reason = ("Daily loss limit reached; no new buys" if daily_pnl_gbp <= -limits.max_daily_loss_gbp else "No validated trading signal; remain in cash" if capacity > 0 else "Risk limits prohibit additional buys")
    return Decision(datetime.now(timezone.utc).isoformat(), market, "HOLD", 0.0, reason)
