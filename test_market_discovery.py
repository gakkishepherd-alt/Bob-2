import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from market_discovery import AssetAliases, EligibilityConfirmation, MarketUniverse, discover_markets


NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
ASSETS = {
    "XXBT": {"altname": "XBT", "wsname": "XBT"},
    "XETH": {"altname": "ETH", "wsname": "ETH"},
    "ZGBP": {"altname": "GBP", "wsname": "GBP"},
    "ZEUR": {"altname": "EUR", "wsname": "EUR"},
    "USD": {"altname": "USD", "wsname": "USD"},
}
PAIRS = {
    "XXBTZGBP": {"altname": "XBTGBP", "wsname": "XBT/GBP", "base": "XXBT", "quote": "ZGBP", "status": "online", "ordermin": "0.0001", "costmin": "0.50", "pair_decimals": 1, "lot_decimals": 8},
    "XETHZEUR": {"altname": "ETHEUR", "wsname": "ETH/EUR", "base": "XETH", "quote": "ZEUR", "status": "online", "ordermin": "0.01", "costmin": "1", "pair_decimals": 2, "lot_decimals": 5},
    "SUSP": {"altname": "BTCEUR-S", "wsname": "XBT/EUR", "base": "XXBT", "quote": "ZEUR", "status": "cancel_only", "ordermin": "0.0001", "costmin": "1", "pair_decimals": 1, "lot_decimals": 8},
    "XBTUSD.F": {"altname": "XBTUSD.F", "base": "XXBT", "quote": "USD", "status": "online", "type": "future", "ordermin": "0.001", "costmin": "1", "pair_decimals": 1, "lot_decimals": 6},
    "BADMIN": {"altname": "BADGBP", "base": "XXBT", "quote": "ZGBP", "status": "online", "ordermin": "NaN", "costmin": "0", "pair_decimals": 1, "lot_decimals": 8},
}


def confirmation(pairs=("XXBTZGBP", "XBTGBP", "XBT/GBP", "XBT/GBP", "XETHZEUR", "ETHEUR", "ETH/EUR"), at=NOW):
    return EligibilityConfirmation(frozenset(pairs), at, timedelta(hours=1), "compliance-review-123")


class AssetAliasTests(unittest.TestCase):
    def test_asset_key_altname_and_prefix_aliases(self):
        aliases = AssetAliases(ASSETS)
        self.assertEqual(aliases.normalize("XXBT"), "XBT")
        self.assertEqual(aliases.normalize("XBT"), "XBT")
        self.assertEqual(aliases.normalize("ZGBP"), "GBP")
        self.assertEqual(aliases.normalize("GBP"), "GBP")
        self.assertEqual(aliases.normalize("XETH"), "ETH")
        self.assertEqual(aliases.normalize("UNKNOWN"), "UNKNOWN")


class MarketDiscoveryTests(unittest.TestCase):
    def test_dynamic_discovery_normalizes_symbols_and_decimal_market_rules(self):
        found = discover_markets(ASSETS, PAIRS, eligibility=confirmation(), now=NOW)
        by_id = {market.pair_id: market for market in found}
        btc = by_id["XXBTZGBP"]
        self.assertEqual((btc.base, btc.quote), ("XBT", "GBP"))
        self.assertEqual(btc.order_minimum, Decimal("0.0001"))
        self.assertEqual(btc.cost_minimum, Decimal("0.50"))
        self.assertEqual(btc.price_increment, Decimal("0.1"))
        self.assertEqual(btc.quantity_increment, Decimal("1E-8"))
        self.assertTrue(btc.eligible_for_uk_account)

    def test_missing_expired_or_nonmatching_account_approval_fails_closed(self):
        for approved in (None, confirmation(at=NOW - timedelta(hours=2)), confirmation(pairs=(), at=NOW)):
            markets = discover_markets(ASSETS, PAIRS, eligibility=approved, now=NOW)
            self.assertFalse(any(m.eligible_for_uk_account for m in markets))
            self.assertTrue(any("eligibility confirmation" in " ".join(m.exclusion_reasons) or
                                "not explicitly confirmed" in " ".join(m.exclusion_reasons) for m in markets))

    def test_suspended_derivative_margin_and_invalid_minimums_excluded_with_reasons(self):
        altered = dict(PAIRS)
        altered["MARGIN"] = {**PAIRS["XXBTZGBP"], "altname": "XBTGBP-M", "margin_only": True}
        markets = discover_markets(ASSETS, altered, eligibility=confirmation(pairs=("XXBTZGBP", "XBTGBP", "XBT/GBP", "MARGIN", "XBTGBP-M")), now=NOW)
        by_id = {market.pair_id: market for market in markets}
        self.assertFalse(by_id["SUSP"].eligible_for_uk_account)
        self.assertTrue(any("status" in reason for reason in by_id["SUSP"].exclusion_reasons))
        self.assertFalse(by_id["XBTUSD.F"].eligible_for_uk_account)
        self.assertTrue(any("derivative" in reason or "not spot" in reason for reason in by_id["XBTUSD.F"].exclusion_reasons))
        self.assertFalse(by_id["MARGIN"].eligible_for_uk_account)
        self.assertFalse(by_id["BADMIN"].eligible_for_uk_account)
        self.assertTrue(any("minimum" in reason for reason in by_id["BADMIN"].exclusion_reasons))

    def test_bad_decimal_and_increment_metadata_are_not_tradable(self):
        bad = {"PAIR": {"base": "XXBT", "quote": "ZGBP", "status": "online", "ordermin": "Infinity", "costmin": "1", "pair_decimals": -1, "lot_decimals": 8}}
        result = discover_markets(ASSETS, bad, eligibility=confirmation(pairs=("PAIR", "XBT/GBP")), now=NOW)[0]
        self.assertFalse(result.eligible_for_uk_account)
        self.assertTrue(any("minimum" in r for r in result.exclusion_reasons))
        self.assertTrue(any("increment" in r for r in result.exclusion_reasons))

    def test_invalid_confirmation_and_response_shapes_rejected(self):
        with self.assertRaises(ValueError):
            EligibilityConfirmation(frozenset({"XBTGBP"}), NOW.replace(tzinfo=None), timedelta(hours=1), "review")
        with self.assertRaises(ValueError):
            discover_markets([], PAIRS, eligibility=confirmation(), now=NOW)


class FakeClient:
    def __init__(self):
        self.fail_pairs = False
    def assets(self):
        return ASSETS
    def asset_pairs(self):
        if self.fail_pairs:
            raise RuntimeError("network unavailable")
        return PAIRS


class MarketUniverseTests(unittest.TestCase):
    def test_refresh_and_snapshot_expiration_fail_closed(self):
        clock = [NOW]
        universe = MarketUniverse(FakeClient(), eligibility=confirmation(), max_age=timedelta(minutes=5), clock=lambda: clock[0])
        universe.refresh()
        self.assertTrue(universe.eligible_markets())
        clock[0] += timedelta(minutes=6)
        self.assertEqual(universe.eligible_markets(), ())

    def test_failed_refresh_clears_previous_eligible_markets(self):
        client = FakeClient()
        universe = MarketUniverse(client, eligibility=confirmation(), clock=lambda: NOW)
        universe.refresh()
        self.assertTrue(universe.eligible_markets())
        client.fail_pairs = True
        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            universe.refresh()
        self.assertEqual(universe.eligible_markets(), ())
        self.assertIsNone(universe.refreshed_at)
        self.assertIn("network unavailable", universe.last_error)


if __name__ == "__main__":
    unittest.main()
