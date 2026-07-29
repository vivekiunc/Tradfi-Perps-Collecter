# Tradfi Perps Collecter

A data-collection pipeline that catalogues every **TradFi-underlying perpetual futures** contract listed across major crypto exchanges. It answers a specific question: for each traditional-finance asset that now trades as a perp (single stocks, ETFs, commodities, FX, indices, pre-IPO), which venues list it, when did each contract launch, and how much volume does each carry?

The output is an Excel matrix of underlying × exchange 24h volume, plus the long-format CSVs behind it.

## How it works

The pipeline pulls from two independent sources and keeps them in a common schema so they can be cross-checked against each other.

**Source 1 — direct exchange APIs** (`collect.py`). Hits the public instrument endpoint of ten venues (Binance, Bybit, OKX, Gate.io, Bitget, BitMEX, BitMart, Delta Exchange, HTX, Hyperliquid), one unauthenticated call each. This path gives the most reliable contract-level detail: launch dates, max leverage, and native 24h volume where the venue exposes it. Where an instrument endpoint omits the launch date, `first_trade_date()` recovers it from the earliest daily kline.

**Source 2 — CoinMarketCap** (`tradfi.py` → `market_pairs.py`). This path exists to find venues the exchange-API list misses (MEXC, KuCoin, Phemex, Aster, Lighter, Paradex, and so on). `tradfi.py` scrapes CMC's "TradFi Assets (Derivatives)" tag to get the ~194 tracked underlyings, then `market_pairs.py` queries CMC's market-pairs endpoint per underlying to enumerate every exchange listing it. Listings that CMC flags as unverified or outlier are dropped by default and set aside in a separate file for the methodology note.

Both sources feed `pivot_by_exchange.py`, which collapses the long table into the final volume matrix.

## Pipeline

```
tradfi.py ──────────────► cmc_tradfi_assets.csv   (~194 TradFi underlyings)
                                 │
                                 ▼
market_pairs.py ────────► cmc_market_pairs.csv     (every venue per underlying)
                          excluded_unverified.csv  (CMC-flagged listings, set aside)
                                 │
collect.py ─────────────► universe.csv             (10 exchange APIs, direct)
                                 │
                                 ▼
pivot_by_exchange.py ───► tradfi_matrix.xlsx        (underlying × exchange volume)
```

`run_daily_report.py` runs the whole chain end to end and archives a dated copy to `Reports/`.

## Setup

```bash
pip install pandas requests beautifulsoup4 lxml openpyxl
```

No API keys required. Every endpoint used is public.

## Usage

Run the full daily job (scrape → market pairs → pivot → exchange collect → archive):

```bash
python3 run_daily_report.py
```

Or run any stage on its own:

```bash
# 1. TradFi underlyings from CMC's derivatives tag
python3 tradfi.py

# 2. Every exchange that lists each underlying (resumable, cached under .cache/)
python3 market_pairs.py
python3 market_pairs.py --category all          # include spot, not just perps
python3 market_pairs.py --include-unverified    # keep CMC-flagged venues

# 3. Direct-from-exchange instrument pull
python3 collect.py --out universe.csv
python3 collect.py --backfill                   # recover missing launch dates from klines
python3 collect.py --existing tracker.csv       # diff against an existing sheet

# 4. Pivot any long CSV into the volume matrix
python3 pivot_by_exchange.py cmc_market_pairs.csv --out tradfi_matrix.xlsx
python3 pivot_by_exchange.py universe.csv --merge-hl   # collapse Hyperliquid builder markets
```

## Files

| File | Purpose |
|------|---------|
| `collect.py` | Fetches perp instruments directly from 10 exchange APIs; kline-based launch-date fallback |
| `tradfi.py` | Scrapes CMC's TradFi Assets (Derivatives) tag for the underlying universe |
| `market_pairs.py` | Enumerates every exchange listing per underlying via CMC; drops unverified venues |
| `pivot_by_exchange.py` | Pivots the long table into a formatted underlying × exchange XLSX matrix |
| `run_daily_report.py` | Orchestrates the full pipeline and archives a dated report |
| `universe.csv` | Long table from the direct exchange-API path |
| `cmc_tradfi_assets.csv` | The ~194 TradFi underlyings CMC tracks |
| `cmc_market_pairs.csv` | Long table from the CMC path, one row per (underlying, exchange, pair) |
| `excluded_unverified.csv` | Listings CMC flags as unverified, retained for the methodology note |
| `Reports/` | Dated XLSX archives from `run_daily_report.py` |

## Output schema

The volume matrix has two tabs: **Volume Matrix** (rows = underlying, columns = exchange, cells = 24h USD volume, with live `Total Volume` and `# Venues` formulas) and **Contract Detail** (the underlying long table, kept for traceability). A blank cell means the underlying is not listed on that venue, which is itself a finding.

The long CSVs share these core columns: `Exchange`, `Pair`, `Ticker`/`Underlying`, `Classification`, `LaunchDate`, `maximum_leverage`, `24h Volume (USDT)`.

## Notes and gotchas

The CMC endpoints in `tradfi.py` and `market_pairs.py` are undocumented web APIs. If a run returns zero rows or an unexpected shape, CMC has moved the endpoint. Open any `/currencies/<slug>/markets/` page with DevTools → Network → XHR, find the request, and paste its URL into `ENDPOINT` (or the `CMC_*_ENDPOINT` constants in `collect.py`). The scraper tolerates field renames, but not a moved endpoint.

`market_pairs.py` caches each underlying's raw JSON under `.cache/`, so a crash or rate-limit block mid-run costs nothing on the next run. Delete `.cache/` to force fresh data. `run_daily_report.py` clears it automatically after archiving.

The unified join key is `exchange + uppercased pair with separators and SWAP/PERP stripped`, because the same contract appears as `BTCUSDT`, `BTC_USDT`, `BTC-USDT-SWAP`, or `BTC-USDT` depending on venue. Hyperliquid builder markets (`xyz:`, `flx:`, `cash:`, etc.) are kept as separate columns by default since they run independent order books; pass `--merge-hl` to collapse them.

If you get 429s from CMC, raise `--sleep` (default 1.5s between requests).
