# A15 Bot B — Standalone intraday / on-fill LOCAL PAPER forward tester

## Status (2026-11-19)

**Implemented and offline-tested, not deployed or running, no authenticated live market data verified, Composer parity not yet established.** The runner *can* run without the existing A15 bot. It never makes brokerage trading/order API calls and cannot trade real money.

This is **only Bot B**. No duplicate OG A15 process is included and the existing VPS/services are unchanged.

## Intended behavior

1. At start of each US trading session, download **completed historical daily bars** for every symbol referenced in `original_a15.json` (57 allocation symbols plus 15 condition-only tickers, 72 total).
2. Every 15 seconds during US regular hours (configurable), fetch read-only, current-session market snapshots from Alpaca IEX or consolidated SIP.
3. Evaluate the **entire unchanged, 2,918-node A15 JSON** using the current observed price as a **partial, in-progress daily bar** for RSI, momentum, SMA, sub-strategy rank, etc. No future daily bar values.
4. Compare A15 target allocations to the locally simulated virtual holdings. If sufficiently different, simulate one fill at current mark +/- configured slippage.
5. **Immediately reevaluate the full 2,918-node A15 tree after that fill**, then fill the next required paper order, reevaluate, etc. Stop at target convergence or safety cap. Continue watching market signals; changing signals may cause **multiple entry, exit or rotation trades within one trading day**.
6. Write the strategy SHA, paper fills, decisions, positions, equity and safety events to an append-only SQLite journal that persists across restarts.

The original JSON has not been modified. GitHub stores its original contents as a compressed, base64-encoded blob `original_a15.json.gz.b64`; `restore_strategy.py` verifies SHA-256 and reconstructs the exact JSON before use. Do not treat the encoded source as a different strategy. Default capital **$1,200**, 5 bps simulated slippage, 0 commissions, $1/0.5%-of-equity minimum adjustment, and 80 maximum fills per trading day. The min-order threshold is **$6 at $1,200 equity** (0.5%), so smaller target weights are not filled.

## Critical limitations

- **Not Composer parity-verified**: The included `a15_realtime_eval.py` evaluates all node *types*, all filters, every conditional and sub-strategy with independently implemented definitions. **Complex historical sub-strategy rank** is computed from a synthetic daily mark-to-market portfolio based on previous-day weights; this is a plausible implementation, not a verified duplicate of Composer's internal metric. Indicator warm-up, RSI implementation, corporate-action handling and rank ties can also differ from Composer. To trade local *paper* orders using this independent replica, explicitly pass `--allow-unverified-parity`. For signal inspection only, use `--observe-only`.
- **Local paper simulator**: virtual fills are immediate and based on the latest read price +/- slippage. These are **not Alpaca paper-account fills**, exchange fills or guaranteed executable prices. There is no broker order endpoint and no account API dependency. No real money is used.
- **Real-time means near-real-time polling** at configured interval, not tick-by-tick streaming. Order-fill recalculations happen immediately within the simulation event loop.
- **Market-data access is required** to start live-forward monitoring. Alpaca Basic provides only IEX venue data, not all consolidated US trades; SIP entitlement may be necessary for many A15 assets. Missing/unreliable data or insufficient history causes a fail-closed **NO_TRADE** log, never silent substitution of yesterday's last quote. The live data API has not been authenticated/tested in this environment.
- **Daily bar indicators with a partial intraday bar are an experimental reinterpretation of A15**, not exactly the scheduled daily Composer strategy. This creates more opportunities to trade and also more opportunities for whipsaws/fees.
- **No holiday calendar in the local runtime**. It uses weekday/NY clock window and then requires current-session market trades. It will fail closed on a market holiday.
- **No running deployment**: Process starts only when you run it on your own machine or server.

## Requirements

Python 3.11+ standard library. Alpaca market-data API key ID and secret (paper-account **data** keys are supported). Do not paste credentials in ChatGPT.

The script reads only these authenticated Alpaca endpoints:

- `GET https://data.alpaca.markets/v2/stocks/bars` (historical bars)
- `GET https://data.alpaca.markets/v2/stocks/snapshots` (read-only current price snapshots)

It has no POST/PUT/DELETE requests and no Alpaca trading endpoint.

## Setup and run (macOS/Linux)

```bash
cd a15-bot-b-paper
python restore_strategy.py
export APCA_API_KEY_ID='YOUR_ALPACA_MARKET_DATA_KEY_ID'
export APCA_API_SECRET_KEY='YOUR_ALPACA_MARKET_DATA_SECRET'

# 1. Safest initial audit: evaluate A15 on real data, log targets, DO NOT paper fill.
python run_realtime_bot_b.py --observe-only --feed iex --poll-seconds 15

# 2. After inspecting target allocations and understanding unverified parity:
python run_realtime_bot_b.py --allow-unverified-parity --feed iex --poll-seconds 15

# If entitled to consolidated SIP, replace --feed iex with --feed sip.
# Do NOT attempt to use the live-keys of your production trading bot.
```

