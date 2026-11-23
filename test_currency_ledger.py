import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal

from paper_ledger import PaperLedger


class CurrencyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "ledger.sqlite3")
        self.ledger = PaperLedger(self.path, initial_balances={"GBP": Decimal("1000.00000001"), "USD": Decimal("20")})

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_decimal_storage_and_fractional_partial_fills(self):
        one = self.ledger.execute("BTC/GBP", "BUY", Decimal("0.123456789123"), Decimal("0.000123456789"),
                                  fee_amount=Decimal("0.00000001"), fee_currency="GBP", timestamp="2026-03-01T10:00:00Z", fill_id="order-a")
        self.ledger.execute("BTC/GBP", "BUY", Decimal("0.123456789123"), Decimal("0.000123456789"),
                            fee_amount=Decimal("0.00000001"), fee_currency="GBP", timestamp="2026-03-01T10:00:00Z", fill_id="order-a")
        self.assertEqual(one["notional"], Decimal("0.000015241578765375706047"))
        self.assertEqual(self.ledger.state("BTC/GBP")["units"], Decimal("0.000246913578"))
        self.assertEqual(len(self.ledger.fills(market="BTC/GBP")), 2)
        with sqlite3.connect(self.path) as db:
            types = {row[1]: row[2] for row in db.execute("PRAGMA table_info(ledger_events)")}
        for col in ("price", "quantity", "notional", "fee_amount", "realized_pnl"):
            self.assertEqual(types[col].upper(), "TEXT")

    def test_backdated_fill_rejected_and_equal_timestamp_sequence_is_stable(self):
        self.ledger.execute("BTC/GBP", "BUY", "10", "1", fee_amount="0", timestamp="2026-03-01T10:00:00Z")
        self.ledger.execute("BTC/GBP", "BUY", "11", "1", fee_amount="0", timestamp="2026-03-01T10:00:00Z")
        with self.assertRaisesRegex(ValueError, "Backdated"):
            self.ledger.execute("ETH/USD", "BUY", "2", "1", fee_amount="0", timestamp="2026-03-01T09:59:59Z")
        self.assertEqual([x["price"] for x in self.ledger.fills()], [Decimal("10"), Decimal("11")])

    def test_currency_balances_and_fee_in_base_quote_and_third_currency(self):
        self.ledger.execute("BTC/GBP", "BUY", "10", "2", fee_amount="0.25", fee_currency="GBP", timestamp="2026-03-01T10:00:00Z")
        self.ledger.execute("ETH/USD", "BUY", "5", "1", fee_amount="0.1", fee_currency="ETH", timestamp="2026-03-01T10:01:00Z")
        self.assertEqual(self.ledger.balances()["GBP"], Decimal("979.75000001"))
        self.assertEqual(self.ledger.balances()["USD"], Decimal("15"))
        self.assertEqual(self.ledger.balances()["ETH"], Decimal("0.9"))
        self.assertEqual(self.ledger.positions()["ETH/USD"]["units"], Decimal("0.9"))

    def test_fee_currency_balance_and_oversell_are_enforced(self):
        self.ledger.execute("BTC/GBP", "BUY", "10", "1", fee_amount="0", timestamp="2026-03-01T10:00:00Z")
        with self.assertRaisesRegex(ValueError, "Insufficient USD"):
            self.ledger.execute("BTC/GBP", "SELL", "12", "0.5", fee_amount="21", fee_currency="USD", timestamp="2026-03-01T10:01:00Z")
        with self.assertRaisesRegex(ValueError, "position"):
            self.ledger.execute("BTC/GBP", "SELL", "12", "1.1", fee_amount="0", timestamp="2026-03-01T10:01:00Z")

    def test_mixed_currency_daily_report_and_missing_market_marks(self):
        self.ledger.execute("BTC/GBP", "BUY", "10", "2", fee_amount="0", timestamp="2026-03-01T10:00:00Z")
        self.ledger.execute("ETH/USD", "BUY", "5", "1", fee_amount="0", timestamp="2026-03-01T10:01:00Z")
        report = self.ledger.daily_report("2026-03-01", market_prices={"BTC/GBP": "12"}, fx_rates={"USD": "0.8"})
        self.assertIsNone(report["verified_equity"])
        self.assertIn("ETH/USD", report["unpriced"])
        self.assertEqual(report["provisional_equity"], Decimal("1020.00000001"))
        fully_marked = self.ledger.daily_report("2026-03-01", market_prices={"BTC/GBP": "12", "ETH/USD": "6"}, fx_rates={"USD": "0.8"})
        self.assertEqual(fully_marked["verified_equity"], Decimal("1020.80000001"))
        self.assertIsNone(fully_marked["provisional_equity"])

    def test_historical_eod_replay_excludes_later_fills(self):
        self.ledger.execute("BTC/GBP", "BUY", "10", "2", fee_amount="0", timestamp="2026-03-01T10:00:00Z")
        self.ledger.execute("BTC/GBP", "SELL", "12", "2", fee_amount="0", timestamp="2026-03-02T10:00:00Z")
        report = self.ledger.daily_report("2026-03-01", market="BTC/GBP", mark_price="11", fx_rates={"USD": "0.8"})
        self.assertEqual(report["position_units"], Decimal("2"))
        self.assertEqual(report["balances_eod"]["GBP"], Decimal("980.00000001"))
        self.assertAlmostEqual(report["unrealized_pnl"], Decimal("2"))
        self.assertEqual(report["fills"], 1)

    def test_migrates_v1_real_schema_preserves_fill_and_documents_quote_fee(self):
        legacy_path = os.path.join(self.temp.name, "legacy.sqlite3")
        with sqlite3.connect(legacy_path) as db:
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT INTO metadata VALUES('initial_cash','100.0')")
            db.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY,timestamp_utc TEXT,market TEXT,action TEXT,price REAL,quantity REAL,notional REAL,fee REAL,realized_pnl REAL)")
            db.execute("INSERT INTO fills VALUES(1,'2026-03-01T10:00:00+00:00','BTC/GBP','BUY',10.0,1.0,10.0,0.1,0.0)")
        migrated = PaperLedger(legacy_path, 100)
        status = migrated.migrate_legacy()
        self.assertTrue(status["migrated"])
        self.assertEqual(status["imported_fills"], 1)
        self.assertEqual(migrated.balances()["GBP"], Decimal("89.9"))
        self.assertEqual(migrated.balances()["BTC"], Decimal("1.0"))
        self.assertEqual(migrated.fills()[0]["fee_currency"], "GBP")


if __name__ == "__main__":
    unittest.main()
