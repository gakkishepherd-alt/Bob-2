"""Explainable, deterministic paper-trading signals from close-price data.

Signals are research suggestions only. This module has no exchange or order
execution dependency.
"""
from dataclasses import asdict, dataclass
import math
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Signal:
    market: str
    action: str
    price: float
    fast_sma: float
    slow_sma: float
    momentum_pct: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        return result


def _validated_prices(prices: Iterable[float]) -> list[float]:
    values = [float(value) for value in prices]
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("Prices must be a non-empty sequence of positive finite numbers")
    return values


def analyze_prices(
    market: str,
    prices: Iterable[float],
    *,
    fast_window: int = 5,
    slow_window: int = 20,
    momentum_window: int = 3,
    momentum_threshold_pct: float = 0.25,
    position_units: float = 0.0,
) -> Signal:
    """Return a BUY/SELL/HOLD recommendation with its indicator rationale.

    BUY requires a fast-SMA-over-slow-SMA trend and positive recent momentum.
    SELL requires the inverse trend and negative momentum, and is suppressed
    when there is no position to sell. All other states are HOLD. Windows use
    closes in oldest-to-newest order; these intentionally simple indicators
    are transparent, not predictive guarantees.
    """
    closes = _validated_prices(prices)
    if not market or not isinstance(market, str):
        raise ValueError("Market must be a non-empty string")
    if fast_window < 1 or slow_window < 2 or fast_window >= slow_window:
        raise ValueError("Require 1 <= fast_window < slow_window")
    if momentum_window < 1 or len(closes) < max(slow_window, momentum_window + 1):
        raise ValueError("Not enough closing prices for the requested indicators")
    if not math.isfinite(momentum_threshold_pct) or momentum_threshold_pct < 0:
        raise ValueError("Momentum threshold must be finite and nonnegative")
    if not math.isfinite(position_units) or position_units < 0:
        raise ValueError("Position must be finite and nonnegative")

    fast = sum(closes[-fast_window:]) / fast_window
    slow = sum(closes[-slow_window:]) / slow_window
    momentum = (closes[-1] / closes[-1 - momentum_window] - 1.0) * 100.0
    trend = "above" if fast > slow else "below" if fast < slow else "equal to"
    reasons = [
        f"Fast {fast_window}-close SMA ({fast:.8g}) is {trend} slow {slow_window}-close SMA ({slow:.8g}).",
        f"{momentum_window}-interval momentum is {momentum:.4f}% (BUY/SELL threshold: ±{momentum_threshold_pct:.4f}%).",
    ]
    if fast > slow and momentum >= momentum_threshold_pct:
        action = "BUY"
        reasons.append("Uptrend and positive momentum agree; BUY is eligible subject to risk controls.")
    elif fast < slow and momentum <= -momentum_threshold_pct:
        if position_units > 0:
            action = "SELL"
            reasons.append("Downtrend and negative momentum agree; SELL is eligible for the held position.")
        else:
            action = "HOLD"
            reasons.append("Bearish conditions exist, but there is no paper position to sell.")
    else:
        action = "HOLD"
        reasons.append("Trend and momentum do not jointly meet a directional rule.")
    return Signal(market, action, closes[-1], fast, slow, momentum, tuple(reasons))


def kraken_ohlc_closes(payload: Mapping[str, object], pair: str | None = None) -> list[float]:
    """Extract close prices from Kraken's public OHLC result payload.

    Kraken includes a ``last`` cursor alongside one pair's candle array; that
    cursor is ignored. The function only parses supplied data and makes no
    network request.
    """
    result = payload.get("result") if isinstance(payload, Mapping) and "result" in payload else payload
    if not isinstance(result, Mapping):
        raise ValueError("OHLC payload must contain a result mapping")
    keys = [key for key, value in result.items() if key != "last" and isinstance(value, Sequence) and not isinstance(value, (str, bytes))]
    if pair is not None:
        keys = [key for key in keys if key == pair]
    if len(keys) != 1:
        raise ValueError("OHLC payload must identify exactly one candle series")
    candles = result[keys[0]]
    closes = []
    for candle in candles:
        if not isinstance(candle, Sequence) or isinstance(candle, (str, bytes)) or len(candle) < 5:
            raise ValueError("Malformed Kraken OHLC candle")
        closes.append(float(candle[4]))
    return _validated_prices(closes)
