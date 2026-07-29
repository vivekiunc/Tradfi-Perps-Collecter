"""
TradFi perpetual futures universe collector.

Two stages:
  1. fetch_<exchange>()  -> normalized instrument rows straight from each venue's
                            public instrument endpoint (no auth, one call each)
  2. first_trade_date()  -> recovers listing date from the earliest daily kline,
                            for venues that don't expose it

Output columns match the existing tracker schema.

Run:  python tradfi_perps_collect.py --out universe.csv
"""

import argparse
import time
from datetime import datetime, timezone

import pandas as pd
import requests

TIMEOUT = 20
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "research-collector/1.0"})


def get(url, **kw):
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def ms_to_date(ms):
    """Epoch milliseconds -> ISO date. Returns None for missing/zero values."""
    if ms in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def row(exchange, pair, ticker, launch=None, leverage=None, volume_usd=None):
    return {
        "Exchange": exchange,
        "Pair": pair,
        "Ticker": ticker,
        "Underlying": None,        # fill from CMC / manual mapping
        "Classification": None,    # Single Stock / ETF / Commodity / Index / FX / Pre-IPO
        "LaunchDate": launch,
        "maximum_leverage": leverage,
        "24h Volume (USDT)": volume_usd,
    }


# --------------------------------------------------------------------------
# Per-exchange instrument fetchers
# --------------------------------------------------------------------------

def fetch_binance():
    d = get("https://fapi.binance.com/fapi/v1/exchangeInfo")
    out = []
    for s in d["symbols"]:
        if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
            continue
        # underlyingSubType tags non-crypto underlyings on Binance
        out.append(row("Binance", s["symbol"], s["baseAsset"],
                       launch=ms_to_date(s.get("onboardDate"))))
        out[-1]["_subtype"] = s.get("underlyingSubType")
    return out


def fetch_bybit():
    out, cursor = [], ""
    while True:
        d = get("https://api.bybit.com/v5/market/instruments-info",
                params={"category": "linear", "limit": 1000, "cursor": cursor})
        for s in d["result"]["list"]:
            if s.get("contractType") != "LinearPerpetual":
                continue
            out.append(row("Bybit", s["symbol"], s["baseCoin"],
                           launch=ms_to_date(s.get("launchTime")),
                           leverage=s.get("leverageFilter", {}).get("maxLeverage")))
        cursor = d["result"].get("nextPageCursor") or ""
        if not cursor:
            return out


def fetch_okx():
    d = get("https://www.okx.com/api/v5/public/instruments",
            params={"instType": "SWAP"})
    return [row("OKX", s["instId"], s["ctValCcy"],
                launch=ms_to_date(s.get("listTime")),
                leverage=s.get("lever"))
            for s in d["data"] if s.get("state") == "live"]


def fetch_gate():
    d = get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    out = []
    for s in d:
        ts = s.get("create_time")
        out.append(row("Gate.io", s["name"], s["name"].split("_")[0],
                       launch=ms_to_date(int(ts) * 1000) if ts else None,
                       leverage=s.get("leverage_max")))
    return out


def fetch_bitget():
    d = get("https://api.bitget.com/api/v2/mix/market/contracts",
            params={"productType": "USDT-FUTURES"})
    return [row("Bitget", s["symbol"], s.get("baseCoin"),
                launch=ms_to_date(s.get("launchTime")),
                leverage=s.get("maxLever"))
            for s in d["data"]]


def fetch_bitmex():
    d = get("https://www.bitmex.com/api/v1/instrument/active")
    out = []
    for s in d:
        if not s.get("symbol"):
            continue
        im = s.get("initMargin")
        lev = round(1 / im, 2) if im else None
        listing = (s.get("listing") or "")[:10] or None
        out.append(row("BitMEX", s["symbol"], s.get("underlying"),
                       launch=listing, leverage=lev,
                       volume_usd=s.get("foreignNotional24h")))
    return out


def fetch_bitmart():
    d = get("https://api-cloud-v2.bitmart.com/contract/public/details")
    return [row("BitMart", s["symbol"], s.get("base_currency"),
                launch=ms_to_date(s.get("open_timestamp")),
                leverage=s.get("max_leverage"),
                volume_usd=s.get("volume_24h"))
            for s in d["data"]["symbols"]]


def fetch_delta():
    d = get("https://api.delta.exchange/v2/products",
            params={"contract_types": "perpetual_futures", "page_size": 1000})
    out = []
    for s in d["result"]:
        lt = s.get("launch_time")
        launch = lt[:10] if isinstance(lt, str) else None
        out.append(row("Delta Exchange", s["symbol"],
                       (s.get("underlying_asset") or {}).get("symbol"),
                       launch=launch, leverage=s.get("default_leverage")))
    return out


def fetch_htx():
    d = get("https://api.hbdm.com/linear-swap-api/v1/swap_contract_info")
    out = []
    for s in d["data"]:
        cd = str(s.get("create_date") or "")  # YYYYMMDD
        launch = f"{cd[:4]}-{cd[4:6]}-{cd[6:8]}" if len(cd) == 8 else None
        out.append(row("HTX", s["contract_code"], s.get("symbol"), launch=launch))
    return out


def fetch_hyperliquid():
    d = SESSION.post("https://api.hyperliquid.xyz/info",
                     json={"type": "metaAndAssetCtxs"}, timeout=TIMEOUT).json()
    meta, ctxs = d[0]["universe"], d[1]
    out = []
    for asset, ctx in zip(meta, ctxs):
        out.append(row("Hyperliquid", asset["name"], asset["name"],
                       leverage=asset.get("maxLeverage"),
                       volume_usd=ctx.get("dayNtlVlm")))
    return out


