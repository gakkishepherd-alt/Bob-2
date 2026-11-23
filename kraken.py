"""Read-only Kraken REST API adapters with strict host and nonce controls.

Only an explicit allowlist of public market-data and private query endpoints is
available. No order, deposit, withdrawal, transfer, margin, or funding operation
is implemented.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

_LOG = logging.getLogger(__name__)
_BASE_URL = "https://api.kraken.com"
_ALLOWED_HOST = "api.kraken.com"
_PUBLIC_ENDPOINTS = frozenset({"Ticker", "OHLC", "AssetPairs", "Depth", "SystemStatus"})
_PRIVATE_ENDPOINTS = frozenset({"Balance", "OpenOrders", "TradesHistory"})
_RATE_LIMIT_MARKERS = ("rate limit", "throttl", "too many requests")


class KrakenError(RuntimeError):
    """Base error for sanitized adapter failures."""


class KrakenTransportError(KrakenError):
    """Network, timeout, HTTP, redirect, or malformed-response error."""


class KrakenAPIError(KrakenError):
    """Kraken returned an API-level error."""

    def __init__(self, messages: list[str]):
        super().__init__("Kraken API error: " + "; ".join(messages))
        self.messages = tuple(messages)

    @property
    def rate_limited(self) -> bool:
        return any(marker in message.lower() for message in self.messages for marker in _RATE_LIMIT_MARKERS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects rather than risk forwarding authentication headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_transport(request: urllib.request.Request, *, timeout: float):
    opener = urllib.request.build_opener(_NoRedirect())
    return opener.open(request, timeout=timeout)


class PersistentNonce:
    """Cross-thread/process monotonically increasing nonce backed by SQLite."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS nonce_state (identity TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def next(self, identity: str) -> str:
        # Only a one-way hash of the API key is persisted, never the key itself.
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM nonce_state WHERE identity = ?", (identity_hash,)).fetchone()
            value = max(time.time_ns() // 1_000_000, (row[0] + 1) if row else 1)
            db.execute(
                "INSERT INTO nonce_state(identity, value) VALUES(?, ?) "
                "ON CONFLICT(identity) DO UPDATE SET value = excluded.value",
                (identity_hash, value),
            )
            db.execute("COMMIT")
        return str(value)


class KrakenClient:
    """Client fixed to Kraken's official HTTPS REST API and query allowlists."""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        retries: int = 2,
        api_key_env: str = "KRAKEN_API_KEY",
        api_secret_env: str = "KRAKEN_API_SECRET",
        nonce_db_path: str | os.PathLike[str] | None = None,
        transport: Callable | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if timeout <= 0 or retries < 0:
            raise ValueError("timeout must be positive and retries nonnegative")
        self.timeout = timeout
        self.retries = retries
        self.api_key_env = api_key_env
        self.api_secret_env = api_secret_env
        self._transport = transport or _default_transport
        self._sleep = sleeper
        path = nonce_db_path or os.environ.get("BOB2_KRAKEN_NONCE_DB") or "~/.bob2/kraken_nonce.sqlite3"
        self._nonce = PersistentNonce(path)

    @staticmethod
    def _validate_official_url(url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _ALLOWED_HOST
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise KrakenTransportError("Refusing Kraken request to a non-official or insecure host")

    @staticmethod
    def _decode_response(response) -> dict:
        try:
            payload = json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise KrakenTransportError("Kraken returned invalid JSON") from exc
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
        self._validate_official_url(request.full_url)
        try:
            response = self._transport(request, timeout=self.timeout)
            # Defend against injected/custom transports that return a redirected response.
            final_url = getattr(response, "geturl", lambda: request.full_url)()
            self._validate_official_url(final_url)
            if final_url != request.full_url:
                raise KrakenTransportError("Kraken redirects are not permitted")
            return response
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise KrakenAPIError(["HTTP rate limit exceeded"]) from exc
            if 300 <= exc.code < 400:
                raise KrakenTransportError("Kraken redirects are not permitted") from exc
            if 500 <= exc.code < 600:
                raise KrakenTransportError("Kraken service temporarily unavailable") from exc
            raise KrakenTransportError(f"Kraken HTTP request failed (status {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise KrakenTransportError("Kraken network request failed or timed out") from exc

    def _get_json(self, endpoint: str, params: Mapping[str, object] | None = None) -> dict:
        if endpoint not in _PUBLIC_ENDPOINTS:
            raise ValueError("Unsupported public Kraken endpoint")
        query = urllib.parse.urlencode(params or {})
        url = f"{_BASE_URL}/0/public/{endpoint}" + ("?" + query if query else "")
        request = urllib.request.Request(url, headers={"User-Agent": "Bob-2-readonly/1"})
        _LOG.debug("Kraken public request: %s", endpoint)
        return self._with_retries(lambda: self._decode_response(self._send(request)))

    def _credentials(self) -> tuple[str, bytes]:
        key = os.environ.get(self.api_key_env)
        secret_text = os.environ.get(self.api_secret_env)
        if not key or not secret_text:
            raise KrakenError(f"Read-only Kraken credentials are required in {self.api_key_env} and {self.api_secret_env}")
        try:
            secret = base64.b64decode(secret_text, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise KrakenError("Kraken API secret environment variable is not valid base64") from exc
        return key, secret

    @staticmethod
    def _signature(path: str, postdata: bytes, nonce: str, secret: bytes) -> str:
        digest = hashlib.sha256(nonce.encode("utf-8") + postdata).digest()
        return base64.b64encode(hmac.new(secret, path.encode("utf-8") + digest, hashlib.sha512).digest()).decode("ascii")

    def _post_private(self, endpoint: str, params: Mapping[str, object] | None = None) -> dict:
        if endpoint not in _PRIVATE_ENDPOINTS:
            raise ValueError("Unsupported private Kraken endpoint; only read-only queries are allowed")
        key, secret = self._credentials()
        path = f"/0/private/{endpoint}"

        def send_fresh_request() -> dict:
            nonce = self._nonce.next(key)
            form = {str(k): str(v) for k, v in (params or {}).items()}
            form["nonce"] = nonce
            body = urllib.parse.urlencode(form).encode("ascii")
            request = urllib.request.Request(
                _BASE_URL + path,
                data=body,
                headers={
                    "API-Key": key,
                    "API-Sign": self._signature(path, body, nonce, secret),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Bob-2-readonly/1",
                },
                method="POST",
            )
            _LOG.debug("Kraken private read-only query: %s", endpoint)
            return self._decode_response(self._send(request))

        return self._with_retries(send_fresh_request)

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
                raise
        raise KrakenTransportError("Kraken retry limit exceeded")

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
        return {"ok": True, "system_status": self._get_json("SystemStatus")}

    def balances(self) -> dict:
        return self._post_private("Balance")

    def open_orders(self, *, trades: bool = False) -> dict:
        return self._post_private("OpenOrders", {"trades": "true" if trades else "false"})

    def trade_history(self, *, ofs: int = 0, trades: bool = False) -> dict:
        if ofs < 0:
            raise ValueError("trade history offset cannot be negative")
        return self._post_private("TradesHistory", {"ofs": ofs, "trades": "true" if trades else "false"})

    def private_health(self) -> dict:
        balances = self.balances()
        return {"ok": True, "private_query": "available", "balance_assets": len(balances)}
