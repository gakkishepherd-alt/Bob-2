# Bob-2 — Kraken trading research manager

Paper-first trading experiment. The repository currently accepts manually supplied account values only; it does not connect to Kraken or place orders.

## Capital allocation and safety

- Allocation policy can use up to 100% of actual unreserved cash; realised profits may be reinvested when reflected in the account cash balance.
- Available funds are account cash less cash reserved for open orders or other commitments. The configurable estimated fee is reserved before calculating affordable buy notional, and configurable minimum order sizes are enforced.
- Allocation fraction, estimated fee rate, minimum order size and daily-loss limit are configurable through `risk.Limits`.
- The existing daily-loss safeguard remains active. An independent emergency trading halt is also available through `Limits(emergency_halt=True)`.
- Never deposit additional funds or initiate withdrawals/transfers. No borrowing, margin, leverage, futures or derivatives are supported.
- Live execution is disabled and no exchange orders are implemented. The capacity calculation is a local policy calculation, not an account-balance or open-order feed; callers must supply current verified cash and reservations. Do not treat it as authorization to trade.
- Never grant withdrawal API permissions or commit secrets. Full loss of allocated funds is possible if live trading is separately developed and approved in future.

## Tests

From the repository root:

```bash
python -m unittest -v test_manager
```

## Paper CLI

Prices and balances are manually supplied examples, not live Kraken values; no orders are placed. Existing CLI example:

```bash
python -m bob2.cli --market BTC/GBP --price 50000 --cash 100 --position 0
```

## Future development

Read-only Kraken market data, verified balance/open-order ingestion, persistent transaction and portfolio ledgers, fee/spread modelling, backtesting, daily reports, and separately reviewed live execution safeguards.
