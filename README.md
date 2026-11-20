# A15 Bot B — Standalone intraday / on-fill LOCAL PAPER forward tester

## Status (2026-11-19)

**Implemented and offline-tested. Automatic GitHub Action committed but gated until repository visibility is PUBLIC. No actual authenticated forward-market run has been verified; Composer parity remains unverified.** The runner *can* run without the existing A15 bot. It never makes brokerage trading/order API calls and cannot trade real money.

This is **only Bot B**. No duplicate OG A15 process is included and the existing VPS/services are unchanged.

## Intended behavior

1. At start of each US trading session, download **completed historical daily bars** for every symbol referenced in `original_a15.json` (57 allocation symbols plus 15 condition-only tickers, 72 total).
2. Every 15 seconds during US regular hours (configurable), fetch read-only, current-session market snapshots from Alpaca IEX or consolidated SIP.
3. Evaluate the **entire unchanged, 2,918-node A15 JSON** using the current observed price as a **partial, in-progress daily bar** for RSI, momentum, SMA, sub-strategy rank, etc. No future daily bar values.
4. Compare A15 target allocations to the locally simulated virtual holdings. If sufficiently different, simulate one fill at the observed price +/- configured slippage. In the optional IEX quote-fallback research mode, the observed price may be a fresh, spread-limited bid/ask midpoint rather than an executed trade.
5. **Immediately reevaluate the full 2,918-node A15 tree after that fill**, then fill the next required paper order, reevaluate, etc. Stop at target convergence or safety cap. Continue watching market signals; changing signals may cause **multiple entry, exit or rotation trades within one trading day**.
6. Write the strategy SHA, paper fills, decisions, positions, equity and safety events to an append-only SQLite journal that persists across restarts.

The original JSON has not been modified. GitHub stores its original contents as a compressed, base64-encoded blob `original_a15.json.gz.b64`; `restore_strategy.py` verifies SHA-256 and reconstructs the exact JSON before use. Do not treat the encoded source as a different strategy. Default capital **$1,200**, 5 bps simulated slippage, 0 commissions, $1/0.5%-of-equity minimum adjustment, and 80 maximum fills per trading day. The min-order threshold is **$6 at $1,200 equity** (0.5%), so smaller target weights are not filled.

## Critical limitations

- **Not Composer parity-verified**: The included `a15_realtime_eval.py` evaluates all node *types*, all filters, every conditional and sub-strategy with independently implemented definitions. **Complex historical sub-strategy rank** is computed from a synthetic daily mark-to-market portfolio based on previous-day weights; this is a plausible implementation, not a verified duplicate of Composer's internal metric. Indicator warm-up, RSI implementation, corporate-action handling and rank ties can also differ from Composer. To trade local *paper* orders using this independent replica, explicitly pass `--allow-unverified-parity`. For signal inspection only, use `--observe-only`.
- **Local paper simulator**: virtual fills are immediate and based on the latest read price +/- slippage. These are **not Alpaca paper-account fills**, exchange fills or guaranteed executable prices. There is no broker order endpoint and no account API dependency. No real money is used.
- **Real-time means near-real-time polling** at configured interval, not tick-by-tick streaming. Order-fill recalculations happen immediately within the simulation event loop.
- **Market-data access is required** to start live-forward monitoring. Alpaca Basic provides only IEX venue data, not all consolidated US trades; SIP entitlement may be necessary for many A15 assets. Missing/unreliable data or insufficient history causes a fail-closed **NO_TRADE** log, never silent substitution of yesterday's last quote. Optional `--quote-fallback` uses ONLY same-session IEX bid/ask midpoint prices with <=1% spread and <=120-second quote age, if a trade print is missing or older than one minute. Such quotes are not executable fills and differ from Composer closing prices. This fallback does not guarantee full A15 quote coverage.
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

GitHub Actions now includes offline CI, optional manual paper sessions, and an automatic scheduled paper session (`.github/workflows/automatic-paper.yml`). None of these deploys a VPS service or makes a brokerage order.

**Repository currently remains PRIVATE.** To enable free standard GitHub-hosted runner minutes, the owner must intentionally change it to PUBLIC. This publishes the COMPLETE original A15 strategy (stored as a reversible compressed base64 blob), code, workflow logs and possibly saved paper-trading artifacts. Do not change visibility unless that disclosure is acceptable. Never commit Alpaca keys or the local SQLite ledger. `.gitignore` excludes `.env*`, paper databases, logs, and local caches. Keep credentials in the eventual VPS secret environment, not GitHub code.

The default paper starting balance is now **$1,200**. Paper account funding occurs only when a *new* ledger is created. Existing ledgers are persistent, and mixing accounts is blocked. If you intentionally want a new $1,200 experiment, select a fresh `--ledger` path. Fractional shares are simulated and broker-specific minimums are **not** validated.

For later VPS deployment, refer to `DEPLOY_LATER.md`. GitHub CI verifies the code on push without access to live market data or the user's credentials.


