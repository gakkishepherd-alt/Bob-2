"""Manual paper decision demonstration; never connects to Kraken."""
import argparse
import json
from .manager import evaluate

def main():
    parser = argparse.ArgumentParser(description="Bob-2 paper-only decision runner")
    parser.add_argument("--market", default="BTC/GBP")
    parser.add_argument("--price", type=float, required=True, help="Manually supplied illustrative GBP price")
    parser.add_argument("--cash", type=float, default=100.0)
    parser.add_argument("--position", type=float, default=0.0, help="Marked GBP position value")
    parser.add_argument("--daily-pnl", type=float, default=0.0)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.market, args.price, args.cash, args.position, args.daily_pnl).as_dict(), indent=2))

if __name__ == "__main__":
    main()
