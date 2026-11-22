"""Explainable decision manager: HOLD until a validated signal exists."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math

try:  # Support both package execution and the repository's root-level tests.
    from .risk import Limits, allowed_buy_gbp
except ImportError:  # pragma: no cover - used when imported as a root module
    from risk import Limits, allowed_buy_gbp


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


def evaluate(
    market: str,
    price_gbp: float,
    cash_gbp: float,
    exposure_gbp: float,
    daily_pnl_gbp: float = 0.0,
    limits: Limits = Limits(),
    reserved_cash_gbp: float = 0.0,
) -> Decision:
    if not all(math.isfinite(value) for value in (price_gbp, cash_gbp, exposure_gbp)):
        raise ValueError("Price and balances must be finite numbers")
    if price_gbp <= 0 or cash_gbp < 0 or exposure_gbp < 0:
        raise ValueError("Price must be positive and balances nonnegative")
    capacity = allowed_buy_gbp(
        cash_gbp, exposure_gbp, daily_pnl_gbp, limits, reserved_cash_gbp
    )
    if limits.emergency_halt:
        reason = "Emergency trading halt is active"
    elif daily_pnl_gbp <= -limits.max_daily_loss_gbp:
        reason = "Daily loss limit reached; no new buys"
    elif capacity > 0:
        reason = "No validated trading signal; remain in cash"
    else:
        reason = "No unreserved cash available for a fee-inclusive minimum order"
    # This project has no exchange connection or order placement implementation.
    return Decision(datetime.now(timezone.utc).isoformat(), market, "HOLD", 0.0, reason)
