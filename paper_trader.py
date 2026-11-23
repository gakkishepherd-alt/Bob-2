"""Paper-only signal runner integrating signals, risk limits and ledger."""
from datetime import datetime, timezone
import math

try:
    from .risk import Limits, allowed_buy_gbp
    from .signal_engine import analyze_prices
except ImportError:
    from risk import Limits, allowed_buy_gbp
    from signal_engine import analyze_prices


class PaperTrader:
    def __init__(self, ledger, limits: Limits = Limits()):
        self.ledger = ledger
        self.limits = limits

    def evaluate(self, market: str, prices, *, daily_pnl_gbp: float = 0.0, timestamp: str | None = None) -> dict:
        if not math.isfinite(daily_pnl_gbp):
            raise ValueError("Daily P&L must be finite")
        state = self.ledger.state(market)
        # Indicators and existing GBP risk helper use floats; conversion happens
        # only at this non-persistent interface boundary. Ledger math stays Decimal.
        signal = analyze_prices(market, prices, position_units=float(state["units"]))
        cash = float(state["cash"])
        exposure = float(state["units"]) * signal.price
        decision = {"timestamp_utc": timestamp or datetime.now(timezone.utc).isoformat(),
                    "market": market, "action": signal.action, "execution": "DISABLED",
                    "signal": signal.as_dict(), "reasons": list(signal.reasons), "transaction": None}
        if signal.action == "BUY":
            capacity = allowed_buy_gbp(cash, exposure, daily_pnl_gbp, self.limits)
            if capacity <= 0:
                decision["reasons"].append("Risk controls prohibit a buy (daily loss, emergency halt, or insufficient fee-inclusive cash).")
                return decision
            transaction = self.ledger.execute(market, "BUY", signal.price, capacity / signal.price,
                                              self.limits.estimated_fee_rate, timestamp=timestamp)
            decision.update(action="BUY", execution="PAPER_SIMULATED", transaction=transaction)
        elif signal.action == "SELL" and state["units"] > 0:
            transaction = self.ledger.execute(market, "SELL", signal.price, state["units"],
                                              self.limits.estimated_fee_rate, timestamp=timestamp)
            decision.update(action="SELL", execution="PAPER_SIMULATED", transaction=transaction)
        return decision
