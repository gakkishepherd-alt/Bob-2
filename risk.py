"""Configurable cash-only allocation and independent trading safeguards."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Limits:
    """Risk settings; fee and minimum-order values should match the venue/market."""

    max_daily_loss_gbp: float = 3.0
    estimated_fee_rate: float = 0.0026
    minimum_order_gbp: float = 0.0
    allocation_fraction: float = 1.0
    emergency_halt: bool = False

    def __post_init__(self):
        if not math.isfinite(self.max_daily_loss_gbp) or self.max_daily_loss_gbp <= 0:
            raise ValueError("Maximum daily loss must be a positive finite amount")
        if not math.isfinite(self.estimated_fee_rate) or self.estimated_fee_rate < 0:
            raise ValueError("Estimated fee rate must be finite and nonnegative")
        if not math.isfinite(self.minimum_order_gbp) or self.minimum_order_gbp < 0:
            raise ValueError("Minimum order must be finite and nonnegative")
        if not math.isfinite(self.allocation_fraction) or not 0 <= self.allocation_fraction <= 1:
            raise ValueError("Allocation fraction must be between zero and one")


def allowed_buy_gbp(
    cash_gbp: float,
    exposure_gbp: float,
    daily_pnl_gbp: float,
    limits: Limits = Limits(),
    reserved_cash_gbp: float = 0.0,
) -> float:
    """Return maximum buy notional, excluding fees, affordable from unreserved cash.

    ``cash_gbp`` must be the current account cash balance. ``reserved_cash_gbp``
    includes cash committed to open orders or otherwise unavailable. Exposure is
    validated for compatibility/portfolio input checking but does not impose a
    fixed exposure ceiling. Fees are conservatively rounded up to pennies.
    """
    values = (cash_gbp, exposure_gbp, daily_pnl_gbp, reserved_cash_gbp)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Balances and P&L must be finite numbers")
    if min(cash_gbp, exposure_gbp, reserved_cash_gbp) < 0:
        raise ValueError("Balances and reservations cannot be negative")
    if reserved_cash_gbp > cash_gbp:
        raise ValueError("Reserved cash cannot exceed account cash")
    if limits.emergency_halt or daily_pnl_gbp <= -limits.max_daily_loss_gbp:
        return 0.0

    available = (cash_gbp - reserved_cash_gbp) * limits.allocation_fraction
    # Start with the maximum fee-inclusive affordable notional, rounded down to
    # pennies. Round estimated fees up to avoid spending beyond available cash.
    order = math.floor((available / (1 + limits.estimated_fee_rate) + 1e-12) * 100) / 100
    while order > 0:
        fee = math.ceil(order * limits.estimated_fee_rate * 100 - 1e-12) / 100
        if order + fee <= available + 1e-10:
            break
        order = round(order - 0.01, 2)
    if order + 1e-10 < limits.minimum_order_gbp:
        return 0.0
    return round(max(0.0, order), 2)
