# JoinMarket Stats

A static JoinMarket mainnet dashboard: monthly CoinJoin-shaped activity, historical orderbooks, and modeled maker returns. CJ sizes and fees use satoshis; relative fees use percent, as makers configure them. Transaction counts and BTC volume share one chart; monthly maker fee totals have a separate chart with visible solver coverage. Light and dark themes follow the system by default, with a saved manual override. No CDNs, analytics, remote fonts, frontend packages, or runtime backend. The generated dashboard is about 180 KB before compression.

## Run locally

Python 3.11 or newer, or Docker. Production uses Python 3.14.

```sh
uv venv --python 3.14
uv pip install -r requirements.txt -e .
.venv/bin/jmstats update
.venv/bin/python -m http.server 8080 --directory site
```

Open http://localhost:8080. The committed archive is the starting point; normal updates never bootstrap missing history. Local collection defaults to `https://mempool.sgn.space/api`. Override with `--api` to use another mainnet Esplora-compatible API. The orderbook comes from https://joinmarket-ng.sgn.space/orderbook.json.

```sh
docker build -t joinmarket-stats .
mkdir -p data site
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/data:/app/data" -v "$PWD/site:/app/site" joinmarket-stats update
docker run --rm -p 8080:8080 -v "$PWD/site:/site:ro" -w /site \
  --entrypoint python joinmarket-stats -m http.server 8080 --bind 0.0.0.0
```

On rootless Docker, omit `--user` because container root already maps to your host user. On SELinux hosts, bind mounts may also require the `:Z` label option.

Collection and rendering are separable:

```sh
jmstats orderbook
jmstats sync --request-budget 600 --max-blocks 200
jmstats render --output site
jmstats --data /path/to/archive render --output /path/to/site
```

## History and explicit bootstrap

The imported full-block corpus begins January 1, 2025, with incremental scanning extending it to the current confirmed tip. The dashboard starts at the first scanned month (currently January 2025); years without observations are omitted from the selector. Gaps after that start and partial months remain labeled, never invented or displayed as zero. JSON and CSV exports retain unscanned months from 2020 for coverage tracking and explicit backfills. The prefilled hourly orderbook history starts May 22, 2026. Inspect the coverage line and `data/state.json` for the current corpus status.

The initial corpus can be imported explicitly from the reference analyzer, without a runtime dependency on it:

```sh
jmstats import-analyzer ../joinmarket_analyzer/tmp/jm_2025_2026.sqlite3
jmstats import-fees ../joinmarket_analyzer/tmp/jm_2025_2026.sqlite3
```

`import-fees` reduces the source's individual conditional maker gains to monthly absolute and relative medians. These are not medians of transaction-level averages. Missing fee estimates remain unavailable rather than triggering a solver or historical scan.

Newly collected CoinJoin-shaped transactions are analyzed with a bounded CBC solver (five seconds by default, one thread, a 1 GiB address-space limit per child process, and a hard subprocess deadline five seconds longer). The solver supports multiple inputs per participant and taker change. Each attempt caches input/output amounts in `data/fee-solutions/` before solving, so later retries need no transaction download. Results are conditional feasible decompositions, not uniquely recovered fees. The price-prior-free fee-ceiling method for the maker workbench remains separate.

Existing transactions are **not** automatically reanalyzed on upgrade. Start an explicit solver-only pass, optionally using the analyzer's read-only transaction cache:

```sh
jmstats analyze-fees --cache ../joinmarket_analyzer/tmp/jm_2025_2026.sqlite3 \
  --cache-only --max-transactions 200 --solver-seconds 5
jmstats analyze-fees --from-height 610670 --max-transactions 200
```

Repeat to continue with unattempted transactions. Imported successful estimates are retained without recomputation. Each invocation is bounded by transaction count, request budget, and wall time (`--max-seconds`, default 900). `--cache-only` never contacts the network; cache misses remain unknown. A durable `started` marker prevents an interrupted attempt from restarting implicitly. Use `--retry` explicitly to retry interrupted, timed-out, or errored attempts, optionally with a larger `--solver-seconds` budget. Infeasible and invalid outcomes are not retried. A throttle response reports a cooldown for the caller to honor without modifying the chain collector's state.

