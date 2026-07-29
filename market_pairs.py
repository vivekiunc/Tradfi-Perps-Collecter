"""
For every TradFi underlying, pull EVERY exchange that lists it.

Reads the slug list from cmc_tradfi_assets.csv, then hits CMC's market-pairs
endpoint per underlying. This is what gets you venues beyond the ones you
already track -- MEXC, KuCoin, Phemex, Aster, Lighter, Paradex, etc.

    python cmc_market_pairs.py
    python cmc_market_pairs.py --category all      # include spot, not just perps

Output: cmc_market_pairs.csv, long format, one row per (underlying, exchange,
pair). Column names match pivot_by_exchange.py, so:

    python pivot_by_exchange.py cmc_market_pairs.csv --out matrix.xlsx

Resumable: each slug's raw JSON is cached under .cache/, so a crash or a
rate-limit block mid-run costs you nothing. Delete .cache/ to force a refresh.

NOTE: the endpoint below is CMC's undocumented web API. If it 404s or returns
an unexpected shape, open any /currencies/<slug>/markets/ page with DevTools >
Network > XHR, find the request, and paste its URL into ENDPOINT.
"""

import argparse
import json
import os
import sys
import time

import pandas as pd
import requests

ENDPOINT = ("https://api.coinmarketcap.com/data-api/v3/"
            "cryptocurrency/market-pairs/latest")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept": "application/json",
}

CACHE = ".cache"
PAGE = 100


def first(d, *keys):
    """CMC renames fields between releases; take whichever key is present."""
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def parse(payload, slug, name):
    """Pull the market-pair rows out of a response, tolerating shape changes."""
    data = payload.get("data") or {}
    pairs = (data.get("marketPairs") or data.get("market_pairs")
             or payload.get("marketPairs") or [])
    rows = []
    for m in pairs:
        rows.append({
            "Underlying": name or slug,
            "cmc_slug": slug,
            "Exchange": first(m, "exchangeName", "exchange_name", "exchangeSlug"),
            "exchange_slug": first(m, "exchangeSlug", "exchange_slug"),
            "Pair": first(m, "marketPair", "market_pair"),
            "market_type": first(m, "category", "marketPairCategory"),
            "price_usd": first(m, "price", "priceUsd"),
            "24h Volume (USDT)": first(m, "volumeUsd", "volume24h",
                                       "volumeUsd24h", "volume_usd"),
            # CMC's own data-quality signals -- the red-database icon on the
            # website. Unverified venues report volumes wildly out of line with
            # their real activity; keep these so you can filter later.
            "outlier_detected": first(m, "outlierDetected", "outlier_detected"),
            "volume_excluded": first(m, "volumeExcluded", "excluded"),
            "price_excluded": first(m, "priceExcluded"),
            "market_score": first(m, "marketScore", "score", "marketReputation"),
            "cmc_updated": first(m, "lastUpdated", "last_updated"),
        })
    total = first(data, "numMarketPairs", "num_market_pairs") or len(rows)
    return rows, int(total)


def fetch_slug(sess, slug, name, category, sleep):
    os.makedirs(CACHE, exist_ok=True)
    cache_file = os.path.join(CACHE, f"{slug}__{category}.json")

    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return parse(json.load(f), slug, name)[0], True

    rows, start, merged = [], 1, None
    while True:
        r = sess.get(ENDPOINT, headers=HEADERS, timeout=30, params={
            "slug": slug, "start": start, "limit": PAGE,
            "category": category, "centerType": "all",
            "sort": "cmc_rank_advanced", "direction": "desc",
            "spotUntracked": "true",
        })
        r.raise_for_status()
        payload = r.json()
        page_rows, total = parse(payload, slug, name)
        if merged is None:
            merged = payload
        else:
            merged["data"]["marketPairs"] += payload["data"]["marketPairs"]
        rows += page_rows
        if len(rows) >= total or not page_rows:
            break
        start += PAGE
        time.sleep(sleep)

    with open(cache_file, "w") as f:
        json.dump(merged, f)
    return rows, False


FLAG_COLS = ["outlier_detected", "volume_excluded", "price_excluded"]


