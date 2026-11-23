"""Read-only Kraken spot discovery with explicitly scoped eligibility metadata.

This module cannot authenticate or establish Kraken-account permissions. The
eligibility record must be supplied by an authorized reviewer from a current,
authoritative account-specific source; it is an application policy input, not
proof that Kraken permits trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
    """Normalize known Kraken asset IDs/aliases and reject ambiguous codes."""
    def __init__(self, assets_payload: Mapping[str, Any]):
        rows = _result(assets_payload, "Assets")
        aliases: dict[str, set[str]] = {}
        self.invalid_ids: set[str] = set()
        for raw_id, raw in rows.items():
            if raw_id == "last":
                continue
            asset_id = raw_id.strip().upper() if isinstance(raw_id, str) else ""
            if not asset_id or not isinstance(raw, Mapping):
                if asset_id:
                    self.invalid_ids.add(asset_id)
                continue
            altname = raw.get("altname")
            if altname is not None and (not isinstance(altname, str) or not altname.strip()):
                self.invalid_ids.add(asset_id)
                continue
            canonical = (altname.strip() if isinstance(altname, str) and altname.strip() else asset_id).upper()
            names = {asset_id, canonical}
            wsname = raw.get("wsname")
            if wsname is not None:
                if not isinstance(wsname, str) or not wsname.strip():
                    self.invalid_ids.add(asset_id)
                    continue
                names.add(wsname.strip().upper())
            for name in names:
                aliases.setdefault(name, set()).add(canonical)
            # Register stripped form only as an alias of a known prefixed ID.
            if len(asset_id) > 1 and asset_id[0] in {"X", "Z"}:
                aliases.setdefault(asset_id[1:], set()).add(canonical)
        self._aliases = aliases

    def normalize(self, code: str) -> str:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Asset identifier is missing or empty")
        normalized = code.strip().upper()
        candidates = self._aliases.get(normalized)
        if not candidates:
            raise ValueError(f"Unknown asset identifier: {normalized}")
        if len(candidates) != 1:
            raise ValueError(f"Ambiguous asset alias: {normalized}")
        canonical = next(iter(candidates))
        if canonical in self.invalid_ids:
            raise ValueError(f"Malformed asset metadata for {normalized}")
        return canonical


@dataclass(frozen=True)
class EligibilityConfirmation:
    """Reviewer-supplied policy assertion; this class does not verify Kraken rights.

    ``authoritative_source`` should identify a current Kraken account-specific
    eligibility source or compliance determination. ``account_scope`` should
    uniquely identify the relevant account without storing API credentials.
    """
    pair_ids: frozenset[str]
    confirmed_at: datetime
    valid_for: timedelta
    reviewer: str
    authoritative_source: str
    account_scope: str

    def __post_init__(self):
        if not isinstance(self.pair_ids, (set, frozenset)):
            raise ValueError("Eligibility pair_ids must be a set")
        normalized = set()
        for item in self.pair_ids:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("Eligibility pair identifiers must be non-empty strings")
            normalized.add(item.strip())
        object.__setattr__(self, "pair_ids", frozenset(normalized))
        object.__setattr__(self, "confirmed_at", _aware_utc(self.confirmed_at, "confirmed_at"))
        if not isinstance(self.valid_for, timedelta) or self.valid_for.total_seconds() <= 0:
            raise ValueError("Eligibility validity must be positive")
        for field in ("reviewer", "authoritative_source", "account_scope"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Eligibility {field} must be identified")

    def is_current(self, now: datetime) -> bool:
        age = _aware_utc(now, "now") - self.confirmed_at
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
    """Parse available pair metadata; missing/unverified items fail closed."""
    current = _aware_utc(now or datetime.now(timezone.utc), "now")
    aliases = AssetAliases(assets_payload)
    pairs = _result(pairs_payload, "AssetPairs")
    confirmation_current = eligibility is not None and eligibility.is_current(current)
    markets: list[SpotMarket] = []
    for key, raw in pairs.items():
        if key == "last" or not isinstance(raw, Mapping):
            continue
        pair_id = str(key)
        alt_value, ws_value = raw.get("altname"), raw.get("wsname")
        altname = alt_value.strip() if isinstance(alt_value, str) and alt_value.strip() else pair_id
        base_raw, quote_raw = raw.get("base"), raw.get("quote")
        reasons: list[str] = []
        try:
            base = aliases.normalize(base_raw)
        except ValueError as exc:
            base = ""
            reasons.append(f"invalid base asset: {exc}")
        try:
            quote = aliases.normalize(quote_raw)
        except ValueError as exc:
            quote = ""
            reasons.append(f"invalid quote asset: {exc}")
        wsname = ws_value.strip() if isinstance(ws_value, str) and ws_value.strip() else f"{base}/{quote}"
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
            pair_decimals, lot_decimals = raw["pair_decimals"], raw["lot_decimals"]
            if isinstance(pair_decimals, bool) or isinstance(lot_decimals, bool):
                raise ValueError("boolean decimal precision")
            pair_decimals, lot_decimals = int(pair_decimals), int(lot_decimals)
            if not (0 <= pair_decimals <= 28 and 0 <= lot_decimals <= 28):
                raise ValueError("decimal precision out of range")
            price_increment = _decimal(raw.get("tick_size", Decimal(1).scaleb(-pair_decimals)), "price increment", positive=True)
            quantity_increment = Decimal(1).scaleb(-lot_decimals)
            if not quantity_increment.is_finite() or quantity_increment <= 0:
                raise ValueError("invalid quantity increment")
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
        self.client, self.eligibility, self.max_age = client, eligibility, max_age
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.markets: tuple[SpotMarket, ...] = ()
        self.refreshed_at: datetime | None = None
        self.last_error: str | None = None

    def refresh(self) -> tuple[SpotMarket, ...]:
        self.markets, self.refreshed_at = (), None
        try:
            now = _aware_utc(self._clock(), "clock")
            markets = discover_markets(self.client.assets(), self.client.asset_pairs(), eligibility=self.eligibility, now=now)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self.markets, self.refreshed_at, self.last_error = markets, now, None
        return markets

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        if self.refreshed_at is None:
            return False
        age = _aware_utc(now or self._clock(), "now") - self.refreshed_at
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
