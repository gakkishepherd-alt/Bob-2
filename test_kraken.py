import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from kraken import KrakenAPIError, KrakenClient, KrakenError, KrakenTransportError, PersistentNonce


class FakeResponse:
    def __init__(self, payload, url="https://api.kraken.com/0/public/Ticker?pair=XBTGBP"):
        self.payload = json.dumps(payload).encode("utf-8")
        self.url = url
    def read(self):
        return self.payload
    def geturl(self):
        return self.url


class KrakenAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.nonce_path = os.path.join(self.temp.name, "nonce.sqlite3")
        self.calls = []
        self.secret = base64.b64encode(b"dummy-secret-for-test").decode("ascii")
        self.credentials = {"KRAKEN_API_KEY": "test-key", "KRAKEN_API_SECRET": self.secret}
    def tearDown(self):
        self.temp.cleanup()
    def nonce(self, request):
        return urllib.parse.parse_qs(request.data.decode("ascii"))["nonce"][0]
    def client(self, transport=None, **kwargs):
        return KrakenClient(transport=transport or self.ok_transport, nonce_db_path=self.nonce_path, **kwargs)
    def ok_transport(self, request, timeout):
        self.calls.append((request, timeout))
        return FakeResponse({"error": [], "result": {"XBTGBP": {"c": ["50000", "1"]}}}, request.full_url)

    def test_signing_matches_independent_known_calculation(self):
        nonce = "123456"
        path = "/0/private/Balance"
        body = urllib.parse.urlencode({"nonce": nonce}).encode()
        secret = b"test-private-key"
        expected = base64.b64encode(__import__("hmac").new(
            secret, path.encode() + __import__("hashlib").sha256(nonce.encode() + body).digest(), __import__("hashlib").sha512
        ).digest()).decode()
        self.assertEqual(KrakenClient._signature(path, body, nonce, secret), expected)

    def test_public_ticker_and_pair_query(self):
        client = self.client()
        self.assertIn("XBTGBP", client.ticker("XBTGBP"))
        request, timeout = self.calls[0]
        self.assertIn("/0/public/Ticker?pair=XBTGBP", request.full_url)
        self.assertIsNone(request.data)
        self.assertEqual(timeout, 10.0)

    def test_public_market_endpoints_are_gets(self):
        client = self.client()
        client.ohlc("XBTGBP", interval=5, since=123)
        client.asset_pairs("XBTGBP")
        client.order_book("XBTGBP", count=5)
        client.public_health()
        self.assertTrue(all(call[0].get_method() == "GET" for call in self.calls))

    def test_private_calls_use_read_only_allowlist_and_strictly_increasing_nonces(self):
        with patch.dict(os.environ, self.credentials):
            client = self.client()
            client.balances(); client.open_orders(); client.trade_history()
        nonces = []
        for request, _ in self.calls:
            self.assertTrue(request.full_url.endswith(("/Balance", "/OpenOrders", "/TradesHistory")))
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Api-key"), "test-key")
            self.assertTrue(request.get_header("Api-sign"))
            nonces.append(int(self.nonce(request)))
        self.assertEqual(nonces, sorted(set(nonces)))
        self.assertNotIn("test-key", repr(client))

    def test_nonce_is_unique_across_threads_and_persistent_across_store_instances(self):
        store = PersistentNonce(self.nonce_path)
        with ThreadPoolExecutor(max_workers=12) as pool:
            values = list(pool.map(lambda _: int(store.next("key-id")), range(120)))
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(sorted(values), values if values == sorted(values) else sorted(values))
        self.assertGreater(int(PersistentNonce(self.nonce_path).next("key-id")), max(values))

    def test_rejects_untrusted_hosts_insecure_urls_and_redirects(self):
        client = self.client()
        for url in ("http://api.kraken.com/path", "https://evil.example/path", "https://api.kraken.com.evil.example/path", "https://user@api.kraken.com/path"):
            with self.assertRaises(KrakenTransportError):
                client._validate_official_url(url)
        response = FakeResponse({"error": [], "result": {}}, "https://evil.example/redirect")
        client = self.client(transport=lambda request, timeout: response)
        with self.assertRaises(KrakenTransportError):
            client.ticker("XBTGBP")

    def test_private_rate_limit_retries_with_fresh_nonce(self):
        def limited_then_ok(request, timeout):
            self.calls.append((request, timeout))
            if len(self.calls) == 1:
                return FakeResponse({"error": ["EAPI:Rate limit exceeded"], "result": None}, request.full_url)
            return FakeResponse({"error": [], "result": {"ZGBP": "110.43"}}, request.full_url)
        with patch.dict(os.environ, self.credentials):
            self.client(transport=limited_then_ok, retries=1, sleeper=lambda _: None).balances()
        self.assertLess(int(self.nonce(self.calls[0][0])), int(self.nonce(self.calls[1][0])))

    def test_http_429_is_classified_as_rate_limit(self):
        def http_429(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "Too many requests", {}, None)
        with patch.dict(os.environ, self.credentials):
            with self.assertRaises(KrakenAPIError) as caught:
                self.client(transport=http_429, retries=0).balances()
        self.assertTrue(caught.exception.rate_limited)

    def test_malformed_response_and_network_failure_are_safe_errors(self):
        with self.assertRaises(KrakenTransportError):
            self.client(transport=lambda request, timeout: type("R", (), {"read": lambda self: b"not json", "geturl": lambda self: request.full_url})()).ticker("XBTGBP")
        def failed(request, timeout):
            raise urllib.error.URLError("offline")
        with self.assertRaises(KrakenTransportError):
            self.client(transport=failed).ticker("XBTGBP")

    def test_missing_credentials_and_write_endpoints_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KrakenError):
                self.client().balances()
        client = self.client()
        for endpoint in ("AddOrder", "Withdraw", "Deposit", "Transfer", "TradeBalance"):
            with self.assertRaises(ValueError):
                client._post_private(endpoint)

    def test_auth_failure_does_not_retry_or_leak_credentials(self):
        def denied(request, timeout):
            self.calls.append((request, timeout))
            return FakeResponse({"error": ["EAPI:Invalid key"], "result": None}, request.full_url)
        with patch.dict(os.environ, self.credentials):
            with self.assertRaises(KrakenAPIError) as caught:
                self.client(transport=denied, retries=3).balances()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("test-key", str(caught.exception))
        self.assertNotIn(self.secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
