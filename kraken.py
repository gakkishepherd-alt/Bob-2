"""Read-only Kraken REST API adapters.

Only an explicit allowlist of public market-data and private query endpoints is
available. This module intentionally contains no order, deposit, withdrawal,
transfer, margin, or funding operation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping


_LOG = logging.getLogger(__name__)
_BASE_URL = "https://api.kraken.com"
_PUBLIC_ENDPOINTS = frozenset({
    "Ticker", "OHLC", "AssetPairs", "Depth", "SystemStatus",
})
_PRIVATE_ENDPOINTS = frozenset({"Balance", "OpenOrders", "TradesHistory"})
_RATE_LIMIT_MARKERS = ("rate limit", "throttl", "too many requests")


class KrakenError(RuntimeError):
    """Base error for safe, sanitized Kraken adapter failures."""


class KrakenTransportError(KrakenError):
    """Network, timeout, HTTP, or malformed-response error."""


class KrakenAPIError(KrakenError):
    """Kraken returned an API-level error."""

    def __init__(self, messages: list[str]):
        # Do not include request headers, payloads, or credentials in exceptions.
        super().__init__("Kraken API error: " + "; ".join(messages))
        self.messages = tuple(messages)

    @property
    def rate_limited(self) -> bool:
        return any(marker in message.lower() for message in self.messages for marker in _RATE_LIMIT_MARKERS)


class KrakenClient:
    """Small REST client limited to read-only Kraken endpoints.

    Credentials are read from KRAKEN_API_KEY and KRAKEN_API_SECRET only when
    a private query is requested. The injected transport and sleeper support
    deterministic tests; production defaults use urllib and time.sleep.
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        retries: int = 2,
        api_key_env: str = "KRAKEN_API_KEY",
        api_secret_env: str = "KRAKEN_API_SECRET",
        transport: Callable | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        base_url: str = _BASE_URL,
    ):
        if timeout <= 0 or retries < 0:
            raise ValueError("timeout must be positive and retries nonnegative")
        self.timeout = timeout
        self.retries = retries
        self.api_key_env = api_key_env
        self.api_secret_env = api_secret_env
        self._transport = transport or urllib.request.urlopen
        self._sleep = sleeper
        self._base_url = base_url.rstrip("/")
        self._nonce_lock = threading.Lock()
        self._last_nonce = 0

    @staticmethod
    def _decode_response(response) -> dict:
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise KrakenTransportError("Kraken returned an invalid JSON response") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("error"), list):
            raise KrakenTransportError("Kraken returned an unexpected response shape")
        errors = [str(item) for item in payload["error"]]
        if errors:
            raise KrakenAPIError(errors)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise KrakenTransportError("Kraken response is missing its result object")
        return result

    def _send(self, request: urllib.request.Request):
        try:
            return self._transport(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise KrakenAPIError(["HTTP rate limit exceeded"]) from exc
            if 500 <= exc.code < 600:
                raise KrakenTransportError("Kraken service temporarily unavailable") from exc
            raise KrakenTransportError(f"Kraken HTTP request failed (status {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise KrakenTransportError("Kraken network request failed or timed out") from exc

    def _get_json(self, endpoint: str, params: Mapping[str, object] | None = None) -> dict:
        if endpoint not in _PUBLIC_ENDPOINTS:
            raise ValueError("Unsupported public Kraken endpoint")
        query = urllib.parse.urlencode(params or {})
        url = f"{self._base_url}/0/public/{endpoint}"
        if query:
            url += "?" + query
        _LOG.debug("Kraken public request: %s", endpoint)
        request = urllib.request.Request(url, headers={"User-Agent": "Bob-2-readonly/1"})
        return self._with_retries(lambda: self._decode_response(self._send(request)))

    def _next_nonce(self) -> str:
        with self._nonce_lock:
            self._last_nonce = max(time.time_ns() // 1_000_000, self._last_nonce + 1)
            return str(self._last_nonce)

    def _credentials(self) -> tuple[str, bytes]:
        key = os.environ.get(self.api_key_env)
        secret_text = os.environ.get(self.api_secret_env)
        if not key or not secret_text:
            raise KrakenError(
                f"Read-only Kraken credentials are required in environment variables "
                f"{self.api_key_env} and {self.api_secret_env}"
            )
        try:
            secret = base64.b64decode(secret_text, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise KrakenError("Kraken API secret environment variable is not valid base64") from exc
        return key, secret

    @staticmethod
    def _signature(path: str, postdata: bytes, nonce: str, secret: bytes) -> str:
        digest = hashlib.sha256(nonce.encode("utf-8") + postdata).digest()
        mac = hmac.new(secret, path.encode("utf-8") + digest, hashlib.sha512).digest()
        return base64.b64encode(mac).decode("ascii")

    def _post_private(self, endpoint: str, params: Mapping[str, object] | None = None) -> dict:
        if endpoint not in _PRIVATE_ENDPOINTS:
            raise ValueError("Unsupported private Kraken endpoint; only read-only queries are allowed")
        key, secret = self._credentials()
        nonce = self._next_nonce()
        form = {str(k): str(v) for k, v in (params or {}).items()}
        form["nonce"] = nonce
        body = urllib.parse.urlencode(form).encode("ascii")
        path = f"/0/private/{endpoint}"
        signature = self._signature(path, body, nonce, secret)
        request = urllib.request.Request(
            self._base_url + path,
            data=body,
            headers={
                "API-Key": key,
                "API-Sign": signature,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Bob-2-readonly/1",
            },
            method="POST",
        )
        # Never log the request, headers, body, key, signature, or response data.
        _LOG.debug("Kraken private read-only query: %s", endpoint)
        return self._with_retries(lambda: self._decode_response(self._send(request)))

    def _with_retries(self, operation: Callable[[], dict]) -> dict:
        for attempt in range(self.retries + 1):
            try:
                return operation()
            except KrakenAPIError as exc:
                if not exc.rate_limited or attempt >= self.retries:
                    raise
                delay = min(0.5 * (2 ** attempt), 4.0)
                _LOG.warning("Kraken rate limited request; retrying after %.1f seconds", delay)
                self._sleep(delay)
            except KrakenTransportError:
                # Transport failures are reported immediately; do not blindly
                # repeat a private request whose server-side outcome is unknown.
                raise
        raise KrakenTransportError("Kraken request retry limit exceeded")

    # Public market-data methods
    def ticker(self, pair: str) -> dict:
        return self._get_json("Ticker", {"pair": pair})

    def ohlc(self, pair: str, *, interval: int = 1, since: int | None = None) -> dict:
        params: dict[str, object] = {"pair": pair, "interval": interval}
        if since is not None:
            params["since"] = since
        return self._get_json("OHLC", params)

    def asset_pairs(self, pair: str | None = None) -> dict:
        return self._get_json("AssetPairs", {"pair": pair} if pair else None)

    def order_book(self, pair: str, *, count: int = 10) -> dict:
        if count < 1:
            raise ValueError("order book count must be positive")
        return self._get_json("Depth", {"pair": pair, "count": count})

    def public_health(self) -> dict:
        """Return Kraken system status and adapter health; does not authenticate."""
        result = self._get_json("SystemStatus")
        return {"ok": True, "system_status": result}

    # Authenticated query-only private methods
    def balances(self) -> dict:
        return self._post_private("Balance")

    def open_orders(self, *, trades: bool = False) -> dict:
        return self._post_private("OpenOrders", {"trades": "true" if trades else "false"})

    def trade_history(self, *, ofs: int = 0, trades: bool = False) -> dict:
        if ofs < 0:
            raise ValueError("trade history offset cannot be negative")
        return self._post_private("TradesHistory", {"ofs": ofs, "trades": "true" if trades else "false"})

    def private_health(self) -> dict:
        """Check credentials and authenticated query access by retrieving balances."""
        balances = self.balances()
        return {"ok": True, "private_query": "available", "balance_assets": len(balances)}
