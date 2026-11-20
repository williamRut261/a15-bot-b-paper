# VPS deployment — deferred deliberately

This GitHub repository is a **staging location** only. Do not enable continuous GitHub Actions polling or any GitHub Actions deployment.

## Before deploying Bot B

1. Verify original-Composer output parity on actual historical target allocations. The original A15 JSON is included but the independently implemented evaluator is **not parity-verified**.
2. Provision a separate system user/service, an isolated persistent ledger directory and separate read-only market-data credentials. Never share production broker trading credentials.
3. Verify all 72 symbols have acceptable timely data using the desired IEX/SIP feed, without stale or placeholder prices.
4. Confirm account assumptions: $1,200 virtual starting capital; fractionals allowed in the simulator; $1 minimum notional and 0.5%-equity minimum rebalancing threshold; slippage 5 bps; no actual broker commissions (simulation only).
5. Start with `--observe-only` and inspect several sessions of allocations before using the `--allow-unverified-parity` simulated-fill mode.
6. Enable a separate process supervisor and health checks for Bot B; keep OG A15 unchanged.
7. Use a fresh ledger for each distinct experiment. Never reset a current ledger unnoticed.

## Foreground development run (after obtaining data credentials)

```bash
cd a15-bot-b-paper
python restore_strategy.py
export APCA_API_KEY_ID=...  # set in your shell or secrets manager, NEVER in Git
export APCA_API_SECRET_KEY=...
python run_realtime_bot_b.py --observe-only --capital 1200 --poll-seconds 15

# LOCAL simulated orders only, only after reviewing the caveats:
python run_realtime_bot_b.py --allow-unverified-parity --capital 1200 --poll-seconds 15
```

The runner uses authenticated `GET` requests for market data, but makes **no actual trading/broker order calls**. `on fill` refers to a simulated local fill event. An actual broker paper execution layer would be a separate future enhancement.