The solver-only command has its own writer lock and can run alongside a chain collector. For new parallel collection processes use `jmstats sync --no-solver`; ordinary `sync` and `update` run the solver themselves and require both locks. Never run two solver writers or two chain writers against the same archive. Solver-only passes change neither chain shards nor scan state. Raw amounts have no addresses, scripts, or ownership identities.

Historical orderbooks can be reduced on their source host without copying raw snapshots or installing this package there:

```sh
mkdir -p tmp
ssh snapshot-host 'nice -n 19 ionice -c 3 python3 - /path/to/snapshots' \
  < src/jmstats/snapshots.py > tmp/orderbook-history.jsonl
jmstats import-orderbook-history tmp/orderbook-history.jsonl
```

The reducer is read-only and emits aggregate JSONL without nicknames, addresses, outpoints, or proofs. Imports are idempotent. Hourly raw totals retain spam attacks and empty books. Rendering uses daily maker/bonded-maker peaks and the last reported daily locked amount, keeping the dashboard small without hiding brief spam peaks. Early snapshots lack verification fields: reported bond capital is retained separately, while verified capital remains `null`, not zero.

The maker workbench also needs historical competing bond weights. Reduce only the positive active weights, deduplicated by bond outpoint, without fees, sizes, or identities:

```sh
ssh snapshot-host 'nice -n 19 ionice -c 3 python3 - --bonds-only /path/to/snapshots' \
  < src/jmstats/snapshots.py > tmp/bond-history.jsonl
jmstats import-bond-history tmp/bond-history.jsonl
```

Each orderbook collection appends to both histories automatically. Fee ceilings for newly scanned CJs are inferred during chain collection from the transaction details it already fetches. Historical CJs need one explicit, bounded pass, optionally reading raw transactions from the analyzer cache before querying the API:

```sh
jmstats infer-cj-fees --days 365 --cache ../joinmarket_analyzer/tmp/jm_2025_2026.sqlite3
```

For an empty archive, explicitly initialize the starting height. Start at least two hours before the first desired UTC month. For January 2020, height 610670 provides that boundary margin.

```sh
jmstats --data fresh-data init --start-height 610670
jmstats --data fresh-data sync --backfill --max-blocks 1000
```

Fill older history alongside an existing 2025+ corpus:

```sh
jmstats sync --backfill --from-height 610670 \
  --api https://mempool.sgn.space/api --interval 0.1 \
  --request-budget 10000 --max-blocks 4000
```

Repeat to resume. Use faster pacing only with an instance you control. Full history requires hundreds of thousands of blocks and potentially hundreds of gigabytes of downloads, though only compact observations are retained. Do not run historical bootstrap against the public service unattended. Seed-following graph samples are intentionally not used for monthly totals: connectivity cannot prove completeness.

An explicit lower backfill floor does not prevent CI from extending the existing complete 2025+ segment. Once the prefix joins, the collector verifies continuity and promotes the persisted automatic scan floor. Only one chain writer may use an archive at a time; a solver-only writer may run independently.

## GitHub Pages and scheduled updates

1. Push this repository yourself. Enable **Settings → Pages → Source → GitHub Actions**.
2. Allow the workflow token to write repository contents. Branch rules must permit the bot's data commits.
3. Run **Validate and publish dashboard** manually once, inspect the collection log and the deployed coverage panel, then leave its daily schedule enabled.

The workflow tests, builds the same Docker image, collects and solves new candidates, commits `data/`, and deploys `site/` daily at 03:23 UTC. It does not start historical solver passes. Solver outcome counts appear in collection logs; solver execution errors fail the run after retained data is rendered. Public CI explicitly uses `https://mempool.space/api`, as requested. No credentials or local DNS overrides are included. Data-only commits do not retrigger the workflow. All production runs share a concurrency group.

Requests are serial, paced two seconds apart, and limited to 600 attempts per run. This pacing is a conservative starting point, **not a documented safe public quota**. Raw blocks cost two requests each, plus transaction detail lookups only for above-floor candidates. Six confirmations are left uncollected. Public raw-block availability and quota must be checked on the first manual CI run.

