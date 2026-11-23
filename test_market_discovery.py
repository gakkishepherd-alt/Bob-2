import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from market_discovery import AssetAliases, EligibilityConfirmation, MarketUniverse, discover_markets

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
ASSETS = {"XXBT": {"altname": "XBT", "wsname": "XBT"}, "XETH": {"altname": "ETH", "wsname": "ETH"},
          "ZGBP": {"altname": "GBP", "wsname": "GBP"}, "ZEUR": {"altname": "EUR", "wsname": "EUR"},
          "USD": {"altname": "USD", "wsname": "USD"}}
PAIRS = {
    "XXBTZGBP": {"altname": "XBTGBP", "wsname": "XBT/GBP", "base": "XXBT", "quote": "ZGBP", "status": "online", "ordermin": "0.0001", "costmin": "0.50", "pair_decimals": 1, "lot_decimals": 8},
    "XETHZEUR": {"altname": "ETHEUR", "wsname": "ETH/EUR", "base": "XETH", "quote": "ZEUR", "status": "online", "ordermin": "0.01", "costmin": "1", "pair_decimals": 2, "lot_decimals": 5},
    "SUSP": {"altname": "BTCEUR-S", "wsname": "XBT/EUR", "base": "XXBT", "quote": "ZEUR", "status": "cancel_only", "ordermin": "0.0001", "costmin": "1", "pair_decimals": 1, "lot_decimals": 8},
    "XBTUSD.F": {"altname": "XBTUSD.F", "base": "XXBT", "quote": "USD", "status": "online", "type": "future", "ordermin": "0.001", "costmin": "1", "pair_decimals": 1, "lot_decimals": 6},
    "BADMIN": {"altname": "BADGBP", "base": "XXBT", "quote": "ZGBP", "status": "online", "ordermin": "NaN", "costmin": "0", "pair_decimals": 1, "lot_decimals": 8},
}


def confirmation(pairs=("XXBTZGBP", "XETHZEUR"), at=NOW):
    return EligibilityConfirmation(frozenset(pairs), at, timedelta(hours=1), "reviewer-123",
                                   "Kraken account product-eligibility review CR-4821", "account-alias-uk-retail-01")


class AssetAliasTests(unittest.TestCase):
    def test_asset_key_altname_and_prefix_aliases(self):
        aliases = AssetAliases(ASSETS)
        self.assertEqual(aliases.normalize("XXBT"), "XBT")
        self.assertEqual(aliases.normalize("XBT"), "XBT")
        self.assertEqual(aliases.normalize("ZGBP"), "GBP")
        self.assertEqual(aliases.normalize("GBP"), "GBP")
        self.assertEqual(aliases.normalize("XETH"), "ETH")
        for unknown in ("UNKNOWN", "", "  ", None):
            with self.assertRaises(ValueError):
                aliases.normalize(unknown)

    def test_alias_collision_is_rejected_as_ambiguous(self):
        aliases = AssetAliases({**ASSETS, "XBT": {"altname": "OTHER", "wsname": "OTHER"}})
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            aliases.normalize("XBT")
        colliding_pairs = {"COLLIDE": {**PAIRS["XXBTZGBP"], "base": "XBT"}}
        result = discover_markets({**ASSETS, "XBT": {"altname": "OTHER", "wsname": "OTHER"}},
                                  colliding_pairs, eligibility=confirmation(pairs=("COLLIDE",)), now=NOW)[0]
        self.assertFalse(result.eligible_for_uk_account)

    def test_malformed_asset_rows_do_not_create_valid_aliases(self):
        aliases = AssetAliases({"BROKEN": None, "EMPTY": {"altname": ""}, "XXBT": ASSETS["XXBT"]})
        for malformed in ("BROKEN", "EMPTY"):
            with self.assertRaises(ValueError):
                aliases.normalize(malformed)
        self.assertEqual(aliases.normalize("XXBT"), "XBT")


