"""Supervised, read-only Kraken spot observations for paper research only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping

LOG = logging.getLogger(__name__)
ZERO = Decimal('0')


def decimal(value: Any, label: str, *, positive: bool = False) -> Decimal:
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f'{label}: invalid decimal') from exc
    if not d.is_finite() or (positive and d <= 0):
        raise ValueError(f'{label}: nonfinite or nonpositive value')
    return d


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone-aware datetime required')
    return value.astimezone(timezone.utc)


def encode(value: Any):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return utc(value).isoformat()
    raise TypeError(type(value).__name__)


@dataclass(frozen=True)
class MonitorConfig:
    poll_seconds: float = 30
    spacing_seconds: float = 1
    retries: int = 2
    backoff_seconds: float = 0.5
    freshness_seconds: float = 120
    interval_minutes: int = 5
    candle_limit: int = 60
    fast: int = 5
    slow: int = 20
    momentum: int = 3
    refresh_attempts: int = 2
    min_depth_quote: Decimal = Decimal('0')
    min_volume_base: Decimal = Decimal('0')
    max_spread_bps: Decimal = Decimal('100')
    max_volatility_pct: Decimal = Decimal('20')

    def __post_init__(self):
        for name in ('poll_seconds', 'spacing_seconds', 'backoff_seconds', 'freshness_seconds'):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f'invalid {name}')
        if self.poll_seconds <= 0 or self.freshness_seconds <= 0:
            raise ValueError('poll and freshness must be positive')
        for name in ('retries', 'refresh_attempts', 'interval_minutes', 'candle_limit', 'fast', 'slow', 'momentum'):
            v = getattr(self, name)
            if type(v) is not int or v < (0 if name in ('retries', 'refresh_attempts') else 1):
                raise ValueError(f'invalid {name}')
        if self.fast >= self.slow or self.candle_limit < max(self.slow, self.momentum + 1):
            raise ValueError('indicator windows exceed candle limit')
        for name in ('min_depth_quote', 'min_volume_base', 'max_spread_bps', 'max_volatility_pct'):
            d = decimal(getattr(self, name), name)
            if d < 0:
                raise ValueError(f'{name} must be nonnegative')
            object.__setattr__(self, name, d)


class ObservationStore:
    """SQLite WAL append-only observations; Decimal fields serialized as JSON strings."""
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('''CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, pair_id TEXT NOT NULL,
                observed_at TEXT NOT NULL, valid INTEGER NOT NULL CHECK(valid IN (0,1)),
                signal TEXT NOT NULL CHECK(signal IN ('BUY','SELL','HOLD','INVALID')),
                payload TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS observations_pair_time ON observations(pair_id,observed_at,id)')
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA busy_timeout=30000')
        return db

    def append(self, observation: Mapping[str, Any]) -> None:
        payload = json.dumps(observation, default=encode, sort_keys=True, allow_nan=False)
        with self._db() as db:
            db.execute('INSERT INTO observations(pair_id,observed_at,valid,signal,payload) VALUES(?,?,?,?,?)',
                       (observation['pair_id'], observation['observed_at'], int(observation['valid']), observation['signal'], payload))

    def history(self, pair_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be positive')
        with self._db() as db:
            rows = db.execute('SELECT payload FROM observations WHERE pair_id=? ORDER BY observed_at DESC,id DESC LIMIT ?',
                              (pair_id, limit)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def integrity_check(self) -> bool:
        with self._db() as db:
            return db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


class ReadOnlyMarketMonitor:
    def __init__(self, client, universe, store: ObservationStore, config: MonitorConfig | None = None,
                 *, clock: Callable[[], datetime] | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        self.client, self.universe, self.store = client, universe, store
        self.config = config or MonitorConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleeper
        self._lock = threading.Lock()
        self._last_request: float | None = None

    def _request(self, fn, *args, **kwargs):
        for attempt in range(self.config.retries + 1):
            with self._lock:
                if self._last_request is not None:
                    remaining = self.config.spacing_seconds - (time.monotonic() - self._last_request)
                    if remaining > 0:
                        self.sleep(remaining)
                self._last_request = time.monotonic()
            try:
                return fn(*args, **kwargs)
            except (OSError, TimeoutError, ConnectionError) as exc:
                if attempt == self.config.retries:
                    raise RuntimeError('read-only request exhausted retries') from exc
                self.sleep(min(self.config.backoff_seconds * 2 ** attempt, 30))
        raise RuntimeError('unreachable')

    def _markets(self, now: datetime):
        """Refresh first; never return expired/unauthorized markets."""
        if not self.universe.is_fresh(now=now):
            for attempt in range(self.config.refresh_attempts):
                try:
                    self._request(self.universe.refresh)
                    break
                except Exception as exc:
                    LOG.warning('Universe refresh attempt %d failed: %s', attempt + 1, type(exc).__name__)
            if not self.universe.is_fresh(now=now):
                return ()
        return tuple(self.universe.eligible_markets(now=now))

    @staticmethod
    def _pair(payload, pair_id: str, label: str, *, candles: bool = False):
        # KrakenClient returns the decoded `result`, not the raw HTTP envelope.
        if not isinstance(payload, Mapping):
            raise ValueError(f'{label}: missing result mapping')
        if pair_id in payload:
            return payload[pair_id]
        matches = [v for k, v in payload.items() if k != 'last']
        if len(matches) != 1:
            raise ValueError(f'{label}: ambiguous pair result')
        return matches[0]

    def _ticker(self, data, pair_id):
        row = self._pair(data, pair_id, 'ticker')
        bid = decimal(row['b'][0], 'bid', positive=True)
        ask = decimal(row['a'][0], 'ask', positive=True)
        last = decimal(row['c'][0], 'last', positive=True)
        volume = decimal(row['v'][1], '24h base volume')
        if ask <= bid or volume < 0:
            raise ValueError('invalid ticker spread or volume')
        return bid, ask, last, volume

    def _candles(self, data, pair_id, now):
        series = self._pair(data, pair_id, 'OHLC')
        if not isinstance(series, list):
            raise ValueError('OHLC series must be a list')
        interval = self.config.interval_minutes * 60
        previous = None
        completed = []
        for row in series:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                raise ValueError('malformed candle')
            timestamp = decimal(row[0], 'candle timestamp')
            if timestamp != timestamp.to_integral_value() or timestamp < 0:
                raise ValueError('invalid candle timestamp')
            opened = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            if opened > now:
                raise ValueError('future candle')
            if previous is not None and (timestamp <= previous or timestamp - previous > interval * 2):
                raise ValueError('duplicate, unordered or gapped candles')
            previous = timestamp
            close = decimal(row[4], 'close', positive=True)
            volume = decimal(row[6], 'candle volume')
            if volume < 0:
                raise ValueError('negative candle volume')
            if opened + timedelta(seconds=interval) <= now:
                completed.append((opened, close))
        completed = completed[-self.config.candle_limit:]
        if len(completed) < max(self.config.slow, self.config.momentum + 1):
            raise ValueError('insufficient completed candles')
        closed_at = completed[-1][0] + timedelta(seconds=interval)
        if (now - closed_at).total_seconds() > self.config.freshness_seconds:
            raise ValueError('stale OHLC data')
        return completed

    def _book(self, data, pair_id):
        book = self._pair(data, pair_id, 'depth')
        def parse(key):
            raw = book[key]
            if not isinstance(raw, list) or not raw:
                raise ValueError('empty order book')
            return [(decimal(row[0], 'book price', positive=True), decimal(row[1], 'book quantity', positive=True))
                    for row in raw]
        bids, asks = parse('bids'), parse('asks')
        if any(bids[i][0] <= bids[i+1][0] for i in range(len(bids)-1)):
            raise ValueError('unsorted bids')
        if any(asks[i][0] >= asks[i+1][0] for i in range(len(asks)-1)):
            raise ValueError('unsorted asks')
        if bids[0][0] >= asks[0][0]:
            raise ValueError('crossed book')
        return bids, asks

    def observe_market(self, requested_market):
        now = utc(self.clock())
        pair_id = getattr(requested_market, 'pair_id', '')
        report = {'pair_id': pair_id, 'observed_at': now.isoformat(), 'valid': False,
                  'signal': 'INVALID', 'reason': '', 'execution': 'READ_ONLY_PAPER_RESEARCH'}
        try:
            authorized = next((m for m in self._markets(now) if m.pair_id == pair_id), None)
            if authorized is None:
                raise ValueError('pair not authorized in fresh universe')
            bid, ask, last, volume = self._ticker(self._request(self.client.ticker, pair_id), pair_id)
            candles = self._candles(self._request(self.client.ohlc, pair_id, interval=self.config.interval_minutes), pair_id, now)
            bids, asks = self._book(self._request(self.client.order_book, pair_id, count=10), pair_id)
            closes = [c for _, c in candles]
            fast = sum(closes[-self.config.fast:], ZERO) / self.config.fast
            slow = sum(closes[-self.config.slow:], ZERO) / self.config.slow
            momentum = (closes[-1] / closes[-1-self.config.momentum] - 1) * 100
            returns = [(closes[i] / closes[i-1] - 1) * 100 for i in range(1, len(closes))]
            with localcontext() as ctx:
                ctx.prec = 40
                avg = sum(returns, ZERO) / len(returns)
                volatility = (sum(((r-avg)**2 for r in returns), ZERO) / len(returns)).sqrt()
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid * 10000
            book_mid = (bids[0][0] + asks[0][0]) / 2
            if abs(book_mid - mid) / mid * 10000 > self.config.max_spread_bps:
                raise ValueError('ticker/book mismatch')
            depth = sum((p*q for p,q in bids+asks), ZERO)
            reasons = []
            if volume < self.config.min_volume_base: reasons.append('insufficient base volume')
            if depth < self.config.min_depth_quote: reasons.append('insufficient quote depth')
            if spread > self.config.max_spread_bps: reasons.append('excessive spread')
            if volatility > self.config.max_volatility_pct: reasons.append('excessive volatility')
            components = {
                'spread': max(ZERO, Decimal(30) - spread),
                'volume': min(Decimal(20), volume / 1000),
                'depth': min(Decimal(20), depth / 1000),
                'volatility': max(ZERO, Decimal(15) - volatility),
                'momentum': max(ZERO, min(Decimal(15), momentum + Decimal('7.5'))),
            }
            score = sum(components.values(), ZERO)
            signal = 'HOLD'
            if not reasons:
                if fast > slow and momentum > 0: signal = 'BUY'
                elif fast < slow and momentum < 0: signal = 'SELL'
            report.update(valid=not reasons, signal=signal, reason='; '.join(reasons),
                          market=authorized.wsname, bid=bid, ask=ask, last_price=last,
                          volume_24h_base=volume, depth_quote=depth, spread_bps=spread,
                          fast_ma=fast, slow_ma=slow, momentum_pct=momentum,
                          volatility_pct=volatility, score=score, score_components=components,
                          completed_candles=len(candles), last_candle_closed_at=(candles[-1][0] + timedelta(minutes=self.config.interval_minutes)).isoformat())
        except Exception as exc:
            report['valid'], report['signal'] = False, 'INVALID'
            report['reason'] = f'{type(exc).__name__}: {exc}'
            LOG.warning('Invalid observation for %s: %s', pair_id, report['reason'])
        self.store.append(report)
        return report

    def poll_once(self):
        now = utc(self.clock())
        markets = self._markets(now)
        reports = [self.observe_market(m) for m in markets]
        return sorted(reports, key=lambda r: (not r['valid'], -decimal(r.get('score', 0), 'score'), r['pair_id']))

    def report(self):
        now = utc(self.clock())
        markets = self._markets(now)
        reports = []
        for market in markets:
            history = self.store.history(market.pair_id, limit=1)
            if not history:
                continue
            item = dict(history[-1])
            observed = datetime.fromisoformat(item['observed_at'])
            stale = not item['valid'] or (now - utc(observed)).total_seconds() > self.config.freshness_seconds
            item['historical'] = True
            item['stale'] = stale
            item['actionable'] = False
            item['research_signal'] = item['signal']
            item['signal'] = 'INVALID' if stale else 'HOLD'
            reports.append(item)
        return {'generated_at': now.isoformat(), 'execution': 'READ_ONLY_PAPER_RESEARCH',
                'actionable': False, 'ranking_method': 'heuristic only, not predictive', 'markets': reports}

    def run(self, stop_event: threading.Event):
        while not stop_event.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                LOG.exception('Monitoring cycle failed')
            stop_event.wait(max(0, self.config.poll_seconds - (time.monotonic() - started)))
