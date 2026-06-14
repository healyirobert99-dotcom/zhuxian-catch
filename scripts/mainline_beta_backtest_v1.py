"""
主线Beta正式回测 v1.1 — Phase 1-4: 状态机重放 + 独立周期事件研究

核心变更：按行业—主线周期(episode)统计，首次CONFIRMED只计一次。
主要终点：首次CONFIRMED后40日行业等权超额（用收盘价复利计算）。

用法:
    python scripts/mainline_beta_backtest_v1.py          # 全量
    python scripts/mainline_beta_backtest_v1.py --quick  # 快速（2023年后）
"""

import argparse
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
DB_PATH = Path("D:/stock-data/a_stock_selector.sqlite3")
OUT_DIR = BASE / "reports" / "mainline_beta_backtest_v1"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PRIMARY_HORIZON = 40

STATE_MAP = {
    "A级主线": "CONFIRMED", "B级主线": "NEAR_CONFIRMED", "C级观察": "CANDIDATE",
    "企稳重估": "STABILIZING", "退潮主线": "DISTRIBUTING", "退潮": "DOWNGRADED",
    "低频监控": "REMOVED", "暂不观察": "REMOVED",
}


def load_db(con):
    """加载个股日线 + 行业映射 → 行业日线（含等权收盘指数）。"""
    basic = pd.read_sql_query("SELECT symbol, name, industry FROM stock_basic WHERE industry IS NOT NULL AND industry!=''", con)
    stocks = pd.read_sql_query("SELECT symbol, date, close, pct_chg FROM stock_daily WHERE source IS NULL OR source!='tushare_fund_daily'", con)
    stocks["date"] = pd.to_datetime(stocks["date"])
    stocks = stocks.merge(basic[["symbol", "industry"]], on="symbol")

    # 行业等权指数
    ind_close = stocks.groupby(["industry", "date"])["close"].mean().reset_index()
    ind_close.columns = ["industry", "date", "eq_close"]

    # 行业统计（用于评分）
    stocks["close20"] = stocks.groupby("symbol")["close"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    stocks["close60"] = stocks.groupby("symbol")["close"].transform(lambda x: x.rolling(60, min_periods=30).mean())
    stocks["above_ma20"] = (stocks["close"] > stocks["close20"]).astype(int)
    stats = stocks.groupby(["industry", "date"]).agg(
        avg_pct=("pct_chg", "mean"),
        up_ratio=("pct_chg", lambda x: (x > 0).mean()),
        above_ma20_ratio=("above_ma20", "mean"),
    ).reset_index()

    # 合并
    industry = stats.merge(ind_close, on=["industry", "date"], how="left")
    return industry


def score_market(industry, trade_date):
    """市场环境分（冻结合逻辑）。"""
    day = industry[industry["date"] == trade_date]
    if day.empty:
        return {"score": 50}
    up = day["up_ratio"].mean()
    ma20 = day["above_ma20_ratio"].mean()
    score = up * 35 + ma20 * 35 + 0.2 * 30
    return {"score": min(100, max(0, score)), "up_ratio": up, "above_ma20": ma20}


def score_industries(industry, trade_date):
    """行业评分（动量+宽度）。"""
    today = industry[industry["date"] == trade_date].copy()
    past = industry[industry["date"] < trade_date]
    ret5 = past[past["date"] >= trade_date - pd.Timedelta(days=7)].groupby("industry")["avg_pct"].sum()
    ret20 = past[past["date"] >= trade_date - pd.Timedelta(days=25)].groupby("industry")["avg_pct"].sum()
    if today.empty:
        return pd.DataFrame()
    today = today.set_index("industry")
    today["ret5"] = ret5
    today["ret20"] = ret20
    today = today.dropna(subset=["ret5", "ret20"])
    today["score"] = (today["ret5"].rank(pct=True) * 30 + today["ret20"].rank(pct=True) * 40 + today["above_ma20_ratio"].rank(pct=True) * 30)
    today = today.reset_index()
    today["date"] = trade_date
    return today.sort_values("score", ascending=False)


def replay_states(industry):
    """逐日重放状态机。"""
    dates = sorted(industry["date"].unique())
    rows, prev_grade = [], {}
    for i, d in enumerate(dates):
        scores = score_industries(industry, d)
        mkt = score_market(industry, d)
        if scores.empty:
            continue
        for rank_idx, (_, r) in enumerate(scores.iterrows()):
            ind = r["industry"]
            prev = prev_grade.get(ind, "")
            s = r["score"]
            rk = rank_idx + 1  # 1-based rank from scoring
            if s >= 90 and rk <= 3:
                g = "A级主线"
            elif s >= 80 and rk <= 5:
                g = "B级主线"
            elif s >= 60 and rk <= 10:
                g = "C级观察"
            elif s >= 50:
                g = "C级观察"
            elif prev in ["A级主线", "B级主线", "C级观察"] and s >= 40:
                g = prev
            else:
                g = "退潮"
            prev_grade[ind] = g
            rows.append({"date": d, "industry": ind, "mainline_grade": g, "momentum_score": s, "market_score": mkt["score"], "up_ratio": r["up_ratio"], "above_ma20": r["above_ma20_ratio"], "ret20": r["ret20"], "ret5": r["ret5"]})
    return pd.DataFrame(rows)


def build_episodes(states):
    """压缩为独立周期。"""
    states = states.sort_values(["industry", "date"])
    states["uni"] = states["mainline_grade"].map(STATE_MAP).fillna("NONE")
    episodes = []
    for ind, grp in states.groupby("industry"):
        in_ep, seen, ep_id = False, set(), 0
        for _, r in grp.iterrows():
            s = r["uni"]
            if not in_ep and s in ("CANDIDATE", "NEAR_CONFIRMED", "CONFIRMED", "STABILIZING"):
                in_ep, ep_id = True, ep_id + 1
                seen = set()
            if in_ep and s in ("CONFIRMED", "NEAR_CONFIRMED", "CANDIDATE", "STABILIZING", "DISTRIBUTING", "DOWNGRADED"):
                if s not in seen:
                    seen.add(s)
                    episodes.append({"industry": ind, "episode_id": f"{ind}_{ep_id}", "state": s, "state_date": r["date"], "market_score": r["market_score"], "momentum_score": r["momentum_score"], "ret20": r["ret20"], "above_ma20": r["above_ma20"]})
            if s in ("REMOVED", "DOWNGRADED"):
                in_ep = False
    return pd.DataFrame(episodes)


def compute_forward(episodes, industry, horizons):
    """用等权收盘指数计算远期收益。"""
    c = industry.pivot(index="date", columns="industry", values="eq_close")
    eq = c.mean(axis=1)  # 等权行业指数
    dates = c.index.tolist()
    results = []
    for _, evt in episodes.iterrows():
        d, ind = evt["state_date"], evt["industry"]
        idx = dates.index(d) if d in dates else -1
        row = {"industry": ind, "episode_id": evt["episode_id"], "state": evt["state"], "state_date": d, "market_score": evt["market_score"]}
        for h in horizons:
            tgt = idx + h if idx >= 0 and idx + h < len(dates) else -1
            if tgt > 0 and ind in c.columns:
                ind_ret = c.iloc[tgt][ind] / c.iloc[idx][ind] - 1
                eq_ret = eq.iloc[tgt] / eq.iloc[idx] - 1
                row[f"abs_ret_{h}d"] = ind_ret
                row[f"excess_{h}d"] = ind_ret - eq_ret
            else:
                row[f"abs_ret_{h}d"] = row[f"excess_{h}d"] = np.nan
        # MFE (max favorable excursion)
        if tgt > 0 and ind in c.columns:
            fwd = c.iloc[idx:tgt+1][ind]
            row["mfe_40d"] = fwd.max() / fwd.iloc[0] - 1
            row["mae_40d"] = fwd.min() / fwd.iloc[0] - 1
        results.append(row)
    return pd.DataFrame(results)


def bootstrap(values, n=2000):
    m = [np.random.choice(values, len(values), replace=True).mean() for _ in range(n)]
    return {"mean": np.mean(values), "median": np.median(values), "ci_low": np.percentile(m, 2.5), "ci_high": np.percentile(m, 97.5), "win_rate": (values > 0).mean(), "n": len(values)}


def baselines(industry, horizons):
    """基准：等权、动量20、动量60、随机。"""
    dates = sorted(industry["date"].unique())
    np.random.seed(42)
    c = industry.pivot(index="date", columns="industry", values="eq_close")
    eq = c.mean(axis=1)
    all_inds = industry["industry"].unique()
    results = {name: {h: [] for h in horizons} for name in ["eq_wt", "mom20", "mom60", "random"]}
    for _ in range(2000):
        i = np.random.randint(0, len(dates) - max(horizons))
        d = dates[i]
        for h in horizons:
            j = i + h if i + h < len(dates) else -1
            if j > 0:
                results["eq_wt"][h].append(eq.iloc[j] / eq.iloc[i] - 1)
            # mom20
            idx_mask = [k for k in range(max(0,i-20), i+1)]
            mom20_ret = pd.Series({ind: c.iloc[i][ind]/c.iloc[idx_mask[0]][ind]-1 if ind in c.columns else np.nan for ind in all_inds}).dropna()
            if len(mom20_ret) > 0:
                top = mom20_ret.idxmax()
                if j>0 and top in c.columns and pd.notna(c.iloc[j][top]):
                    results["mom20"][h].append(c.iloc[j][top]/c.iloc[i][top]-1)
            # random
            rind = np.random.choice(all_inds)
            if j>0 and rind in c.columns and pd.notna(c.iloc[j][rind]):
                results["random"][h].append(c.iloc[j][rind]/c.iloc[i][rind]-1)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    con = sqlite3.connect(DB_PATH)
    t0 = time.time()
    print("=" * 55)
    print("主线Beta回测 v1.1 — 独立周期事件研究")
    print("=" * 55)

    print("\n[1/4] 加载行业日线...")
    industry = load_db(con)
    print(f"  {len(industry)}行, {industry['date'].nunique()}天, {industry['industry'].nunique()}行业")
    if args.quick:
        industry = industry[industry["date"] >= "2023-01-01"]

    print("\n[2/4] 状态机重放...")
    states = replay_states(industry)
    print(f"  {len(states)}条, {states['date'].min().date()}~{states['date'].max().date()}")

    print("\n[3/4] 独立周期压缩...")
    episodes = build_episodes(states)
    for s in ["CONFIRMED", "NEAR_CONFIRMED", "CANDIDATE", "STABILIZING"]:
        n = len(episodes[episodes["state"] == s])
        ni = episodes[episodes["state"] == s]["industry"].nunique()
        print(f"  {s}: {n}次, {ni}行业")
    ne = episodes["episode_id"].nunique()
    print(f"  独立周期: {ne}个")

    print("\n[4/4] 远期收益...")
    fwd = compute_forward(episodes, industry, [5,10,20,40,60])

    # ── 主要结论 ──
    cf = fwd[(fwd["state"] == "CONFIRMED") & fwd["excess_40d"].notna()]
    if len(cf) > 0:
        b = bootstrap(cf["excess_40d"].values)
        print(f"\n{'='*55}")
        print(f"  CONFIRMED后40日行业等权超额")
        print(f"  事件数: {b['n']}")
        print(f"  均值: {b['mean']:.2%} [95%CI: {b['ci_low']:.2%}~{b['ci_high']:.2%}]")
        print(f"  中位数: {b['median']:.2%}")
        print(f"  胜率: {b['win_rate']:.1%}")

    # 各状态对比
    print(f"\n{'状态':<18} {'N':>5} {'均值':>8} {'中位':>8} {'胜率':>7}")
    print("-" * 48)
    for s in ["CANDIDATE", "NEAR_CONFIRMED", "CONFIRMED", "STABILIZING", "DISTRIBUTING"]:
        sub = fwd[(fwd["state"] == s) & fwd["excess_40d"].notna()]
        if len(sub) >= 10:
            bs = bootstrap(sub["excess_40d"].values)
            print(f"{s:<18} {bs['n']:>5} {bs['mean']:>8.2%} {bs['median']:>8.2%} {bs['win_rate']:>6.1%}")

    # 基准
    bl = baselines(industry, [20,40,60])
    print(f"\n{'基准':<15} {'N':>6} {'40d均值':>9} {'胜率':>7}")
    print("-" * 38)
    for name in ["eq_wt", "mom20", "mom60", "random"]:
        if 40 in bl[name] and bl[name][40]:
            bv = bootstrap(np.array(bl[name][40]))
            print(f"{name:<15} {bv['n']:>6} {bv['mean']:>9.2%} {bv['win_rate']:>6.1%}")

    print(f"\n耗时: {(time.time()-t0)/60:.1f}分")
    con.close()


if __name__ == "__main__":
    main()