FETCHERS = {
    "Binance": fetch_binance,
    "Bybit": fetch_bybit,
    "OKX": fetch_okx,
    "Gate.io": fetch_gate,
    "Bitget": fetch_bitget,
    "BitMEX": fetch_bitmex,
    "BitMart": fetch_bitmart,
    "Delta Exchange": fetch_delta,
    "HTX": fetch_htx,
    "Hyperliquid": fetch_hyperliquid,
}


# --------------------------------------------------------------------------
# Listing-date fallback: earliest daily candle
# --------------------------------------------------------------------------

def first_trade_date(exchange, symbol):
    """Earliest daily kline == first day the contract traded.

    Use wherever the instrument endpoint has no launch field, or to
    cross-check a launch date against an announcement.
    """
    try:
        if exchange == "Binance":
            d = get("https://fapi.binance.com/fapi/v1/klines",
                    params={"symbol": symbol, "interval": "1d",
                            "startTime": 0, "limit": 1})
            return ms_to_date(d[0][0]) if d else None

        if exchange == "Bybit":
            d = get("https://api.bybit.com/v5/market/kline",
                    params={"category": "linear", "symbol": symbol,
                            "interval": "D", "start": 0, "limit": 1})
            lst = d["result"]["list"]
            return ms_to_date(lst[-1][0]) if lst else None

        if exchange == "OKX":
            d = get("https://www.okx.com/api/v5/market/history-candles",
                    params={"instId": symbol, "bar": "1D", "limit": 100})
            return ms_to_date(d["data"][-1][0]) if d.get("data") else None

        if exchange == "Bitget":
            d = get("https://api.bitget.com/api/v2/mix/market/history-candles",
                    params={"symbol": symbol, "productType": "USDT-FUTURES",
                            "granularity": "1D", "limit": 200})
            return ms_to_date(d["data"][0][0]) if d.get("data") else None
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None
    return None


def backfill_dates(df, sleep=0.25):
    """Fill LaunchDate via klines wherever the instrument endpoint left it blank."""
    missing = df["LaunchDate"].isna()
    print(f"backfilling {missing.sum()} missing launch dates...")
    for i in df[missing].index:
        df.at[i, "LaunchDate"] = first_trade_date(df.at[i, "Exchange"],
                                                  df.at[i, "Pair"])
        df.at[i, "LaunchDate_source"] = "first daily kline"
        time.sleep(sleep)
    return df


# --------------------------------------------------------------------------
# CMC universe — fill the endpoint in from DevTools > Network > XHR
# --------------------------------------------------------------------------

CMC_TAG_ENDPOINT = None   # the XHR behind /view/tradfi-assets-derivatives/
CMC_PAIRS_ENDPOINT = None # the XHR behind /currencies/<slug>/markets/


def cmc_universe():
    """Returns the ~194 TradFi underlyings CMC tracks: slug, symbol, name."""
    if not CMC_TAG_ENDPOINT:
        raise SystemExit("Set CMC_TAG_ENDPOINT (see docstring).")
    rows = []
    for page in (1, 2):
        d = get(CMC_TAG_ENDPOINT, params={"start": (page - 1) * 100 + 1,
                                          "limit": 100})
        for c in d["data"]["cryptoCurrencyList"]:
            rows.append({"slug": c["slug"], "cmc_symbol": c["symbol"],
                         "cmc_name": c["name"]})
    return pd.DataFrame(rows)


def cmc_markets(slug):
    """Every exchange + pair CMC shows for one underlying."""
    if not CMC_PAIRS_ENDPOINT:
        raise SystemExit("Set CMC_PAIRS_ENDPOINT (see docstring).")
    d = get(CMC_PAIRS_ENDPOINT, params={"slug": slug, "start": 1, "limit": 200,
                                        "category": "perpetual"})
    return [{"cmc_slug": slug,
             "Exchange": m.get("exchangeName"),
             "Pair": m.get("marketPair"),
             "24h Volume (USDT)": m.get("volumeUsd")}
            for m in d["data"]["marketPairs"]]


# --------------------------------------------------------------------------

def normalize(df):
    """Join key: exchange + uppercased pair with separators stripped.

    Needed because the same contract is BTCUSDT / BTC_USDT / BTC-USDT-SWAP /
    BTC-USDT depending on venue.
    """
    df["join_key"] = (df["Exchange"].str.lower().str.strip() + "|" +
                      df["Pair"].str.upper()
                        .str.replace(r"[-_/:]|SWAP|PERP", "", regex=True))
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="universe.csv")
    ap.add_argument("--existing", help="current tracker CSV, to diff against")
    ap.add_argument("--backfill", action="store_true",
                    help="recover missing launch dates from klines (slow)")
    args = ap.parse_args()

    frames = []
    for name, fn in FETCHERS.items():
        try:
            rows = fn()
            print(f"{name:16} {len(rows):>5} instruments")
            frames.append(pd.DataFrame(rows))
        except Exception as e:
            print(f"{name:16} FAILED: {type(e).__name__}: {e}")

    df = normalize(pd.concat(frames, ignore_index=True))

    if args.backfill:
        df = backfill_dates(df)

    if args.existing:
        have = normalize(pd.read_csv(args.existing))
        new = df[~df["join_key"].isin(have["join_key"])]
        print(f"\n{len(new)} contracts not in existing sheet")
        new.to_csv("new_contracts.csv", index=False)

    df.to_csv(args.out, index=False)
    print(f"\nwrote {len(df)} rows -> {args.out}")


if __name__ == "__main__":
    main()