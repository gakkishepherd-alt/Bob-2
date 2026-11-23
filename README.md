# Bob-2 — Kraken trading research manager

Paper-first trading experiment. Kraken integration in this branch is read-only and no live orders are implemented.

## Capital allocation and safety

- Allocation policy can use up to 100% of actual unreserved cash; realised profits may be reinvested when reflected in the account cash balance.
- Available funds are account cash less cash reserved for open orders or other commitments. The configurable estimated fee is reserved before calculating affordable buy notional, and configurable minimum order sizes are enforced.
- Allocation fraction, estimated fee rate, minimum order size and daily-loss limit are configurable through `risk.Limits`.
- The existing daily-loss safeguard remains active. An independent emergency trading halt is also available through `Limits(emergency_halt=True)`.
- The system has no deposit, withdrawal, transfer, borrowing, margin, leverage, futures, derivatives or order-placement methods. Kraken private endpoints are allowlisted to balance/open-order/trade-history queries only.
- Live execution is disabled. Read-only credentials cannot place orders; do not enable trading permission on the Kraken API key.

## Read-only Kraken setup

The adapter uses the Kraken REST API over HTTPS. Public ticker, OHLC, asset-pair, order-book, and system-status calls do not require credentials. Authenticated calls are limited in code to `Balance`, `OpenOrders`, and `TradesHistory`.

1. Create a Kraken API key with **query/read permissions only**: query funds, open orders, and closed orders/trades as needed. Do not enable trade, deposit, withdrawal, transfer, or other write permissions. Enable IP restrictions where available.
2. Provide the key and Kraken's base64-encoded private key through the process environment or your deployment secrets manager. Never paste credentials into chat, source files, command-line arguments, logs, or commit history.

   ```sh
   export KRAKEN_API_KEY='provided-by-your-secret-store'
   export KRAKEN_API_SECRET='provided-by-your-secret-store'
   ```

3. Use a least-privilege environment and rotate/revoke credentials if exposed. Keep `.env` files out of version control; the client reads environment variables only when a private query is invoked.

## Tests and CI

Run locally with:

```bash
python -m unittest discover -v
```

GitHub Actions runs this command on pushes and pull requests using Python 3.11, 3.12 and 3.13. The adapter tests use mocked responses and make no Kraken network calls.

The private retry path constructs and signs a new request on every attempt with a fresh strictly increasing nonce. Rate-limited responses are retried with bounded backoff; authentication failures are surfaced without retry. Ambiguous network failures are not retried.

## Existing paper CLI

Prices and balances supplied to the CLI are examples; they are not live Kraken values and no orders are placed.

```bash
python -m bob2.cli --market BTC/GBP --price 50000 --cash 100 --position 0
```

## Outstanding limitations

This adapter does not provide websocket feeds, persistence, balance-to-portfolio reconciliation, GBP market eligibility checks, paper order simulation, backtesting, or automated agent coordination. Kraken API permissions must be configured and independently audited; code-level endpoint restrictions do not replace venue-side read-only key permissions. Do not use any output as trade authorization. Live execution remains absent and disabled.
