"""Command-line demo for the isolated, local paper signal engine."""
import argparse
import json

try:
    from .paper_ledger import PaperLedger
    from .paper_trader import PaperTrader
except ImportError:
    from paper_ledger import PaperLedger
    from paper_trader import PaperTrader


def main():
    parser = argparse.ArgumentParser(description="Bob-2 local paper signals and simulated ledger; never places orders")
    parser.add_argument("--market", default="BTC/GBP")
    parser.add_argument("--prices", required=True, help="Comma-separated closes, oldest first (illustrative unless sourced separately)")
    parser.add_argument("--cash", type=float, default=100.0, help="Initial paper cash; must match an existing ledger")
    parser.add_argument("--ledger", default="~/.bob2/paper_ledger.sqlite3")
    parser.add_argument("--fee-rate", type=float, default=0.0026)
    parser.add_argument("--daily-pnl", type=float, default=0.0)
    parser.add_argument("--daily-report", action="store_true")
    args = parser.parse_args()
    closes = [float(value.strip()) for value in args.prices.split(",") if value.strip()]
    ledger = PaperLedger(args.ledger, args.cash)
    trader = PaperTrader(ledger)
    # CLI fee customization is explicit and remains entirely paper-only.
    from risk import Limits
    trader.limits = Limits(estimated_fee_rate=args.fee_rate)
    result = trader.evaluate(args.market, closes, daily_pnl_gbp=args.daily_pnl)
    if args.daily_report:
        from datetime import datetime, timezone
        result["daily_report"] = ledger.daily_report(datetime.now(timezone.utc).date().isoformat(), market=args.market, mark_price=closes[-1])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
