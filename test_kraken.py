import base64
import json
import os
import unittest
import urllib.parse
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
        self.secret = base64.b64encode(b"dummy-secret-for-test").decode("ascii")
        self.credentials = {"KRAKEN_API_KEY": "test-key", "KRAKEN_API_SECRET": self.secret}

    def fake_transport(self, request, timeout):
        self.calls.append((request, timeout))
        return FakeResponse({"error": [], "result": {"XBTGBP": {"c": ["50000", "1"]}}})

    @staticmethod
    def nonce(request):
        return urllib.parse.parse_qs(request.data.decode("ascii"))["nonce"][0]

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
        with patch.dict(os.environ, self.credentials):
            client = KrakenClient(transport=self.fake_transport)
            client.balances()
            client.open_orders()
            client.trade_history()
        self.assertEqual(len(self.calls), 3)
        nonces = []
        for request, _ in self.calls:
            self.assertTrue(request.full_url.endswith(("/Balance", "/OpenOrders", "/TradesHistory")))
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Api-key"), "test-key")
            self.assertTrue(request.get_header("Api-sign"))
            nonces.append(int(self.nonce(request)))
        self.assertEqual(nonces, sorted(set(nonces)))
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

    def test_authentication_failure_fails_without_retry_and_does_not_leak_credentials(self):
        def denied(request, timeout):
            self.calls.append((request, timeout))
            return FakeResponse({"error": ["EAPI:Invalid key"], "result": None})
        with patch.dict(os.environ, self.credentials):
            client = KrakenClient(transport=denied, retries=3, sleeper=lambda _: None)
            with self.assertRaises(KrakenAPIError) as error:
                client.balances()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("test-key", str(error.exception))
        self.assertNotIn(self.secret, str(error.exception))

    def test_private_rate_limit_retry_uses_fresh_strictly_increasing_nonce(self):
        sleeps = []
        def throttled_then_ok(request, timeout):
            self.calls.append((request, timeout))
            if len(self.calls) < 3:
                return FakeResponse({"error": ["EAPI:Rate limit exceeded"], "result": None})
            return FakeResponse({"error": [], "result": {"ZGBP": "110.43"}})
        with patch.dict(os.environ, self.credentials):
            client = KrakenClient(transport=throttled_then_ok, retries=2, sleeper=sleeps.append)
            result = client.balances()
        nonces = [int(self.nonce(request)) for request, _ in self.calls]
        self.assertEqual(len(nonces), 3)
        self.assertEqual(nonces, sorted(set(nonces)))
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(result, {"ZGBP": "110.43"})

    def test_rate_limit_retry_exhaustion_is_bounded(self):
        def throttled(request, timeout):
            self.calls.append((request, timeout))
            return FakeResponse({"error": ["EAPI:Rate limit exceeded"], "result": None})
        with patch.dict(os.environ, self.credentials):
            client = KrakenClient(transport=throttled, retries=1, sleeper=lambda _: None)
            with self.assertRaises(KrakenAPIError):
                client.open_orders()
        self.assertEqual(len(self.calls), 2)
        nonces = [int(self.nonce(request)) for request, _ in self.calls]
        self.assertEqual(nonces, sorted(set(nonces)))

    def test_public_rate_limit_retries_boundedly(self):
        sleeps = []
        def throttled_then_ok(request, timeout):
            self.calls.append((request, timeout))
            if len(self.calls) == 1:
                return FakeResponse({"error": ["EAPI:Rate limit exceeded"], "result": None})
            return FakeResponse({"error": [], "result": {"status": "online"}})
        client = KrakenClient(transport=throttled_then_ok, retries=1, sleeper=sleeps.append)
        self.assertTrue(client.public_health()["ok"])
        self.assertEqual((len(self.calls), sleeps), (2, [0.5]))

    def test_private_health_never_returns_balance_values(self):
        def balances(request, timeout):
            return FakeResponse({"error": [], "result": {"ZGBP": "110.43"}})
        with patch.dict(os.environ, self.credentials):
            result = KrakenClient(transport=balances).private_health()
        self.assertEqual(result, {"ok": True, "private_query": "available", "balance_assets": 1})
        self.assertNotIn("110.43", repr(result))


if __name__ == "__main__":
    unittest.main()
