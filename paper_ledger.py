"""Currency-aware, Decimal-exact SQLite paper ledger; no exchange execution.

New financial values are stored as decimal TEXT. Legacy v1 REAL values are
migrated once using their available decimal strings; legacy fees are explicitly
assumed to have been charged in the trade's quote currency.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

CENT = Decimal("0.01")
ZERO = Decimal("0")


def decimal(value: Any, label: str = "value") -> Decimal:
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
    """Append-only paper fills with replayed multi-currency balances/positions.

    ``initial_cash`` remains a GBP-compatible shorthand. New ledgers may use
    ``initial_balances``. Backdated fills are rejected; equal timestamps replay
    in insertion order. Repeated fills can use the same fill_id for partial fills.
    """
    def __init__(self, path: str | os.PathLike[str], initial_cash: Any | None = None,
                 *, initial_balances: Mapping[str, Any] | None = None):
        if initial_cash is not None and initial_balances is not None:
            raise ValueError("Use initial_cash or initial_balances, not both")
        requested: dict[str, Decimal] | None = None
        if initial_balances is not None:
            requested = {str(c).upper(): decimal(v, "Initial balance") for c, v in initial_balances.items()}
            if any(not c or v < 0 for c, v in requested.items()):
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
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_utc TEXT NOT NULL,
                market TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
                base_currency TEXT NOT NULL, quote_currency TEXT NOT NULL,
                price TEXT NOT NULL, quantity TEXT NOT NULL, notional TEXT NOT NULL,
                fee_amount TEXT NOT NULL, fee_currency TEXT NOT NULL,
                realized_pnl TEXT NOT NULL, pnl_currency TEXT NOT NULL, fill_id TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ledger_events_time ON ledger_events(timestamp_utc,id)")
            self._migrate_legacy(db, requested)
            saved = self._initial_balances(db)
            if not saved:
                defaults = requested or {"GBP": Decimal("100.00")}
                for currency, amount in sorted(defaults.items()):
                    db.execute("INSERT INTO ledger_meta(key,value) VALUES(?,?)", (f"initial:{currency}", str(amount)))
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
        return {r["key"].split(":", 1)[1]: decimal(r["value"], "Stored initial balance")
                for r in db.execute("SELECT key,value FROM ledger_meta WHERE key LIKE 'initial:%'")}

    def _migrate_legacy(self, db: sqlite3.Connection, requested: dict[str, Decimal] | None) -> None:
        if db.execute("SELECT value FROM ledger_meta WHERE key='legacy_migration'").fetchone():
            return
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"metadata", "fills"}.issubset(tables):
            return
        old_meta = db.execute("SELECT value FROM metadata WHERE key='initial_cash'").fetchone()
        initial = decimal(old_meta[0], "Legacy initial cash") if old_meta else (requested or {}).get("GBP", Decimal("100.00"))
        if requested is not None and requested != {"GBP": initial}:
            raise ValueError("Legacy ledgers can only be opened with their original GBP initial cash")
        db.execute("INSERT OR IGNORE INTO ledger_meta(key,value) VALUES('initial:GBP',?)", (str(initial),))
        columns = {r[1] for r in db.execute("PRAGMA table_info(fills)")}
        required = {"timestamp_utc", "market", "action", "price", "quantity", "notional", "fee", "realized_pnl"}
        if required.issubset(columns):
            for row in db.execute("SELECT timestamp_utc,market,action,price,quantity,notional,fee,realized_pnl,id FROM fills ORDER BY timestamp_utc,id").fetchall():
                base, quote = _market_currencies(row["market"])
                vals = [decimal(row[k]) for k in ("price", "quantity", "notional", "fee", "realized_pnl")]
                db.execute("""INSERT INTO ledger_events(timestamp_utc,market,action,base_currency,quote_currency,price,quantity,notional,fee_amount,fee_currency,realized_pnl,pnl_currency,fill_id)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (_timestamp(row["timestamp_utc"]), row["market"], str(row["action"]).upper(), base, quote,
                            *(str(v) for v in vals[:3]), str(vals[3]), quote, str(vals[4]), quote, f"legacy:{row['id']}"))
        db.execute("INSERT INTO ledger_meta(key,value) VALUES('legacy_migration','v1 imported; legacy fees assumed quote currency')")
        db.execute("INSERT OR REPLACE INTO ledger_meta(key,value) VALUES('schema_version','2')")

    def _replay(self, db: sqlite3.Connection, *, before: str | None = None) -> dict[str, Any]:
        balances = self._initial_balances(db)
        positions: dict[str, dict[str, Any]] = {}
        realized: dict[str, Decimal] = {}
        query, args = "SELECT * FROM ledger_events", ()
        if before is not None:
            query, args = query + " WHERE timestamp_utc < ?", (before,)
        for row in db.execute(query + " ORDER BY timestamp_utc,id", args):
            base, quote = row["base_currency"], row["quote_currency"]
            price, qty, notional, fee = (decimal(row[k]) for k in ("price", "quantity", "notional", "fee_amount"))
            pos = positions.setdefault(row["market"], {"base_currency": base, "quote_currency": quote, "units": ZERO, "cost_basis": ZERO})
            if (pos["base_currency"], pos["quote_currency"]) != (base, quote):
                raise ValueError("Market currency metadata changed within the ledger")
            if row["action"] == "BUY":
                balances[quote] = balances.get(quote, ZERO) - notional
                balances[base] = balances.get(base, ZERO) + qty
                pos["units"] += qty
                pos["cost_basis"] += notional
            else:
                balances[base] = balances.get(base, ZERO) - qty
                balances[quote] = balances.get(quote, ZERO) + notional
                prior_units = pos["units"]
                if qty > prior_units:
                    raise ValueError("Ledger replay found a sell exceeding the position")
                basis_sold = (pos["cost_basis"] / prior_units * qty) if prior_units else ZERO
                pos["units"] -= qty
                pos["cost_basis"] -= basis_sold
                realized[quote] = realized.get(quote, ZERO) + decimal(row["realized_pnl"])
            fee_currency = row["fee_currency"]
            balances[fee_currency] = balances.get(fee_currency, ZERO) - fee
            if row["action"] == "BUY" and fee_currency == quote:
                pos["cost_basis"] += fee
            if row["action"] == "BUY" and fee_currency == base:
                pos["units"] -= fee
            if row["action"] == "SELL" and fee_currency == base:
                pos["units"] -= fee
            if any(value < ZERO for value in balances.values()):
                raise ValueError("Ledger replay produces a negative currency balance")
        return {"balances": balances, "positions": positions, "realized_pnl": realized}

    def balances(self) -> dict[str, Decimal]:
        with self._connect() as db:
            values = self._replay(db)["balances"]
        return {k: v for k, v in values.items() if v != ZERO}

    def positions(self) -> dict[str, dict[str, Any]]:
        with self._connect() as db:
            values = self._replay(db)["positions"]
        return {k: dict(v) for k, v in values.items() if v["units"] != ZERO}

    def state(self, market: str) -> dict[str, Any]:
        base, quote = _market_currencies(market)
        with self._connect() as db:
            replay = self._replay(db)
        pos = replay["positions"].get(market, {"units": ZERO, "cost_basis": ZERO})
        return {"cash": replay["balances"].get(quote, ZERO), "units": pos["units"],
                "cost_basis": pos["cost_basis"], "realized_pnl": replay["realized_pnl"].get(quote, ZERO),
                "base_currency": base, "quote_currency": quote, "balances": replay["balances"]}

    def execute(self, market: str, action: str, price: Any, quantity: Any, fee_rate: Any = ZERO, *,
                timestamp: str | datetime | None = None, base_currency: str | None = None,
                quote_currency: str | None = None, fee_amount: Any | None = None,
                fee_currency: str | None = None, fill_id: str | None = None) -> dict[str, Any]:
        """Append a simulated fill; repeated calls can represent partial fills."""
        base, quote = _market_currencies(market, base_currency, quote_currency)
        action = action.upper() if isinstance(action, str) else action
        if action not in ("BUY", "SELL"):
            raise ValueError("Action must be BUY or SELL")
        p, qty, rate = decimal(price, "Price"), decimal(quantity, "Quantity"), decimal(fee_rate, "Fee rate")
        if p <= 0 or qty <= 0 or rate < 0:
            raise ValueError("Price/quantity must be positive and fee rate nonnegative")
        notional = p * qty
        fee_ccy = (fee_currency or quote).upper()
        fee = (notional * rate).quantize(CENT, rounding=ROUND_CEILING) if fee_amount is None else decimal(fee_amount, "Fee amount")
        if fee < 0:
            raise ValueError("Fee amount cannot be negative")
        stamp = _timestamp(timestamp)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT timestamp_utc FROM ledger_events ORDER BY timestamp_utc DESC,id DESC LIMIT 1").fetchone()
            if latest and stamp < latest[0]:
                db.execute("ROLLBACK")
                raise ValueError("Backdated fills are not allowed")
            replay = self._replay(db)
            balances = replay["balances"]
            pos = replay["positions"].get(market, {"units": ZERO, "cost_basis": ZERO})
            if action == "SELL" and qty + (fee if fee_ccy == base else ZERO) > pos["units"]:
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
            for currency, delta in changes.items():
                if balances.get(currency, ZERO) + delta < ZERO:
                    db.execute("ROLLBACK")
                    raise ValueError(f"Insufficient {currency} balance for paper fill and fees")
            realized = ZERO
            if action == "SELL":
                avg = pos["cost_basis"] / pos["units"] if pos["units"] else ZERO
                realized = notional - avg * qty
                if fee_ccy == quote:
                    realized -= fee
                elif fee_ccy == base:
                    realized -= fee * p
            db.execute("""INSERT INTO ledger_events(timestamp_utc,market,action,base_currency,quote_currency,price,quantity,notional,fee_amount,fee_currency,realized_pnl,pnl_currency,fill_id)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (stamp, market, action, base, quote, str(p), str(qty), str(notional), str(fee), fee_ccy, str(realized), quote, fill_id))
            db.execute("COMMIT")
        return {"timestamp_utc": stamp, "market": market, "action": action, "price": p, "quantity": qty,
                "notional": notional, "fee": fee, "fee_currency": fee_ccy, "realized_pnl": realized,
                "pnl_currency": quote, "fill_id": fill_id, "execution": "PAPER_SIMULATED"}

    def fills(self, *, market: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            query, args = "SELECT * FROM ledger_events", ()
            if market:
                query, args = query + " WHERE market=?", (market,)
            rows = db.execute(query + " ORDER BY timestamp_utc,id", args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("price", "quantity", "notional", "fee_amount", "realized_pnl"):
                item[key] = decimal(item[key])
            result.append(item)
        return result

    def daily_report(self, day: str, *, market: str | None = None, mark_price: Any | None = None,
                     market_prices: Mapping[str, Any] | None = None, fx_rates: Mapping[str, Any] | None = None,
                     reporting_currency: str = "GBP") -> dict[str, Any]:
        """UTC daily activity and historical EOD state; verified equity needs all marks."""
        try:
            d = date.fromisoformat(day)
            if d.isoformat() != day:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("Day must use YYYY-MM-DD") from exc
        if mark_price is not None:
            if not market:
                raise ValueError("market is required with mark_price")
            market_prices = dict(market_prices or {})
            market_prices[market] = mark_price
        prices = {k: decimal(v, "Market price") for k, v in (market_prices or {}).items()}
        if any(v <= ZERO for v in prices.values()):
            raise ValueError("Market prices must be positive")
        rates = {k.upper(): decimal(v, "FX rate") for k, v in (fx_rates or {}).items()}
        rates[reporting_currency.upper()] = Decimal(1)
        if any(v <= ZERO for v in rates.values()):
            raise ValueError("FX rates must be positive")
        start = datetime.combine(d, time.min, timezone.utc).isoformat(timespec="microseconds")
        end = datetime.combine(d + timedelta(days=1), time.min, timezone.utc).isoformat(timespec="microseconds")
        with self._connect() as db:
            query, args = "SELECT * FROM ledger_events WHERE timestamp_utc>=? AND timestamp_utc<?", (start, end)
            if market:
                query, args = query + " AND market=?", args + (market,)
            rows = db.execute(query, args).fetchall()
            snap = self._replay(db, before=end)
        fees: dict[str, Decimal] = {}
        realized: dict[str, Decimal] = {}
        buys: dict[str, Decimal] = {}
        sells: dict[str, Decimal] = {}
        for r in rows:
            quote = r["quote_currency"]
            bucket = buys if r["action"] == "BUY" else sells
            bucket[quote] = bucket.get(quote, ZERO) + decimal(r["notional"])
            fees[r["fee_currency"]] = fees.get(r["fee_currency"], ZERO) + decimal(r["fee_amount"])
            realized[quote] = realized.get(quote, ZERO) + decimal(r["realized_pnl"])
        report: dict[str, Any] = {"day_utc": day, "market": market, "fills": len(rows),
            "fees_by_currency": fees, "realized_pnl_by_currency": realized,
            "buy_notional_by_currency": buys, "sell_notional_by_currency": sells,
            "balances_eod": snap["balances"],
            "positions_eod": {s: dict(p) for s, p in snap["positions"].items() if p["units"] != ZERO}}
        if market:
            quote = _market_currencies(market)[1]
            report.update({"buy_notional": sum(buys.values(), ZERO), "sell_notional": sum(sells.values(), ZERO),
                           "fees": sum(fees.values(), ZERO), "realized_pnl_after_fees": snap["realized_pnl"].get(quote, ZERO),
                           "cash": snap["balances"].get(quote, ZERO)})
            selected = report["positions_eod"].get(market, {"units": ZERO, "cost_basis": ZERO, "quote_currency": quote})
            report["position_units"] = selected["units"]
            report["unrealized_pnl"] = (selected["units"] * prices[market] - selected["cost_basis"] if market in prices else None)
        # Derive conversion of crypto base balances from marked pairs; quote FX
        # rates are explicit supplied rates. Unmarked positions use cost basis
        # once only and are strictly labeled provisional.
        currency_rates = dict(rates)
        positions = {s: p for s, p in snap["positions"].items() if p["units"] != ZERO}
        for symbol, pos in positions.items():
            if symbol in prices and pos["quote_currency"] in currency_rates:
                currency_rates[pos["base_currency"]] = prices[symbol] * currency_rates[pos["quote_currency"]]
        represented = {p["base_currency"] for p in positions.values()}
        valued_balances = ZERO
        provisional_balances = ZERO
        missing: list[str] = []
        all_marked = True
        for currency, amount in snap["balances"].items():
            if currency in represented:
                continue
            rate = currency_rates.get(currency)
            if rate is None:
                all_marked = False
                missing.append(f"currency:{currency}")
            else:
                valued_balances += amount * rate
                provisional_balances += amount * rate
        verified_assets = valued_balances
        provisional_assets = provisional_balances
        for symbol, pos in positions.items():
            rate = currency_rates.get(pos["quote_currency"])
            if symbol in prices and rate is not None:
                value = pos["units"] * prices[symbol] * rate
                verified_assets += value
                provisional_assets += value
            else:
                all_marked = False
                missing.append(symbol)
                if rate is not None:
                    provisional_assets += pos["cost_basis"] * rate
        report.update({"reporting_currency": reporting_currency.upper(),
                       "verified_equity": verified_assets if all_marked else None,
                       "provisional_equity": provisional_assets if not all_marked else None,
                       "unpriced": sorted(set(missing))})
        if all_marked:
            report["equity"] = verified_assets  # Legacy alias, only for fully verified values.
        return report

    def migrate_legacy(self) -> dict[str, Any]:
        with self._connect() as db:
            marker = db.execute("SELECT value FROM ledger_meta WHERE key='legacy_migration'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
            return {"migrated": marker is not None, "details": marker[0] if marker else None,
                    "imported_fills": count, "numeric_storage": "TEXT"}
