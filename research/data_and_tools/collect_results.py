#!/usr/bin/env python3
"""
collect_results.py — final numbers used in the report:

  1. equal 30-day "loop" windows on the primary M1 tape (5 profiles + tuned set)
  2. cost sensitivity (spread multiplier x stop-fill slippage)
  3. trade anatomy of the tuned configuration
  4. writes research/results/results_tables.md and a ready-to-load .set file
"""
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nyao_engine import PROFILE_FILES, load_set_file, run_backtest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
RESULTS = os.path.abspath(os.path.join(HERE, "..", "results"))
SETTINGS = os.path.abspath(os.path.join(HERE, "..", "..", "settings"))

DATASETS = {
    "M1-primary": "xauusd_m1_2026H2",
    "M1-FTMO": "xauusd_m1_ftmo",
    "M5-vudo": "xauusdm_m5_2026H2",
}
PROFILES = list(PROFILE_FILES.keys())

# Tuned configuration: winner of research/data_and_tools/optimize.py (selected on
# the May 18 - Jul 1 window, then validated untouched on Jul 1 - Aug 17).
TUNED = {
    "EnableHedgeChain": False,
    "EnableRiskReward": True, "RRRiskMode": 1, "RRAtrMultiplier": 2.0,
    "RiskRewardRatio": 1.5,
    "MinBuySignalScore": 3.5, "MinSellSignalScore": 3.5,
    "MaxSpreadATRRatio": 0.25, "MinVolRatioToTrade": 0.6,
    "TrailingValueMultiplier": 0.2, "MinBreakEvenProfit": 0.3,
    "ProfitThresholdMultiplier": 1.0, "MinHealthScore": 0.3, "SLValue": 0.5,
    "SignalSmoothingCandles": 1, "CurrentCandleBlend": 0.2,
}
TUNED_BASE = "default"

WIN_EDGES = ["2026-05-18", "2026-06-17", "2026-07-17", "2026-08-17"]


def load(ds):
    df = pd.read_csv(os.path.join(DATA, ds + ".csv"), parse_dates=["datetime"])
    meta = json.load(open(os.path.join(DATA, ds + ".json")))
    return df, meta


def run(df, meta, profile, tag, overrides=None, slippage=0.0, spread_mult=1.0):
    m = dict(meta)
    m["trade_start"] = tag
    res, _ = run_backtest(None, None, profile, df=df, meta=m, overrides=overrides,
                          slippage=slippage, spread_mult=spread_mult, label=profile)
    return res


def fmt(res):
    s = res.stats
    return dict(net=s["net_profit"], net_pct=s["net_profit_pct"], trades=s["trades"],
                win_rate=s["win_rate"], pf=s["profit_factor"], dd_pct=s["max_dd_pct"],
                avg=s["avg_trade"], final=s["final_balance"],
                min_equity=s["min_equity"], reasons=s.get("exit_reasons", {}))