Print one evaluation and exit (only useful when market is actively trading):

```bash
python run_realtime_bot_b.py --once --observe-only --feed iex
```

Default market hours: 09:35–15:55 America/New_York, weekdays. Interrupt `Ctrl+C` to stop; restart with same ledger filename to restore the local paper holdings. To test independently with a different hypothetical account, use a new `--ledger` filename.

**Do not run two simultaneous processes using the same ledger path**; SQLite logging does not implement cross-process order coordination. Manual and automatic restarts should be serialized.

## Offline tests

```bash
python -m unittest discover -s tests -v
```

These include: all-original-JSON evaluation, no-future-data guard, simulated current-session GDXU crash-to-BIL and recovery, three intraday signal updates with multiple simulated fills, reconciliation after each fill, stale-price guard and ledger recovery. **Synthetic market data are used** and are not a demonstration of financial returns or Composer parity.

The previous isolated smoke-test runner remains available:

```bash
python a15_fill_bot.py --strategy original_a15.json \
  --ledger /tmp/a15-b-demo.sqlite --evaluator a15_fill_bot:demo_evaluator \
  < sample_feed.jsonl
```

The above **demo evaluator is not A15** and is only retained for the original small core tests; use `run_realtime_bot_b.py` for full-tree intraday evaluation.

## Verify Composer equivalence before relying on results

Export daily historical **target allocation weights** from the OG Composer A15 for at least 10–20 days and compare exact symbol/weight snapshots to this evaluator at a **comparable daily cutoff**, using consistent provider data. A difference is a mismatch that must be resolved; the strategy is not Composer-identical until discrepancies are eliminated. The current package's synthetic-group ranks and intraday RSI may differ even if every original JSON decision branch is represented.

## Project contents

| File | Purpose |
|---|---|
| `original_a15.json.gz.b64` | Exact original 2,918-node source compressed for GitHub storage (restored with SHA-256 verification) |
| `restore_strategy.py` | Deterministically restores `original_a15.json` before starting Bot B |
| `a15_realtime_eval.py` | All-node research evaluator using completed history plus evolving partial daily bar |
| `alpaca_market_data.py` | Authenticated **GET-only** Alpaca historical and current snapshot data |
| `run_realtime_bot_b.py` | Continuous market polling, full reevaluations, and LOCAL paper order-on-fill event loop |
| `a15_fill_bot.py` | Paper-only portfolio, fill engine, ledger and safety checks |
| `tests/test_engine.py` | Original five paper ledger tests |
| `tests/test_realtime_eval.py` | Five additional full A15 intraday tests |
| `a15_adapter_template.py` | Optional hook for a future parity-validated Composer/A15 engine |

See the live feeds / rate limits / venue restrictions at [Alpaca Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api) and [stock snapshots](https://docs.alpaca.markets/us/reference/stocksnapshots-1).

## GitHub preparation / deferred deployment

This repository is **source control only**. The included GitHub Actions workflow runs offline tests; it **does not start a paper trader** and contains no VPS deployment action.

**Intended destination: a new PRIVATE GitHub repository.** The original `original_a15.json` is a proprietary trading strategy and must not be pushed to a public repository. Never commit Alpaca keys or the local SQLite ledger. `.gitignore` excludes `.env*`, paper databases, logs, and local caches. Keep credentials in the eventual VPS secret environment, not GitHub code.

The default paper starting balance is now **$1,200**. Paper account funding occurs only when a *new* ledger is created. Existing ledgers are persistent, and mixing accounts is blocked. If you intentionally want a new $1,200 experiment, select a fresh `--ledger` path. Fractional shares are simulated and broker-specific minimums are **not** validated.

For later VPS deployment, refer to `DEPLOY_LATER.md`. GitHub CI verifies the code on push without access to live market data or the user's credentials.


## GitHub Actions short forward sessions (manual, not continuously running)

The optional `.github/workflows/paper-session.yml` allows a **manually triggered** 15/30/60/120 minute session during US regular market hours. It does NOT run on a schedule, does NOT deploy to the VPS, and does NOT make any trading/order API requests. A free IEX data feed may not have fresh trades for every original-A15 signal symbol; in that case it fails closed.

To enable this manual workflow, set the two read-only Alpaca market-data credentials as **repository Actions secrets** `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`. Then go to **Actions → A15 Bot B manual paper forward session → Run workflow**. Choose `observe` for inspection or `paper` for local simulated order fills. The paper mode persists its SQLite ledger as a private Actions artifact for up to 60 days. Keep only one active session; use the separate original A15 bot unchanged.

Each session consumes GitHub-hosted runner compute. Repeating sessions all day every day may exceed the monthly included allocation. A 15-second evaluation interval is active only while the workflow job is running. Longer-lived continuous paper trading belongs on a properly configured VPS with persistent storage, not GitHub Actions. **The workflow is not automatically started by committing it.**