#!/usr/bin/env python3
"""
prepare_data.py - Normalize broker market data for the Nyao Scalper research harness.

Sources (public GitHub repos, downloaded manually into ./_raw):

  A) XAUUSD M1, 2026 YTD           -> https://github.com/vudo805/XAUUSD-M1
     data/XAUUSD_M1_2026.csv        (columns: timestamp,datetime,open,high,low,close,volume)
     Prices carry 3 decimals  -> point = 0.001

  B) XAUUSD M1, FTMO-Server4       -> https://github.com/Gianniskas/mt5-m1-bars-us500-eurusd-xauusd
     XAUUSD_m1.csv                  (columns: datetime,open,high,low,close,volume; UTC, mid-price)
     spreads_summary.csv            (hourly live spread distribution, points, point = 0.01)
     symbol_specs.csv               (broker contract specs)
     Prices carry 2 decimals  -> point = 0.01

  C) XAUUSDm M5, 2023-12..2026-08  -> https://github.com/Anilk2978/xauusd-csv
     XAUUSDm_M5_*.csv               (MT5 tab export, includes <SPREAD> in points, point = 0.001)

Output (research/data/):
  <name>.csv  -> datetime,open,high,low,close,spread   (spread = bid/ask distance in PRICE units)
  <name>.json -> symbol/broker meta (point, digits, contract size, tick value/size, lots, window)

Run:  python prepare_data.py --raw /home/user/data --out ../data
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- windows ----
# We keep 3 months of "tradeable" data in every file plus 10 days of warm-up
# (indicators need history; the engine only trades inside the trade window).
WINDOWS = {
    "xauusd_m1_2026H2": dict(  # PRIMARY: most recent 3 months available
        src="vudo805", symbol="XAUUSD", point=0.001, digits=3,
        trade_start="2026-05-18", trade_end="2026-08-17",
        warmup_start="2026-05-08",
    ),
    "xauusd_m1_ftmo": dict(   # SECONDARY: independent broker / measurement
        src="ftmo", symbol="XAUUSD", point=0.01, digits=2,
        trade_start="2026-02-16", trade_end="2026-05-16",
        warmup_start="2026-02-06",
    ),
    "xauusdm_m5_2026H2": dict(  # TIMEFRAME CHECK: M5 feed, has its own spread column
        src="m5", symbol="XAUUSDm", point=0.001, digits=3,
        trade_start="2026-05-18", trade_end="2026-08-22",
        warmup_start="2026-05-08",
    ),
}

# FTMO XAUUSD hourly median spread (points of 0.01) -> used when a source has
# no per-bar spread of its own.  Values come from spreads_summary.csv.
FTMO_FALLBACK_SPREAD_POINTS = 45.0


def load_raw(raw_dir: str, src: str) -> pd.DataFrame:
    if src == "vudo805":
        df = pd.read_csv(os.path.join(raw_dir, "XAUUSD_M1_2026.csv"),
                         usecols=["datetime", "open", "high", "low", "close"])
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        df["spread"] = np.nan
        return df
    if src == "ftmo":
        df = pd.read_csv(os.path.join(raw_dir, "ftmo", "XAUUSD_m1.csv"))
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
        df["spread"] = np.nan
        return df
    if src == "m5":
        path = os.path.join(raw_dir, "m5", "xauusd-csv-main",
                            "XAUUSDm_M5_202312010705_202608211705.csv")
        df = pd.read_csv(path, sep="\t")
        df = df.rename(columns={"<DATE>": "date", "<TIME>": "time", "<OPEN>": "open",
                                "<HIGH>": "high", "<LOW>": "low", "<CLOSE>": "close",
                                "<SPREAD>": "spread_points"})
        df["datetime"] = pd.to_datetime(df["date"] + " " + df["time"])
        df["spread"] = df["spread_points"] * 0.001   # XAUUSDm point = 0.001
        return df[["datetime", "open", "high", "low", "close", "spread"]]
    raise ValueError(src)


def hourly_spread_profile(raw_dir: str) -> dict:
    """FTMO hourly median spread (in USD of price) keyed by UTC hour."""
    p = os.path.join(raw_dir, "ftmo", "spreads_summary.csv")
    df = pd.read_csv(p)
    df = df[df.symbol == "XAUUSD"]
    prof = {int(r.hour_utc): float(r.p50) * 0.01 for _, r in df.iterrows()}
    return prof


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="/home/user/data")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    args = ap.parse_args()

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)
    prof = hourly_spread_profile(args.raw)
    fallback = FTMO_FALLBACK_SPREAD_POINTS * 0.01

    cache = {}
    for name, cfg in WINDOWS.items():
        key = cfg["src"]
        if key not in cache:
            cache[key] = load_raw(args.raw, key)
        df = cache[key]
        df = df[(df.datetime >= cfg["warmup_start"]) & (df.datetime < cfg["trade_end"])].copy()
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)

        # Spread model
        if df["spread"].isna().all():
            df["spread"] = df.datetime.dt.hour.map(lambda h: prof.get(h, fallback))
            spread_src = "FTMO hourly median (p50)"
        else:
            df["spread"] = df["spread"].fillna(fallback)
            # never let a stale/zero spread through
            df.loc[df["spread"] <= 0, "spread"] = fallback
            spread_src = "broker <SPREAD> column"

        df = df[["datetime", "open", "high", "low", "close", "spread"]]
        csv_path = os.path.join(outdir, name + ".csv")
        df.to_csv(csv_path, index=False)

        trade_rows = ((df.datetime >= cfg["trade_start"]) & (df.datetime < cfg["trade_end"])).sum()
        meta = dict(
            name=name, symbol=cfg["symbol"], source=cfg["src"],
            point=cfg["point"], digits=cfg["digits"],
            contract_size=100.0, tick_value=1.0, tick_size=0.01,  # XAUUSD 100 oz, $1 per 0.01
            volume_min=0.01, volume_max=100.0, volume_step=0.01,
            spread_model=spread_src,
            spread_mean_price=float(df.spread.mean()),
            spread_p50_price=float(df.spread.median()),
            trade_start=cfg["trade_start"], trade_end=cfg["trade_end"],
            warmup_start=cfg["warmup_start"],
            bars_total=int(len(df)), bars_tradeable=int(trade_rows),
            first_bar=str(df.datetime.iloc[0]), last_bar=str(df.datetime.iloc[-1]),
        )
        with open(os.path.join(outdir, name + ".json"), "w") as fh:
            json.dump(meta, fh, indent=2)
        print(f"[ok] {name}: {len(df):>7} bars ({trade_rows} tradeable)  "
              f"spread p50={meta['spread_p50_price']:.3f} USD  -> {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