The [upstream API documentation](https://github.com/mempool/mempool/blob/f669f14851fb62b5fdbd16f662667228354b027f/frontend/src/app/docs/api-docs/api-docs.component.html) documents HTTP 429 and potential bans after repeated violations, but no numeric allowance or guaranteed retry headers. This project does not probe the service to discover its limits. The first 429 stops chain collection immediately and persists a cooldown of at least one hour, honoring a longer `Retry-After`. Other transient errors have bounded exponential backoff and jitter. Request-budget exhaustion checkpoints completed blocks. Orderbook collection is independent.

Unexpected source errors preserve completed observations and surface a failed workflow after rendering retained data. Chain and watcher freshness are visible separately. A rendered timestamp is not evidence of a fresh scan.

## Data and methodology

`data/chain/YYYY-MM.jsonl` stores one block per line:

```text
[height, block_hash, unix_timestamp, transaction_count, candidates]
candidate = [txid, denomination_sats, equal_outputs, miner_fee_sats_or_null,
             transaction_weight_or_null, conditional_total_maker_gain_sats_or_null]
```

The archive retains the broad structural cohort (denomination at least one satoshi). Reports select denominations of at least 75,000 satoshis, at least three equal outputs, equal-output count equal to half the output count rounded up, and at least that many inputs. This mirrors the reference analyzer. It can include false positives and miss atypical JoinMarket transactions. A shape match does not authenticate the protocol. Block timestamps, not calendar height estimates, define UTC months. Fully scanned months require one contiguous segment bracketing both boundaries by two hours, conservatively accounting for nonmonotonic block timestamps.

Equal-output volume is denomination times equal-output count, not unique capital. Equal outputs are only a participant proxy. Miner fees are separate from CoinJoin fees. Imported and new solver gains are non-unique, conditional net maker gains under a fee ceiling of `max(0.5% × denomination, 10,000 sat)` per maker and the assumption that makers do not lose sats, not observed gross fees. Every solver witness is independently checked against integer amounts and fee conservation. The maker fee chart sums only solved transactions and shows their fraction of all detected candidates; it does not extrapolate unresolved fees. Complete block coverage does not imply complete fee coverage. Missing estimates remain unavailable, while a solved zero gain is a real zero. The original analyzer corpus attempted every candidate with a two-second budget, but only successful solutions were imported; its coverage is not a random sample.

Mean gain is weighted by solved maker slots. Imported individual-maker medians in `data/fee-estimates.json` are retained; months without imported medians use individual gains from new solver witnesses. The table includes solved transaction counts and fee totals; JSON and CSV also include known attempt counts (older unsuccessful attempts were not imported). Solver evidence is joined by transaction ID to the current chain, so orphaned transactions do not contribute and re-mining the same transaction preserves its evidence. The imported corpus, parser, and solver constraints originate from [joinmarket_analyzer](https://github.com/m0wer/joinmarket_analyzer), MIT licensed. BTC amounts have at most two decimal places and no decimals at 100 BTC or more.

The watcher snapshot publishes anonymized current offers and aggregate hourly history, not nickname, address, or bond-outpoint histories. Reported locked principal is counted once per unexpired outpoint with consistent amounts. Verified principal is recorded separately using the watcher's flags, not independently checked by this collector. The chart uses reported values so early snapshots with unavailable verification remain comparable. Bonded-maker history counts positive advertised weight; raw maker and offer totals deliberately include spam. Advertised offer sizes do not prove available funds.

The maker workbench estimates the ROI-optimal relative fee and fidelity bond for a new maker from historical CJs only. Current offers, advertised fees, and offer-size limits are not inputs. It assumes unlimited offer liquidity, a one-year lock, and bonds from 0.01 to 1,000 BTC. The 90-day window is primary; the 365-day window shows seasonality. Both end two hours before the latest scanned block and must be fully scanned.

- **Typical round.** Median CJ size and median makers per CJ (equal outputs minus one, fractional medians interpolated) in the window. Monthly activity is the window's CJ count scaled to a month. This is a typical-round estimate; mean CJ size is several times the median, so relative-fee income on large CJs is not captured.
- **Fee ceilings.** `data/fee-inferences/` (method `single-input-minimax-v1`) covers CJs with exactly one input per participant. For each, exact bipartite matching over every input/change assignment finds the smallest possible maximum maker net gain, assuming makers never lose sats. The taker's limit was at least that. Multi-input, oversized, infeasible, and budget-limited CJs remain unknown, never zero. This subset is roughly 10% of CJs and skews small.
- **Relative-only approximation.** Every inferred ceiling is treated as a relative rate, `(gain - 0.5) / CJ size`, allowing for half-even fee rounding. Fee eligibility for a candidate rate is the share of samples at or above it. Realistic relative fees are below 1%: samples implying 1% or more are excluded as outliers, and candidate fees are public-grid rates below 1% (up to 0.5%). Without this limit, absolute fees on small CJs inflate relative-only estimates into 10% rates, and some equal-output non-CoinJoins show gains of up to twice the CJ amount.
- **Selection.** Each CJ is matched to the latest bond snapshot before it, within twice the median snapshot spacing (at least two hours). Competition is all positive active bond weights then, including matured bonds that still carry decaying weight, regardless of their fees (a conservative pool). A new one-year bond weighs `(amount × (e^0.015 - 1))^1.3`. Following [JoinMarket NG](https://github.com/m0wer/joinmarket-ng), each slot is weighted with 95% probability, integrated binomially, and otherwise goes outside the bonded pool. 128 deterministic paths simulate other makers' draws without replacement; the candidate's survival is the product of `W / (W + w)`.
- **Result.** Monthly fills = monthly CJs × fee eligibility × mean selection probability; net = fills × fee at the median size; annual ROI = 12 × net / bond. The fee maximizes eligibility × fee; the bond maximizes ROI on a log grid with local refinement, rounded to 0.01 BTC. At least 100 fee samples, 100 bond matches, and an 80% match rate are required, otherwise the window is unavailable.

Because weight grows as bond^1.3, per-BTC selection probability rises with bond size until the bond dominates competing weight, so small bonds earn a lower ROI. The dashboard shows ROI at several bond sizes. Net fees exclude maker miner contributions, operating costs, bond funding and redemption fees, and opportunity cost. Fee limits and competition are assumed independent. Actual taker policies, protocol attribution, demand, and funding constraints remain uncertain; this is a modeled scenario, not a forecast.

## Recovery and upgrades

Monthly shards are replaced atomically, flushed before another shard becomes dirty and at least every 20 blocks. Heights are checked across all shards because timestamps can go backward. Crashes can redownload at most 20 uncheckpointed blocks. OS-level writer locks release automatically on process exit; do not delete locks to bypass an active collector.

Absent chain state requires explicit initialization or import. Empty or gapped automatic ranges refuse to scan without explicit backfill. Unknown state versions or detector IDs fail closed, without migrations, deletion, or automatic rescans. Upgrading from the previous release leaves chain state, imported fees, and the existing single-input fee-inference format unchanged. An absent solver sidecar proves nothing and does not trigger historical work: only newly collected candidates are solved automatically. Explicit `analyze-fees` selects historical work. Unknown solver formats, or solver shards without their metadata, fail closed rather than initiating recomputation. Amounts and interrupted attempts are durably recorded before solving; successful and infeasible outcomes are not silently replaced.

Before extending the chain, the collector checks mainnet genesis and every segment's final hash, verifies fetched raw header hashes and adjacent ancestry, and checks joins. A reorganization stops collection; it does not silently delete history. After identifying the last good height, back up the archive, then explicitly repair it:

```sh
jmstats rollback --to LAST_GOOD_HEIGHT
jmstats sync
```

Rollback deletes only later observations and is idempotent. Rerun the same explicit rollback after an interrupted repair. If it empties the automatic range, a subsequent explicit backfill is required.

## Development

```sh
uv pip install -r requirements-dev.txt -e .
playwright install chromium
pytest -q
prek run --all-files
```

Unit tests use synthetic blocks and mock API responses, including throttling, cooldown, interrupted scans, gaps, mainnet guards, reorgs, bond deduplication, and fee eligibility. Browser tests serve generated artifacts locally, check desktop and mobile, and reject external runtime requests. Dependency locks are generated with `pip-compile -Uv pyproject.toml` and `pip-compile -Uv --extra dev pyproject.toml` with the corresponding output filenames. Generate the development lock under Python 3.11 so its transitive dependencies also support the minimum version. CI follows GitHub's `ubuntu-latest` runners and uses Node 24 actions, testing code changes on Python 3.11 and 3.14; scheduled data-only updates reuse the validated code without reinstalling browser test tooling.