def main():
    out = {}

    # ---- 1. loop windows ---------------------------------------------------
    loops = {}
    for dsname, ds in DATASETS.items():
        df, meta = load(ds)
        loops[dsname] = {}
        for i in range(len(WIN_EDGES) - 1):
            tag = WIN_EDGES[i]
            key = f"{WIN_EDGES[i]}..{WIN_EDGES[i+1]}"
            loops[dsname][key] = {}
            for prof in PROFILES + ["TUNED"]:
                if prof == "TUNED":
                    res = run(df, meta, TUNED_BASE, tag, overrides=TUNED)
                else:
                    res = run(df, meta, prof, tag)
                loops[dsname][key][prof] = fmt(res)
                print(f"[{dsname}] {key} {prof:16s} net={res.stats['net_profit']:>9.1f} "
                      f"({res.stats['net_profit_pct']:>7.1f}%) trades={res.stats['trades']:>5d} "
                      f"DD={res.stats['max_dd_pct']:>5.1f}%")
    out["loop_windows"] = loops

    # ---- 2. whole-window results per dataset -------------------------------
    overall = {}
    for dsname, ds in DATASETS.items():
        df, meta = load(ds)
        overall[dsname] = {}
        for prof in PROFILES + ["TUNED"]:
            res = run(df, meta, TUNED_BASE if prof == "TUNED" else prof, meta["trade_start"],
                      overrides=TUNED if prof == "TUNED" else None)
            overall[dsname][prof] = fmt(res)
            overall[dsname][prof]["diag"] = {k: v for k, v in res.diagnostics.items() if v}
            print(f"[{dsname}] FULL {prof:16s} net={res.stats['net_profit']:>9.1f} "
                  f"DD={res.stats['max_dd_pct']:>5.1f}% PF={res.stats['profit_factor']}")
    out["overall"] = overall

    # ---- 3. cost sensitivity (primary tape, full window) -------------------
    df, meta = load("xauusd_m1_2026H2")
    sens = {}
    for label, prof, ov in [("default(shipped)", "default", None),
                            ("safe(shipped)", "safe", None),
                            ("TUNED", TUNED_BASE, TUNED)]:
        sens[label] = {}
        for sm in (0.5, 1.0, 1.5, 2.0):
            for slip in (0.0, 0.05, 0.10):
                res = run(df, meta, prof, meta["trade_start"], overrides=ov,
                          slippage=slip, spread_mult=sm)
                sens[label][f"spread_x{sm}_slip{slip}"] = dict(
                    net=res.stats["net_profit"], dd=res.stats["max_dd_pct"],
                    trades=res.stats["trades"], pf=res.stats["profit_factor"])
                print(f"[cost] {label:16s} spread x{sm:<4} slip {slip:<4} "
                      f"net={res.stats['net_profit']:>9.1f} PF={res.stats['profit_factor']} "
                      f"DD={res.stats['max_dd_pct']:>5.1f}%")
    out["cost_sensitivity"] = sens

    # ---- 4. trade anatomy of the tuned set on the primary tape -------------
    res = run(df, meta, TUNED_BASE, meta["trade_start"], overrides=TUNED)
    tr = pd.DataFrame([t.__dict__ for t in res.trades])
    wins, losses = tr[tr.profit > 0], tr[tr.profit <= 0]
    out["tuned_anatomy"] = dict(
        trades=len(tr), win_rate=res.stats["win_rate"],
        avg_win=round(wins.profit.mean(), 3), avg_loss=round(losses.profit.mean(), 3),
        best=round(tr.profit.max(), 2), worst=round(tr.profit.min(), 2),
        median_bars=int(tr.bars_held.median()),
        mean_bars=round(tr.bars_held.mean(), 2),
        by_reason=tr.groupby("reason").profit.agg(["count", "mean", "sum"]).round(2)
                    .to_dict("index"),
        lot_counts=tr.lot.value_counts().to_dict(),
        spread_cost_paid=round(res.diagnostics["spread_cost_paid"], 1),
        gross_profit=res.stats["gross_profit"], gross_loss=res.stats["gross_loss"],
        equity_start=res.initial_balance, equity_end=res.final_balance,
    )
    # monthly P/L of the tuned set
    tr["month"] = pd.to_datetime(tr.exit_time).dt.to_period("M").astype(str)
    out["tuned_by_month"] = tr.groupby("month").profit.agg(["count", "sum"]).round(2)\
        .to_dict("index")
    curve = pd.DataFrame(res.equity_curve, columns=["ts", "equity"])
    curve["ts"] = pd.to_datetime(curve.ts, unit="s")
    curve.to_csv(os.path.join(RESULTS, "tuned_equity_curve.csv"), index=False)
    out["tuned_equity"] = dict(start=float(curve.equity.iloc[0]),
                               end=float(curve.equity.iloc[-1]),
                               max=float(curve.equity.max()),
                               min=float(curve.equity.min()))

    # ---- 5. write a ready-to-load .set for the tuned configuration ---------
    base = load_set_file(os.path.join(SETTINGS, "default.set"))
    base.update(TUNED)
    tset_dir = os.path.join(HERE, "..", "recommended_settings")
    os.makedirs(tset_dir, exist_ok=True)
    path = os.path.join(tset_dir, "tuned-lowrisk-nomartingale.set")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("; Nyao Scalper - tuned low-risk configuration (research result)\n")
        fh.write("; base: settings/default.set + walk-forward tuned overrides\n")
        fh.write("; source: research/BACKTEST_REPORT_zh.md\n")
        for k, v in base.items():
            if isinstance(v, bool):
                fh.write(f"{k}={'true' if v else 'false'}\n")
            else:
                fh.write(f"{k}={v}\n")
    print("\nwrote", path)

    with open(os.path.join(RESULTS, "final_tables.json"), "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("wrote", os.path.join(RESULTS, "final_tables.json"))


if __name__ == "__main__":
    main()
