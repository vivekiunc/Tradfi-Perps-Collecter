"""
Pull the ~194 assets from CoinMarketCap's "TradFi Assets (Derivatives)" tag page.

Primary method parses the __NEXT_DATA__ JSON blob embedded in the server-rendered
page, which keeps the slugs. Falls back to table parsing if CMC changes the
page structure (slugs lost in that case).

    pip install requests beautifulsoup4 lxml pandas
    python cmc_tradfi_assets.py
"""

import json
import re
import sys
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://coinmarketcap.com/view/tradfi-assets-derivatives/"

# CMC returns 403 to bare requests; a normal browser UA is enough.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def find_coin_dicts(node, out):
    """Walk the __NEXT_DATA__ tree and collect anything shaped like a coin.

    Structure-agnostic on purpose: CMC moves the nesting around between
    releases, but the leaf objects reliably carry slug + symbol + name.
    """
    if isinstance(node, dict):
        if {"slug", "symbol", "name"} <= node.keys():
            out.append({
                "cmc_id": node.get("id"),
                "rank": node.get("cmcRank") or node.get("rank"),
                "name": node["name"],
                "symbol": node["symbol"],
                "slug": node["slug"],
            })
        for v in node.values():
            find_coin_dicts(v, out)
    elif isinstance(node, list):
        for v in node:
            find_coin_dicts(v, out)
    return out


def scrape_page(page):
    url = BASE if page == 1 else f"{BASE}?page={page}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    blob = soup.find("script", id="__NEXT_DATA__")
    if blob:
        rows = find_coin_dicts(json.loads(blob.string), [])
        if rows:
            return rows, "next_data"

    # Fallback: visible table. Loses slugs, so reconstruct them from the
    # anchor hrefs in the same row order.
    print(f"  page {page}: __NEXT_DATA__ not found, falling back to table",
          file=sys.stderr)
    slugs = [m.group(1) for m in
             re.finditer(r'/currencies/([a-z0-9-]+)/', r.text)]
    seen, ordered = set(), []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return [{"slug": s} for s in ordered], "table"


def main():
    rows = []
    for page in (1, 2):
        page_rows, method = scrape_page(page)
        print(f"page {page}: {len(page_rows)} raw records via {method}")
        rows += page_rows
        time.sleep(2)

    df = pd.DataFrame(rows).drop_duplicates(subset="slug")

    # The tag is derivatives-only, so every real entry is named "<X> (Derivatives)".
    # This drops nav/related/trending coins the tree-walk picks up incidentally.
    if "name" in df.columns:
        df = df[df["name"].str.contains(r"\(Derivatives\)", na=False)]
        df["underlying_name"] = (df["name"]
                                 .str.replace(r"\s*\(Derivatives\)$", "",
                                              regex=True))

    df = df.reset_index(drop=True)
    df.to_csv("cmc_tradfi_assets.csv", index=False)

    print(f"\n{len(df)} TradFi underlyings -> cmc_tradfi_assets.csv")
    print("expected ~194; well under that means the filter is too aggressive\n")
    print(df.head(15).to_string(index=False))


if __name__ == "__main__":
    main()
