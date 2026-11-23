"""SQLite-backed paper ledger. It cannot connect to an exchange or place orders."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
from typing import Any


def _fee_cents(notional: float, rate: float) -> float:
    return math.ceil(notional * rate * 100.0 - 1e-10) / 100.0


def _timestamp(value: str | None = None) -> str:
    stamp = value or datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Timestamp must be ISO-8601 with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


class PaperLedger:
    """Append-only simulated fills and reproducible account state in SQLite."""

    def __init__(self, path: str | os.PathLike[str], initial_cash: float):
        if not math.isfinite(initial_cash) or initial_cash < 0:
            raise ValueError("Initial cash must be finite and nonnegative")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initial_cash = round(float(initial_cash), 2)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                market TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
                price REAL NOT NULL CHECK(price > 0),
                quantity REAL NOT NULL CHECK(quantity > 0),
                notional REAL NOT NULL,
                fee REAL NOT NULL,
                realized_pnl REAL NOT NULL
            )""")
            row = db.execute("SELECT value FROM metadata WHERE key='initial_cash' ").fetchone()
            if row is None:
                db.execute("INSERT INTO metadata(key, value) VALUES('initial_cash', ?)", (str(self._initial_cash),))
            elif not math.isclose(float(row[0]), self._initial_cash, rel_tol=0, abs_tol=0.005):
                raise ValueError("Initial cash does not match the existing ledger")
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
    def _state(db: sqlite3.Connection, market: str) -> dict[str, float]:
        initial = float(db.execute("SELECT value FROM metadata WHERE key='initial_cash'").fetchone()[0])
        cash = initial
        units = 0.0
        cost_basis = 0.0
        realized = 0.0
        rows = db.execute("SELECT action, quantity, notional, fee, realized_pnl FROM fills ORDER BY id").fetchall()
        for row in rows:
            quantity, notional, fee = float(row["quantity"]), float(row["notional"]), float(row["fee"])
            if row["action"] == "BUY":
                cash -= notional + fee
                if row["market"] == market:
                    units += quantity
                    cost_basis += notional + fee
            else:
                cash += notional - fee
                realized += float(row["realized_pnl"])
                if row["market"] == market:
                    average_cost = cost_basis / units if units else 0.0
                    cost_basis -= average_cost * quantity
                    units -= quantity
                    if abs(units) < 1e-12:
                        units, cost_basis = 0.0, 0.0
        return {"cash": round(cash, 8), "units": max(0.0, units), "cost_basis": max(0.0, cost_basis), "realized_pnl": realized}

    def state(self, market: str) -> dict[str, float]:
        with self._connect() as db:
            return self._state(db, market)

    def execute(self, market: str, action: str, price: float, quantity: float, fee_rate: float, *, timestamp: str | None = None) -> dict[str, Any]:
        """Record one simulated fill if affordable; never contacts an exchange."""
        if not market or action not in ("BUY", "SELL"):
            raise ValueError("Market and BUY/SELL action are required")
        if not all(math.isfinite(value) for value in (price, quantity, fee_rate)) or price <= 0 or quantity <= 0 or fee_rate < 0:
            raise ValueError("Price/quantity must be positive and fee rate nonnegative")
        stamp = _timestamp(timestamp)
        notional = round(price * quantity, 8)
        fee = _fee_cents(notional, fee_rate)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db, market)
            if action == "BUY":
                if notional + fee > state["cash"] + 1e-8:
                    db.execute("ROLLBACK")
                    raise ValueError("Insufficient paper cash for notional plus fees")
                realized = 0.0
            else:
                if quantity > state["units"] + 1e-10:
                    db.execute("ROLLBACK")
                    raise ValueError("Cannot sell more units than the paper position")
                average_cost = state["cost_basis"] / state["units"] if state["units"] else 0.0
                realized = round(notional - fee - average_cost * quantity, 8)
            db.execute("""INSERT INTO fills(timestamp_utc, market, action, price, quantity, notional, fee, realized_pnl)
                          VALUES(?,?,?,?,?,?,?,?)""", (stamp, market, action, price, quantity, notional, fee, realized))
            db.execute("COMMIT")
        return {"timestamp_utc": stamp, "market": market, "action": action, "price": price,
                "quantity": quantity, "notional": notional, "fee": fee, "realized_pnl": realized,
                "execution": "PAPER_SIMULATED"}

    def fills(self, *, market: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if market:
                rows = db.execute("SELECT * FROM fills WHERE market=? ORDER BY id", (market,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM fills ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    def daily_report(self, day: str, *, market: str | None = None, mark_price: float | None = None) -> dict[str, Any]:
        """Summarize UTC-day activity; optional mark adds unrealized P&L/equity."""
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise ValueError("Day must use YYYY-MM-DD") from exc
        if mark_price is not None and (not math.isfinite(mark_price) or mark_price <= 0):
            raise ValueError("Mark price must be positive and finite")
        query = "SELECT action, notional, fee, realized_pnl FROM fills WHERE substr(timestamp_utc,1,10)=?"
        params: tuple[Any, ...] = (day,)
        if market:
            query += " AND market=?"
            params += (market,)
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
            fills = len(rows)
            buys = sum(float(r["notional"]) for r in rows if r["action"] == "BUY")
            sells = sum(float(r["notional"]) for r in rows if r["action"] == "SELL")
            fees = sum(float(r["fee"]) for r in rows)
            realized = sum(float(r["realized_pnl"]) for r in rows)
            result: dict[str, Any] = {
                "day_utc": day, "market": market, "fills": fills, "buy_notional": round(buys, 8),
                "sell_notional": round(sells, 8), "fees": round(fees, 8),
                "realized_pnl_after_fees": round(realized, 8),
            }
            if mark_price is not None and market:
                state = self._state(db, market)
                unrealized = state["units"] * mark_price - state["cost_basis"]
                result.update({"cash": state["cash"], "position_units": state["units"],
                               "mark_price": mark_price, "unrealized_pnl": round(unrealized, 8),
                               "equity": round(state["cash"] + state["units"] * mark_price, 8)})
            return result