class MarketDiscoveryTests(unittest.TestCase):
    def test_exact_pair_id_is_required_for_account_eligibility(self):
        confirmed = confirmation(pairs=("XXBTZGBP",))
        found = {m.pair_id: m for m in discover_markets(ASSETS, PAIRS, eligibility=confirmed, now=NOW)}
        self.assertTrue(found["XXBTZGBP"].eligible_for_uk_account)
        self.assertFalse(found["XETHZEUR"].eligible_for_uk_account)
        # Approval for an altname, wsname, or shared base/quote symbol cannot authorize another ID.
        for alias in ("XBTGBP", "XBT/GBP", "XBT/GBP"):
            alias_confirmation = confirmation(pairs=(alias,))
            markets = {m.pair_id: m for m in discover_markets(ASSETS, PAIRS, eligibility=alias_confirmation, now=NOW)}
            self.assertFalse(markets["XXBTZGBP"].eligible_for_uk_account)
            self.assertTrue(any("exact Kraken pair ID" in r for r in markets["XXBTZGBP"].exclusion_reasons))

    def test_dynamic_discovery_market_rules_are_decimal(self):
        found = {m.pair_id: m for m in discover_markets(ASSETS, PAIRS, eligibility=confirmation(), now=NOW)}
        btc = found["XXBTZGBP"]
        self.assertEqual((btc.base, btc.quote), ("XBT", "GBP"))
        self.assertEqual(btc.order_minimum, Decimal("0.0001"))
        self.assertEqual(btc.cost_minimum, Decimal("0.50"))
        self.assertEqual(btc.price_increment, Decimal("0.1"))
        self.assertEqual(btc.quantity_increment, Decimal("1E-8"))
        self.assertTrue(btc.eligible_for_uk_account)

    def test_missing_null_empty_and_unknown_assets_fail_closed(self):
        pairs = {"NONE": {**PAIRS["XXBTZGBP"], "base": None},
                 "EMPTY": {**PAIRS["XXBTZGBP"], "base": ""},
                 "UNKNOWN": {**PAIRS["XXBTZGBP"], "quote": "NOPE"},
                 "GOOD": PAIRS["XXBTZGBP"]}
        eligible_ids = ("NONE", "EMPTY", "UNKNOWN", "GOOD")
        found = {m.pair_id: m for m in discover_markets(ASSETS, pairs, eligibility=confirmation(eligible_ids), now=NOW)}
        for pair in ("NONE", "EMPTY", "UNKNOWN"):
            self.assertFalse(found[pair].eligible_for_uk_account)
            self.assertTrue(any("invalid" in reason for reason in found[pair].exclusion_reasons))
        self.assertTrue(found["GOOD"].eligible_for_uk_account)

    def test_expired_or_nonmatching_account_policy_fails_closed(self):
        for approved in (None, confirmation(at=NOW - timedelta(hours=2)), confirmation(pairs=())):
            markets = discover_markets(ASSETS, PAIRS, eligibility=approved, now=NOW)
            self.assertFalse(any(m.eligible_for_uk_account for m in markets))

    def test_restrictions_and_missing_trading_rules_are_excluded(self):
        altered = dict(PAIRS)
        altered["MARGIN"] = {**PAIRS["XXBTZGBP"], "margin_only": True}
        altered["NORULES"] = {"base": "XXBT", "quote": "ZGBP", "status": "online"}
        confirmed_ids = ("XXBTZGBP", "XETHZEUR", "MARGIN", "NORULES")
        by_id = {m.pair_id: m for m in discover_markets(ASSETS, altered, eligibility=confirmation(confirmed_ids), now=NOW)}
        self.assertFalse(by_id["SUSP"].eligible_for_uk_account)
        self.assertFalse(by_id["XBTUSD.F"].eligible_for_uk_account)
        self.assertFalse(by_id["MARGIN"].eligible_for_uk_account)
        self.assertFalse(by_id["BADMIN"].eligible_for_uk_account)
        self.assertFalse(by_id["NORULES"].eligible_for_uk_account)

    def test_invalid_confirmation_and_response_shapes_rejected(self):
        with self.assertRaises(ValueError):
            EligibilityConfirmation(frozenset({"XBTGBP"}), NOW.replace(tzinfo=None), timedelta(hours=1), "r", "s", "a")
        with self.assertRaises(ValueError):
            EligibilityConfirmation(frozenset({None}), NOW, timedelta(hours=1), "r", "s", "a")
        with self.assertRaises(ValueError):
            discover_markets([], PAIRS, eligibility=confirmation(), now=NOW)


class FakeClient:
    def __init__(self): self.fail_pairs = False
    def assets(self): return ASSETS
    def asset_pairs(self):
        if self.fail_pairs: raise RuntimeError("network unavailable")
        return PAIRS


class MarketUniverseTests(unittest.TestCase):
    def test_snapshot_age_and_eligibility_expire(self):
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