## GitHub Actions short forward sessions (manual, not continuously running)

The optional `.github/workflows/paper-session.yml` allows a **manually triggered** 15/30/60/120 minute session during US regular market hours. It does NOT run on a schedule, does NOT deploy to the VPS, and does NOT make any trading/order API requests. A free IEX data feed may not have fresh trades for every original-A15 signal symbol; in that case it fails closed.

To enable this manual workflow, set the two read-only Alpaca market-data credentials as **repository Actions secrets** `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`. Then go to **Actions → A15 Bot B manual paper forward session → Run workflow**. Choose `observe` for inspection or `paper` for local simulated order fills. The paper mode persists its SQLite ledger as a private Actions artifact for up to 60 days. Keep only one active session; use the separate original A15 bot unchanged.

Each session consumes GitHub-hosted runner compute. Repeating sessions all day every day may exceed the monthly included allocation. A 15-second evaluation interval is active only while the workflow job is running. Longer-lived continuous paper trading belongs on a properly configured VPS with persistent storage, not GitHub Actions. **The workflow is not automatically started by committing it.**

## Automatic GitHub-hosted paper sessions (requires PUBLIC repository)

The owner opted to permit publicly exposing the original A15 strategy and using GitHub-hosted public-repository compute. Workflow: `.github/workflows/automatic-paper.yml`.

- Starts **weekdays at approximately 9:36 AM and 12:54 PM America/New_York**, using timezone-aware cron. The first job ends by 12:52 PM ET and the second by 3:55 PM ET.
- The two segments avoid GitHub's 6-hour/job limit. There will be at least a short handoff gap, and **GitHub scheduled executions can be delayed, dropped, or otherwise unavailable**. This is NOT a guaranteed continuous service. It does not run outside regular US trading hours.
- Each active segment polls the market data feed about every **15 seconds**, independently evaluates the original A15 tree using the unverified research evaluator and recalculates after every **simulated LOCAL fill**; multiple paper trades per session are permitted.
- Virtual initial capital is **$1,200**; the ledger is restored from the most recent available private/public-repo Actions artifact before each run and re-uploaded at the end. Artifacts are retained up to **60 days**, can expire, and are **not** a durable database. Download/back up the ledger periodically. If the saved ledger cannot be restored, the bot may start a new $1,200 ledger; audit the first evaluation for continuity before interpreting returns.
- Uses the limited `iex` feed. Missing/incomplete/stale quotes produce `NO_TRADE` and prevent simulated orders; a workflow being green does NOT certify that real-time data were complete or that trades happened.
- Uses **read-only market-data GET calls**, **no actual Alpaca order API requests** and no production A15 service modifications. The strategy evaluator is **not verified Composer-equivalent**.
- **Hard gate:** automatically scheduled runs only execute when GitHub reports the repository as **public**. When private, the job is skipped to protect the monthly metered allowance. Changing repository visibility is a user-controlled GitHub setting, not performed by the workflow.
- Review repository security/settings before making public, since public readers can copy the original A15 JSON and view Actions logs and artifacts. Never publish market-data secrets.

The original manual paper-session workflow remains available for debugging and does not need to be used each trading day.

## November 20, 2026: Diagnosed no-trade failure and data-coverage fix

The first live GitHub-hosted run loaded 996 completed historical SPY bars but logged 589 consecutive `IncompleteMarketData: Missing price for GDXU on index 996` errors. **Index 996 is the changing CURRENT intraday daily bar**, not a prior historical bar. The feed was `iex`, and no current-session eligible GDXU last trade appeared in the prices passed to the A15 evaluator. The bot executed **zero paper fills**. Another scheduled GitHub job arrived after market close, skipped every trading step, and nevertheless showed a green check.

New safeguards:
- Scheduled public workflow enables `--quote-fallback`, using only current-session IEX best bid/ask with tight spread/age conditions when current-session trade prints are missing or stale. This is a *research-price approximation*, not a verified Composer or execution-equivalent trade price.
- `SNAPSHOT_COVERAGE` log entries identify priced/missing symbols (including GDXU). After 20 consecutive `NO_TRADE` errors (~5 minutes), `FATAL_DATA_COVERAGE` terminates the process with an error; no more multiple-hour silent nontrading sessions.
- The workflow now marks late scheduled starts as failures rather than presenting successful skips as successful paper trading.
- The `A15_B_FEED` **repository variable** defaults to `iex`. If the user has **paid real-time SIP entitlement**, set it to `sip` under repository Settings -> Secrets and variables -> Actions -> Variables. With free Basic IEX, the required live quote coverage may still be insufficient. **Do not substitute delayed SIP data and claim it is real-time.**

These changes are code-level safeguards, not verified Alpaca live-data remediation. A green CI test only verifies synthetic/offline behavior. The first future in-market paper job and its logs must be inspected before reporting any trades. The original VPS live A15 is unchanged.