def drop_unverified(df):
    """Remove listings CMC flags as unverified/outlier.

    These are the venues carrying the red-database icon on the website. They
    self-report volumes that are not independently verifiable and are commonly
    orders of magnitude above their real activity.

    Returns (kept, dropped).
    """
    present = [c for c in FLAG_COLS if c in df.columns]
    if not present:
        return df, df.iloc[0:0]

    flagged = pd.Series(False, index=df.index)
    for c in present:
        v = df[c]
        flagged |= v.notna() & ~v.isin([0, "0", False, "false", "False", ""])

    return df[~flagged].copy(), df[flagged].copy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="cmc_tradfi_assets.csv")
    ap.add_argument("--out", default="cmc_market_pairs.csv")
    ap.add_argument("--category", default="perpetual",
                    choices=["perpetual", "futures", "spot", "all"])
    ap.add_argument("--sleep", type=float, default=1.5,
                    help="seconds between requests; raise if you get 429s")
    ap.add_argument("--include-unverified", action="store_true",
                    help="keep venues CMC flags as unverified (default: drop)")
    args = ap.parse_args()

    try:
        assets = pd.read_csv(args.assets)
    except FileNotFoundError:
        sys.exit(f"{args.assets} not found -- run cmc_tradfi_assets.py first")

    name_col = ("underlying_name" if "underlying_name" in assets.columns
                else "name" if "name" in assets.columns else None)

    all_rows, failed = [], []
    for i, row in assets.iterrows():
        slug = row["slug"]
        name = row[name_col] if name_col else slug
        try:
            rows, cached = fetch_slug(requests, slug, name,
                                      args.category, args.sleep)
            all_rows += rows
            tag = "cached" if cached else "fetched"
            print(f"[{i+1:>3}/{len(assets)}] {slug:<45} "
                  f"{len(rows):>3} venues ({tag})")
            if not cached:
                time.sleep(args.sleep)
        except Exception as e:
            failed.append(slug)
            print(f"[{i+1:>3}/{len(assets)}] {slug:<45} FAILED: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

    if not all_rows:
        sys.exit("\nNo rows returned. The endpoint has almost certainly moved "
                 "-- see the NOTE at the top of this file.")

    df = pd.DataFrame(all_rows)
    raw_n = len(df)

    if args.include_unverified:
        print("\n[!] keeping unverified venues (--include-unverified)")
    else:
        df, dropped = drop_unverified(df)
        if len(dropped):
            print(f"\ndropped {len(dropped)} unverified listings across "
                  f"{dropped['Exchange'].nunique()} venues:")
            summary = (dropped.groupby("Exchange")["24h Volume (USDT)"]
                       .agg(["count", "sum"])
                       .sort_values("sum", ascending=False))
            print(summary.to_string())
            dropped.to_csv("excluded_unverified.csv", index=False)
            print("  -> excluded_unverified.csv (kept for the methodology note)")
        else:
            print("\n[!] WARNING: 0 listings flagged as unverified.")
            print("    CMC definitely flags some venues, so either the quality")
            print("    fields are named differently in this response or they")
            print("    are absent. Inspect one cached file before trusting")
            print("    these volumes:")
            print("      python3 -c \"import json,glob;"
                  "print(json.dumps(json.load(open(glob.glob('.cache/*.json')[0]))"
                  "['data']['marketPairs'][0],indent=2))\"")
            print("    Then add the real field names to FLAG_COLS / parse().")

    df.to_csv(args.out, index=False)

    print(f"\n{len(df)} listings ({raw_n} before filtering) | "
          f"{df['Underlying'].nunique()} underlyings "
          f"| {df['Exchange'].nunique()} exchanges -> {args.out}")
    if failed:
        print(f"{len(failed)} slugs failed: {', '.join(failed[:10])}"
              f"{' ...' if len(failed) > 10 else ''}")
        print("re-run to retry; successful slugs are cached and will be skipped")

    print("\nexchanges by number of TradFi listings:")
    print(df.groupby("Exchange")["Underlying"].nunique()
            .sort_values(ascending=False).to_string())


if __name__ == "__main__":
    main()