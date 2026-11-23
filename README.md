# Bob-2 — Kraken read-only trading research manager

Bob-2 is a paper-first research project. Kraken API support is **read-only**; no exchange order placement or live trading is implemented. The signal engine and ledger described below operate locally and simulate fills only.

## Explainable signal engine

`signal_engine.py` evaluates chronological closing prices using a fast/slow simple-moving-average comparison plus recent percentage momentum. A BUY requires both an uptrend and sufficiently positive momentum; a SELL requires both bearish conditions and an existing paper position. Otherwise it recommends HOLD. The result includes the indicator values and human-readable reasons. Defaults are a 5-close fast SMA, 20-close slow SMA, 3-interval momentum, and 0.25% threshold. These are transparent research heuristics, not investment advice or a guarantee of performance. `kraken_ohlc_closes` parses OHLC payloads already supplied to it; it does not fetch data.

## Paper trading, fees and reports

`PaperTrader` applies the existing cash-allocation, fee, daily-loss and emergency-halt controls before simulating a BUY. SELL signals close the held paper position; no leverage, shorting, or borrowing is supported. `PaperLedger` persists initial cash and append-only simulated fills in SQLite, calculating cash, holdings, average-cost basis, estimated fees (rounded up to a penny), realized P&L after fees, and optional mark-to-market unrealized P&L/equity. Daily reports aggregate fills, notionals, fees and realized P&L for a UTC date. Mark-to-market fields are included when a market and current mark price are provided; no historical price feed is implied.

Ledger files are local state and should not be committed. The default CLI ledger is `~/.bob2/paper_ledger.sqlite3`; its initial balance is fixed at creation and later CLI invocations must use the same `--cash` value.

```bash
python -m unittest discover -v
python -m bob2.paper_cli --market BTC/GBP --prices 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24 --cash 100 --daily-report
```

Prices passed on the command line are samples. Market data collection is separate; Kraken methods in `kraken.py` remain constrained to public market data and allowlisted private read-only queries. Do not grant trade permissions to a Kraken API key.

## Existing safeguards and Kraken adapter

Allocation fraction, estimated fee rate, minimum order size, daily-loss limit, and emergency halt are configurable through `risk.Limits`. Kraken credentials are read from environment variables only for private balance/open-order/trade-history queries. No order, deposit, withdrawal, transfer, borrowing, margin, leverage, futures, or derivatives operation is implemented.

Run the full test suite with `python -m unittest discover -v`. Kraken adapter tests use mocked responses and make no network calls.
