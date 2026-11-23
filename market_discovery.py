"""Read-only Kraken spot-market discovery with fail-closed account eligibility.

Public market metadata is not evidence that a specific UK retail account may
trade a product. A current, explicitly reviewed eligibility confirmation is
required before a market is returned by ``eligible_markets``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Mapping


def _decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{label} must be {qualifier}")
    return result


def _aware_utc(value: str | datetime, label: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be timezone-aware ISO-8601") from exc
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _result(payload: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} response must be a mapping")
    data = payload.get("result", payload)
    if not isinstance(data, Mapping):
        raise ValueError(f"{label} result must be a mapping")
    return data


class AssetAliases:
    """Maps Kraken asset IDs, alt names, and ws names to canonical symbols."""
    def __init__(self, assets_payload: Mapping[str, Any]):
        rows = _result(assets_payload, "Assets")
        aliases: dict[str, str] = {}
        for asset_id, raw in rows.items():
            if asset_id == "last" or not isinstance(raw, Mapping):
                continue
            canonical = str(raw.get("altname") or asset_id).upper()
            names = {str(asset_id).upper(), canonical}
            for field in ("wsname", "aclass", "altname"):
                value = raw.get(field)
                if isinstance(value, str):
                    names.add(value.upper())
            for name in names:
                aliases[name] = canonical
            # Only register stripped X/Z forms as aliases when the full code
            # is an actual Assets key; never blindly strip user-provided codes.
            if len(str(asset_id)) > 1 and str(asset_id)[0].upper() in {"X", "Z"}:
                aliases.setdefault(str(asset_id)[1:].upper(), canonical)
        self._aliases = aliases

    def normalize(self, code: str) -> str:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Asset code must be a non-empty string")
        normalized = code.strip().upper()
        return self._aliases.get(normalized, normalized)


@dataclass(frozen=True)
class EligibilityConfirmation:
    """Explicit UK-account market approval with an expiration window."""
    pair_ids: frozenset[str]
    confirmed_at: datetime
    valid_for: timedelta
    reviewer: str

    def __post_init__(self):
        if not isinstance(self.pair_ids, (set, frozenset)):
            raise ValueError("Eligibility pair_ids must be a set")
        object.__setattr__(self, "pair_ids", frozenset(str(item) for item in self.pair_ids))
        object.__setattr__(self, "confirmed_at", _aware_utc(self.confirmed_at, "confirmed_at"))
        if not isinstance(self.valid_for, timedelta) or self.valid_for.total_seconds() <= 0:
            raise ValueError("Eligibility validity must be positive")
        if not isinstance(self.reviewer, str) or not self.reviewer.strip():
            raise ValueError("Eligibility reviewer must be identified")

    def is_current(self, now: datetime) -> bool:
        current = _aware_utc(now, "now")
        age = current - self.confirmed_at
        return timedelta(0) <= age <= self.valid_for

    def confirms(self, *identifiers: str) -> bool:
        return any(identifier in self.pair_ids for identifier in identifiers)


@dataclass(frozen=True)
class SpotMarket:
    pair_id: str
    altname: str
    wsname: str
    base: str
    quote: str
    status: str
    order_minimum: Decimal
    cost_minimum: Decimal
    price_increment: Decimal
    quantity_increment: Decimal
    eligible_for_uk_account: bool
    exclusion_reasons: tuple[str, ...]


def discover_markets(assets_payload: Mapping[str, Any], pairs_payload: Mapping[str, Any], *,
                     eligibility: EligibilityConfirmation | None,
                     now: datetime | None = None) -> tuple[SpotMarket, ...]:
    """Parse Kraken Assets and AssetPairs; preserve excluded markets with reasons.

    Only explicitly confirmed pairs are eligible. Missing/expired confirmation
    leaves all products ineligible. Non-spot, non-online, margin-only and
    derivative metadata is rejected. Missing or invalid trading increments and
    minimums are exclusion reasons, not defaults.
    """
    current = _aware_utc(now or datetime.now(timezone.utc), "now")
    asset_aliases = AssetAliases(assets_payload)
    rows = _result(pairs_payload, "AssetPairs")
    confirmation_current = eligibility is not None and eligibility.is_current(current)
    markets: list[SpotMarket] = []
    for key, raw in rows.items():
        if key == "last" or not isinstance(raw, Mapping):
            continue
        pair_id = str(key)
        altname = str(raw.get("altname") or pair_id)
        base_raw, quote_raw = raw.get("base"), raw.get("quote")
        reasons: list[str] = []
        try:
            base = asset_aliases.normalize(str(base_raw))
            quote = asset_aliases.normalize(str(quote_raw))
        except ValueError:
            base, quote = str(base_raw or ""), str(quote_raw or "")
            reasons.append("missing or invalid base/quote asset")
        wsname = str(raw.get("wsname") or f"{base}/{quote}")
        status = str(raw.get("status") or "unknown").lower()
        if status != "online":
            reasons.append(f"market status is {status}")

        market_type = str(raw.get("type", "spot")).lower()
        pair_lower = (pair_id + " " + altname + " " + wsname).lower()
        if market_type not in {"spot", "cash"}:
            reasons.append(f"product type {market_type} is not spot")
        if any(tag in pair_lower for tag in (".f", "perp", "future", ".d")):
            reasons.append("derivative or dark-pool product")
        if raw.get("margin_only") is True or raw.get("spot_tradable") is False:
            reasons.append("market is margin-only or not spot-tradable")

        try:
            order_minimum = _decimal(raw.get("ordermin"), "order minimum", positive=True)
        except ValueError:
            order_minimum = Decimal(0)
            reasons.append("missing or invalid positive order minimum")
        try:
            cost_minimum = _decimal(raw.get("costmin"), "cost minimum", positive=True)
        except ValueError:
            cost_minimum = Decimal(0)
            reasons.append("missing or invalid positive cost minimum")
        try:
            pair_decimals = int(raw["pair_decimals"])
            lot_decimals = int(raw["lot_decimals"])
            if isinstance(raw["pair_decimals"], bool) or isinstance(raw["lot_decimals"], bool):
                raise ValueError
            if not (0 <= pair_decimals <= 28 and 0 <= lot_decimals <= 28):
                raise ValueError
            price_increment = _decimal(raw.get("tick_size", Decimal(1).scaleb(-pair_decimals)), "price increment", positive=True)
            quantity_increment = Decimal(1).scaleb(-lot_decimals)
            if not quantity_increment.is_finite() or quantity_increment <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, InvalidOperation):
            price_increment = quantity_increment = Decimal(0)
            reasons.append("missing or invalid price/quantity increment metadata")

        identifiers = (pair_id, altname, wsname, f"{base}/{quote}")
        if not confirmation_current:
            reasons.append("current UK account-specific eligibility confirmation is missing or expired")
        elif not eligibility.confirms(*identifiers):
            reasons.append("pair is not explicitly confirmed eligible for this UK account")
        markets.append(SpotMarket(pair_id, altname, wsname, base, quote, status,
                                  order_minimum, cost_minimum, price_increment, quantity_increment,
                                  not reasons, tuple(reasons)))
    return tuple(sorted(markets, key=lambda item: (item.wsname, item.pair_id)))


class MarketUniverse:
    """Read-only refreshed market snapshot that expires and fails closed."""
    def __init__(self, client, *, eligibility: EligibilityConfirmation | None,
                 max_age: timedelta = timedelta(minutes=5), clock=None):
        if not isinstance(max_age, timedelta) or max_age.total_seconds() <= 0:
            raise ValueError("max_age must be a positive timedelta")
        self.client = client
        self.eligibility = eligibility
        self.max_age = max_age
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.markets: tuple[SpotMarket, ...] = ()
        self.refreshed_at: datetime | None = None
        self.last_error: str | None = None

    def refresh(self) -> tuple[SpotMarket, ...]:
        # Clear eligibility before I/O. Any failed refresh therefore cannot leave
        # old listings eligible while exchange state or account policy is stale.
        self.markets = ()
        self.refreshed_at = None
        try:
            now = _aware_utc(self._clock(), "clock")
            markets = discover_markets(self.client.assets(), self.client.asset_pairs(),
                                       eligibility=self.eligibility, now=now)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self.markets = markets
        self.refreshed_at = now
        self.last_error = None
        return markets

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        if self.refreshed_at is None:
            return False
        current = _aware_utc(now or self._clock(), "now")
        age = current - self.refreshed_at
        return timedelta(0) <= age <= self.max_age

    def eligible_markets(self, *, now: datetime | None = None) -> tuple[SpotMarket, ...]:
        current = _aware_utc(now or self._clock(), "now")
        if not self.is_fresh(now=current) or self.eligibility is None or not self.eligibility.is_current(current):
            return ()
        return tuple(m for m in self.markets if m.eligible_for_uk_account)

    def excluded_markets(self, *, now: datetime | None = None) -> tuple[SpotMarket, ...]:
        if not self.is_fresh(now=now):
            return ()
        return tuple(m for m in self.markets if not m.eligible_for_uk_account)
