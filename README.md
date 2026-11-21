# Bob-2 — Kraken trading research manager

Paper-first trading experiment for a £100 maximum initial budget.

## Safety
- Default mode is paper; live orders are not implemented.
- No deposits, withdrawals, margin, leverage or derivatives.
- Never grant withdrawal API permissions or commit secrets.
- Loss of the full £100 remains possible if live trading is later enabled.

## Quick start
```bash
python -m unittest discover -s tests -v
python -m bob2.cli --market BTC/GBP --price 50000 --cash 100 --position 0
```

Prices are manually supplied examples, not live Kraken prices. No orders are placed.

## Future development
Read-only Kraken market data, persistence, fee/spread modelling, backtesting, daily reports, and separately reviewed live execution safeguards.
