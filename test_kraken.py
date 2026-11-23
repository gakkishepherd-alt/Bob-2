import base64
import json
import os
import unittest
from unittest.mock import patch

from kraken import KrakenAPIError, KrakenClient, KrakenError


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload


class KrakenAdapterTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake_transport(self, request, timeout):
        self.calls.append((request, timeout))
        return FakeResponse({"error": [], "result": {"XBTGBP": {"c": ["50000", "1"]}}})

    def test_public_ticker_and_pair_query(self):
        client = KrakenClient(transport=self.fake_transport)
        result = client.ticker("XBTGBP")
        self.assertIn("XBTGBP", result)
        request, timeout = self.calls[0]
        self.assertIn("/0/public/Ticker?pair=XBTGBP", request.full_url)
        self.assertIsNone(request.data)
        self.assertEqual(timeout, 10.0)

    def test_public_ohlc_pairs_depth_health_are_read_only_gets(self):
        client = KrakenClient(transport=self.fake_transport)
        client.ohlc("XBTGBP", interval=5, since=123)
        client.asset_pairs("XBTGBP")
        client.order_book("XBTGBP", count=5)
        client.public_health()
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(call[0].get_method() == "GET" for call in self.calls))

    def test_private_calls_use_query_allowlist_and_signed_post(self):
        secret = base64.b64encode(b"dummy-secret-for-test").decode("ascii")
        with patch.dict(os.environ, {"KRAKEN_API_KEY": "test-key", "KRAKEN_API_SECRET": secret}):
            client = KrakenClient(transport=self.fake_transport)
            client.balances()
            client.open_orders()
            client.trade_history()
        self.assertEqual(len(self.calls), 3)
        for request, _ in self.calls:
            self.assertTrue(request.full_url.endswith(("/Balance", "/OpenOrders", "/TradesHistory")))
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Api-key"), "test-key")
            self.assertTrue(request.get_header("Api-sign"))
        self.assertNotIn("test-key", repr(client))

    def test_missing_credentials_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            client = KrakenClient(transport=self.fake_transport)
            with self.assertRaises(KrakenError):
                client.balances()
        self.assertEqual(self.calls, [])

    def test_rejects_non_query_private_endpoint(self):
        client = KrakenClient(transport=self.fake_transport)
        with self.assertRaises(ValueError):
            client._post_private("AddOrder")
        with self.assertRaises(ValueError):
            client._post_private("Withdraw")
        self.assertEqual(self.calls, [])

    def test_api_errors_are_raised_without_retry_unless_rate_limited(self):
        def denied(request, timeout):
            return FakeResponse({"error": ["EAPI:Invalid key"], "result": None})
        client = KrakenClient(transport=denied, retries=2, sleeper=lambda _: None)
        with self.assertRaises(KrakenAPIError):
            client.ticker("XBTGBP")

    def test_rate_limit_retries_boundedly(self):
        calls = []
        sleeps = []
        def throttled_then_ok(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                return FakeResponse({"error": ["EAPI:Rate limit exceeded"], "result": None})
            return FakeResponse({"error": [], "result": {"status": "online"}})
        client = KrakenClient(transport=throttled_then_ok, retries=1, sleeper=sleeps.append)
        self.assertTrue(client.public_health()["ok"])
        self.assertEqual((len(calls), sleeps), (2, [0.5]))

    def test_private_health_never_returns_balance_values(self):
        secret = base64.b64encode(b"dummy-secret-for-test").decode("ascii")
        def balances(request, timeout):
            return FakeResponse({"error": [], "result": {"ZGBP": "110.43"}})
        with patch.dict(os.environ, {"KRAKEN_API_KEY": "test-key", "KRAKEN_API_SECRET": secret}):
            result = KrakenClient(transport=balances).private_health()
        self.assertEqual(result, {"ok": True, "private_query": "available", "balance_assets": 1})
        self.assertNotIn("110.43", repr(result))


if __name__ == "__main__":
    unittest.main()
