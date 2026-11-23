## Kraken spot market discovery (paper-only)

The `market_discovery.py` module reads the public Kraken `Assets` and `AssetPairs` metadata through the read-only adapter. It normalizes asset IDs and aliases, records Decimal order/cost minimums and price/quantity increments, and explains why markets are excluded. Public exchange status is not UK account eligibility: the discovery result stays fail-closed unless a current `EligibilityConfirmation` explicitly names the pair(s) for the account. Confirmations expire, snapshots expire, and a failed refresh clears the prior eligible snapshot.

This component only discovers metadata; it neither places orders nor proves regulatory/account eligibility. Confirmation must come from an authorized, current account-specific review. Execution remains paper-only.
