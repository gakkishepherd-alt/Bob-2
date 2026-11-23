"""Currency-aware, Decimal-exact SQLite paper ledger; no exchange execution.

Numeric values are persisted as decimal TEXT, never SQLite REAL. Legacy v1
``metadata``/``fills`` databases are imported once. Since legacy fees did not
carry a currency, migration explicitly treats them as quote-currency fees.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

CENT = Decimal("0.01")
ZERO = Decimal("0")


def decimal(value: Any, label: str = "value") -> Decimal:
    """Convert via text, rejecting floats that are not finite."""
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite decimal")
    return result


def _timestamp(value: str | datetime | None = None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Timestamp must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _market_currencies(market: str, base: str | None = None, quote: str | None = None) -> tuple[str, str]:
    if base and quote:
        return base.upper(), quote.upper()
    if not isinstance(market, str) or "/" not in market:
        raise ValueError("Market must be BASE/QUOTE, or base_currency and quote_currency must be supplied")
    left, right = market.split("/", 1)
    if not left or not right:
        raise ValueError("Market must contain base and quote currencies")
    return left.upper(), right.upper()


class PaperLedger:
    """Append-only paper fills with replayed multi-currency balances and positions.

    ``initial_cash`` remains a GBP-compatible shorthand. New ledgers should use
    ``initial_balances={"GBP": Decimal("100.00"), "USD": Decimal("25.00")}``.
    Fill timestamps are UTC and monotonic: equal timestamps are ordered by the
    SQLite sequence id; inserting a backdated fill is rejected.
    """

    def __init__(self, path: str | os.PathLike[str], initial_cash: Any | None = None,
                 *, initial_balances: Mapping[str, Any] | None = None):
        if initial_cash is not None and initial_balances is not None:
            raise ValueError("Use initial_cash or initial_balances, not both")
        requested: dict[str, Decimal] | None = None
        if initial_balances is not None:
            requested = {str(currency).upper(): decimal(amount, "Initial balance")
                         for currency, amount in initial_balances.items()}
            if any(not currency or amount < 0 for currency, amount in requested.items()):
                raise ValueError("Initial balances require currency codes and nonnegative amounts")
        elif initial_cash is not None:
            amount = decimal(initial_cash, "Initial cash")
            if amount < 0:
                raise ValueError("Initial cash must be nonnegative")
            requested = {"GBP": amount}
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS ledger_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                market TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
                base_currency TEXT NOT NULL,
                quote_currency TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                notional TEXT NOT NULL,
                fee_amount TEXT NOT NULL,
                fee_currency TEXT NOT NULL,
                realized_pnl TEXT NOT NULL,
                pnl_currency TEXT NOT NULL,
                fill_id TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ledger_events_time ON ledger_events(timestamp_utc, id)")
            self._migrate_legacy(db, requested)
            saved = self._initial_balances(db)
            if not saved:
                defaults = requested or {"GBP": Decimal("100.00")}
                for currency, amount in sorted(defaults.items()):
                    db.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?)",
                               (f"initial:{currency}", str(amount)))
                db.execute("INSERT INTO ledger_meta(key,value) VALUES('schema_version','2')")
                saved = defaults
            elif requested is not None and saved != requested:
                raise ValueError("Initial balances do not match the existing ledger")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _initial_balances(db: sqlite3.Connection) -> dict[str, Decimal]:
        return {row["key"].split(":", 1)[1]: decimal(row["value"], "Stored initial balance")
                for row in db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE 'initial:%'")}

    def _migrate_legacy(self, db: sqlite3.Connection, requested: dict[str, Decimal] | None) -> None:
        """One-time v1 migration; preserve source tables for audit/rollback."""
        done = db.execute("SELECT value FROM ledger_meta WHERE key='legacy_migration'").fetchone()
        if done:
            return
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"metadata", "fills"}.issubset(tables):
            return
        old_meta = db.execute("SELECT value FROM metadata WHERE key='initial_cash'").fetchone()
        if old_meta:
            initial = decimal(old_meta[0], "Legacy initial cash")
        else:
            initial = (requested or {}).get("GBP", Decimal("100.00"))
        if requested is not None and requested != {"GBP": initial}:
            raise ValueError("Legacy ledgers can only be opened with their original GBP initial cash")
        db.execute("INSERT OR IGNORE INTO ledger_meta(key,value) VALUES('initial:GBP',?)", (str(initial),))
        columns = {row[1] for row in db.execute("PRAGMA table_info(fills)")}
        required = {"timestamp_utc", "market", "action", "price", "quantity", "notional", "fee", "realized_pnl"}
        if required.issubset(columns):
            old_rows = db.execute("SELECT timestamp_utc,market,action,price,quantity,notional,fee,realized_pnl,id FROM fills ORDER BY timestamp_utc,id").fetchall()
            for row in old_rows:
                base, quote = _market_currencies(row["market"])
                stamp = _timestamp(row["timestamp_utc"])
                price, quantity = decimal(row["price"]), decimal(row["quantity"])
                notional, fee = decimal(row["notional"]), decimal(row["fee"])
                action = str(row["action"]).upper()
                if action not in ("BUY", "SELL"):
                    raise ValueError("Cannot migrate unknown legacy fill action")
                db.execute("""INSERT INTO ledger_events(timestamp_utc,market,action,base_currency,quote_currency,price,quantity,notional,fee_amount,fee_currency,realized_pnl,pnl_currency,fill_id)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (stamp, row["market"], action, base, quote, str(price), str(quantity), str(notional),
                            str(fee), quote, str(decimal(row["realized_pnl"])), quote, f"legacy:{row['id']}"))
        db.execute("INSERT INTO ledger_meta(key,value) VALUES('legacy_migration','v1-imported; legacy fees assumed quote currency')")
        db.execute("INSERT OR REPLACE INTO ledger_meta(key,value) VALUES('schema_version','2')")

    def _replay(self, db: sqlite3.Connection, *, before: str | None = None) -> dict[str, Any]:
        balances = self._initial_balances(db)
        positions: dict[str, dict[str, Any]] = {}
        realized: dict[str, Decimal] = {}
        query = "SELECT * FROM ledger_events"
        args: tuple[Any, ...] = ()
        if before is not None:
            query += " WHERE timestamp_utc < ?"
            args = (before,)
        query += " ORDER BY timestamp_utc,id"
        for row in db.execute(query, args):
            base, quote = row["base_currency"], row["quote_currency"]
            price, qty = decimal(row["price"]), decimal(row["quantity"])
            notional, fee = decimal(row["notional"]), decimal(row["fee_amount"])
            fee_currency = row["fee_currency"]
            pos = positions.setdefault(row["market"], {"base_currency": base, "quote_currency": quote,
                                                          "units": ZERO, "cost_basis": ZERO})
            if pos["base_currency"] != base or pos["quote_currency"] != quote:
                raise ValueError("Market currency metadata changed within the ledger")
            if row["action"] == "BUY":
                balances[quote] = balances.get(quote, ZERO) - notional
                balances[base] = balances.get(base, ZERO) + qty
                pos["units"] += qty
                pos["cost_basis"] += notional
            else:
                balances[base] = balances.get(base, ZERO) - qty
                balances[quote] = balances.get(quote, ZERO) + notional
                pos["units"] -= qty
                if pos["units"] == 0:
                    pos["cost_basis"] = ZERO
                else:
                    # Remove average-cost basis for the quantity sold.
                    pos["cost_basis"] -= (pos["cost_basis"] / (pos["units"] + qty)) * qty
                pnl_currency = row["pnl_currency"]
                realized[pnl_currency] = realized.get(pnl_currency, ZERO) + decimal(row["realized_pnl"])
            balances[fee_currency] = balances.get(fee_currency, ZERO) - fee
            # Cost of quote-denominated fees is included in acquisition basis and
            # fees in base currency reduce inventory, handled by fee balance.
            if row["action"] == "BUY" and fee_currency == quote:
                pos["cost_basis"] += fee
            if row["action"] == "BUY" and fee_currency == base:
                pos["units"] -= fee
            if row["action"] == "SELL" and fee_currency == base:
                pos["units"] -= fee
            if row["action"] == "SELL" and fee_currency == quote:
                realized[quote] = realized.get(quote, ZERO) - fee
            if any(value < Decimal("-1e-24") for value in balances.values()):
                raise ValueError("Ledger replay produces a negative currency balance")
        return {"balances": balances, "positions": positions, "realized_pnl": realized}

    def balances(self) -> dict[str, Decimal]:
        with self._connect() as db:
            replay = self._replay(db)
        return {currency: amount for currency, amount in replay["balances"].items() if amount != 0}

    def positions(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            positions = self._replay(db)["positions"]
        return {market: dict(position) for market, position in positions.items() if position["units"] != 0}

    def state(self, market: str) -> dict[str, Any]:
        base, quote = _market_currencies(market)
        with self._connect() as db:
            replay = self._replay(db)
        position = replay["positions"].get(market, {"units": ZERO, "cost_basis": ZERO})
        return {"cash": replay["balances"].get(quote, ZERO), "units": position["units"],
                "cost_basis": position["cost_basis"], "realized_pnl": replay["realized_pnl"].get(quote, ZERO),
                "base_currency": base, "quote_currency": quote, "balances": replay["balances"]}

    def execute(self, market: str, action: str, price: Any, quantity: Any,
                fee_rate: Any = ZERO, *, timestamp: str | datetime | None = None,
                base_currency: str | None = None, quote_currency: str | None = None,
                fee_amount: Any | None = None, fee_currency: str | None = None,
                fill_id: str | None = None) -> dict[str, Any]:
        """Append a simulated fill. Repeated calls model partial fills."""
        base, quote = _market_currencies(market, base_currency, quote_currency)
        action = action.upper() if isinstance(action, str) else action
        if action not in ("BUY", "SELL"):
            raise ValueError("Action must be BUY or SELL")
        price_d, qty = decimal(price, "Price"), decimal(quantity, "Quantity")
        rate = decimal(fee_rate, "Fee rate")
        if price_d <= 0 or qty <= 0 or rate < 0:
            raise ValueError("Price/quantity must be positive and fee rate nonnegative")
        notional = price_d * qty
        fee_ccy = (fee_currency or quote).upper()
        fee = ((notional * rate).quantize(CENT, rounding=ROUND_CEILING)
               if fee_amount is None else decimal(fee_amount, "Fee amount"))
        if fee < 0:
            raise ValueError("Fee amount cannot be negative")
        stamp = _timestamp(timestamp)
        if action == "SELL" and fee_ccy == base:
            base_debit = qty + fee
        else:
            base_debit = qty if action == "SELL" else ZERO
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT timestamp_utc FROM ledger_events ORDER BY timestamp_utc DESC,id DESC LIMIT 1").fetchone()
            if latest and stamp < latest[0]:
                db.execute("ROLLBACK")
                raise ValueError("Backdated fills are not allowed")
            replay = self._replay(db)
            balances = replay["balances"]
            position = replay["positions"].get(market, {"units": ZERO, "cost_basis": ZERO})
            if action == "SELL" and base_debit > position["units"]:
                db.execute("ROLLBACK")
                raise ValueError("Cannot sell more units than the paper position")
            changes: dict[str, Decimal] = {}
            if action == "BUY":
                changes[quote] = changes.get(quote, ZERO) - notional
                changes[base] = changes.get(base, ZERO) + qty
            else:
                changes[base] = changes.get(base, ZERO) - qty
                changes[quote] = changes.get(quote, ZERO) + notional
            changes[fee_ccy] = changes.get(fee_ccy, ZERO) - fee
            after = {currency: amount + changes.get(currency, ZERO) for currency, amount in balances.items()}
            for currency, delta in changes.items():
                after.setdefault(currency, ZERO)
                if after[currency] < ZERO:
                    db.execute("ROLLBACK")
                    raise ValueError(f"Insufficient {currency} balance for paper fill and fees")
            realized = ZERO
            if action == "SELL":
                avg = position["cost_basis"] / position["units"] if position["units"] else ZERO
                basis_sold = avg * qty
                realized = notional - basis_sold
                if fee_ccy == quote:
                    realized -= fee
                elif fee_ccy == base:
                    realized -= fee * price_d
                db.execute("INSERT INTO ledger_meta(key,value) VALUES('last_pnl_currency',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (quote,))
            db.execute("""INSERT INTO ledger_events(timestamp_utc,market,action,base_currency,quote_currency,price,quantity,notional,fee_amount,fee_currency,realized_pnl,pnl_currency,fill_id)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (stamp, market, action, base, quote, str(price_d), str(qty), str(notional), str(fee),
                        fee_ccy, str(realized), quote, fill_id))
            db.execute("COMMIT")
        return {"timestamp_utc": stamp, "market": market, "action": action, "price": price_d,
                "quantity": qty, "notional": notional, "fee": fee, "fee_currency": fee_ccy,
                "realized_pnl": realized, "pnl_currency": quote, "fill_id": fill_id,
                "execution": "PAPER_SIMULATED"}

    def fills(self, *, market: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            query, params = "SELECT * FROM ledger_events", ()
            if market:
                query += " WHERE market=?"
                params = (market,)
            rows = db.execute(query + " ORDER BY timestamp_utc,id", params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("price", "quantity", "notional", "fee_amount", "realized_pnl"):
                item[key] = decimal(item[key])
            result.append(item)
        return result

    def daily_report(self, day: str, *, market: str | None = None,
                     mark_price: Any | None = None, market_prices: Mapping[str, Any] | None = None,
                     fx_rates: Mapping[str, Any] | None = None,
                     reporting_currency: str = "GBP") -> dict[str, Any]:
        """UTC daily activity and EOD replay. Verified equity requires complete marks.

        ``market_prices`` map market to quote-per-base marks; ``fx_rates`` map
        currency units into the reporting currency. GBP reports implicitly use
        GBP=1. Missing asset marks produce ``verified_equity=None`` and a
        clearly labeled provisional value from cost basis when FX is available.
        """
        try:
            report_day = date.fromisoformat(day)
            if report_day.isoformat() != day:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("Day must use YYYY-MM-DD") from exc
        if mark_price is not None:
            if not market:
                raise ValueError("market is required with mark_price")
            market_prices = dict(market_prices or {})
            market_prices[market] = mark_price
        prices = {symbol: decimal(value, "Market price") for symbol, value in (market_prices or {}).items()}
        if any(value <= 0 for value in prices.values()):
            raise ValueError("Market prices must be positive")
        rates = {currency.upper(): decimal(value, "FX rate") for currency, value in (fx_rates or {}).items()}
        rates[reporting_currency.upper()] = Decimal("1")
        if any(value <= 0 for value in rates.values()):
            raise ValueError("FX rates must be positive")
        end = datetime.combine(report_day + timedelta(days=1), time.min, timezone.utc).isoformat(timespec="microseconds")
        start = datetime.combine(report_day, time.min, timezone.utc).isoformat(timespec="microseconds")
        with self._connect() as db:
            query = "SELECT * FROM ledger_events WHERE timestamp_utc>=? AND timestamp_utc<?"
            params: tuple[Any, ...] = (start, end)
            if market:
                query += " AND market=?"
                params += (market,)
            rows = db.execute(query, params).fetchall()
            snapshot = self._replay(db, before=end)
        fees: dict[str, Decimal] = {}
        realized: dict[str, Decimal] = {}
        buys: dict[str, Decimal] = {}
        sells: dict[str, Decimal] = {}
        for row in rows:
            quote = row["quote_currency"]
            if row["action"] == "BUY":
                buys[quote] = buys.get(quote, ZERO) + decimal(row["notional"])
            else:
                sells[quote] = sells.get(quote, ZERO) + decimal(row["notional"])
            fees[row["fee_currency"]] = fees.get(row["fee_currency"], ZERO) + decimal(row["fee_amount"])
            realized[quote] = realized.get(quote, ZERO) + decimal(row["realized_pnl"])
        report: dict[str, Any] = {"day_utc": day, "market": market, "fills": len(rows),
            "fees_by_currency": fees, "realized_pnl_by_currency": realized,
            "buy_notional_by_currency": buys, "sell_notional_by_currency": sells,
            "balances_eod": snapshot["balances"],
            "positions_eod": {symbol: dict(p) for symbol, p in snapshot["positions"].items() if p["units"] != ZERO}}
        if market:
            report["buy_notional"] = sum(buys.values(), ZERO)
            report["sell_notional"] = sum(sells.values(), ZERO)
            report["fees"] = sum(fees.values(), ZERO)
            report["realized_pnl_after_fees"] = snapshot["realized_pnl"].get(_market_currencies(market)[1], ZERO)
            state = report["positions_eod"].get(market, {"units": ZERO, "cost_basis": ZERO})
            report["position_units"] = state["units"]
            report["cash"] = snapshot["balances"].get(_market_currencies(market)[1], ZERO)
        # Derive currency marks from supplied market marks for each open position.
        currency_rates = dict(rates)
        all_marked = True
        provisional_total = ZERO
        verified_total = ZERO
        missing = []
        base_currencies = set()
        for symbol, pos in snapshot["positions"].items():
            if pos["units"] == ZERO:
                continue
            base_currencies.add(pos["base_currency"])
            mark = prices.get(symbol)
            quote_rate = currency_rates.get(pos["quote_currency"])
            if mark is not None and quote_rate is not None:
                currency_rates[pos["base_currency"]] = mark * quote_rate
            else:
                all_marked = False
                missing.append(symbol)
                if quote_rate is not None:
                    provisional_total += pos["cost_basis"] * quote_rate
        for currency, amount in snapshot["balances"].items():
            if amount == ZERO or currency in base_currencies:
                continue
            rate = currency_rates.get(currency)
            if rate is None:
                all_marked = False
                missing.append(f"currency:{currency}")
            else:
                verified_total += amount * rate
                provisional_total += amount * rate
        for symbol, pos in snapshot["positions"].items():
            if pos["units"] == ZERO:
                continue
            mark = prices.get(symbol)
            quote_rate = currency_rates.get(pos["quote_currency"])
            if mark is not None and quote_rate is not None:
                verified_total += pos["units"] * mark * quote_rate
                provisional_total += pos["units"] * mark * quote_rate
            elif quote_rate is not None:
                provisional_total += pos["cost_basis"] * quote_rate
        report.update({"reporting_currency": reporting_currency.upper(),
                       "verified_equity": verified_total if all_marked else None,
                       "provisional_equity": provisional_total if not all_marked else None,
                       "unpriced": sorted(set(missing))})
        if market:
            pos = snapshot["positions"].get(market, {"units": ZERO, "cost_basis": ZERO, "quote_currency": _market_currencies(market)[1]})
            mark = prices.get(market)
            quote_rate = rates.get(pos["quote_currency"])
            report["unrealized_pnl"] = (pos["units"] * mark - pos["cost_basis"] if mark is not None else None)
            if mark is not None and quote_rate is not None:
                report["market_valued_position"] = pos["units"] * mark
            else:
                report["provisional_position_value"] = pos["cost_basis"]
        return report

    def migrate_legacy(self) -> dict[str, Any]:
        """Return migration status/details for an existing v1 ledger."""
        with self._connect() as db:
            marker = db.execute("SELECT value FROM ledger_meta WHERE key='legacy_migration'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
            return {"migrated": marker is not None, "details": marker[0] if marker else None,
                    "imported_fills": count, "numeric_storage": "TEXT"}
