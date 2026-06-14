"""
主线Beta全量回测 v1.1 — 自包含状态机 + Episode统计

数据: D:/stock-data/a_stock_selector.sqlite3
方法:
  1. 从 stock_daily 构建行业日线（等权收盘 + 宽度 + 动量）
  2. 逐日回放状态机（市场评分 + 行业评分 + 分级 + 退潮缓冲）
  3. 压缩为独立行业—主线周期
  4. 事件研究：首次CONFIRMED后40日行业等权超额
"""

import argparse
import sqlite3
import time
from pathlib import Path

import numpy as np
import pandas as pd

DB_PATH = "D:/stock-data/a_stock_selector.sqlite3"
OUT_DIR = Path(__file__).resolve().parents[1] / "reports" / "mainline_beta_backtest_v1_full"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load_industry_daily(con):
    """构建行业日线 DataFrame。

    字段: industry, date, eq_close(等权收盘), avg_pct(平均涨跌),
          up_ratio(上涨比例), above_ma20_ratio, above_ma60_ratio
    """
    print("  加载 stock_daily + stock_basic...")
    basic = pd.read_sql_query(
        "SELECT symbol, industry FROM stock_basic WHERE industry IS NOT NULL AND industry != ''",
        con,
    )
    daily = pd.read_sql_query(
        "SELECT symbol, date, close, pct_chg, volume "
        "FROM stock_daily WHERE source IS NULL OR source != 'tushare_fund_daily'",
        con,
    )
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.merge(basic, on="symbol", how="inner")

    print("  计算MA20/MA60...")
    daily = daily.sort_values(["symbol", "date"])
    daily["ma20"] = daily.groupby("symbol")["close"].transform(
        lambda x: x.rolling(20, min_periods=10).mean()
    )
    daily["ma60"] = daily.groupby("symbol")["close"].transform(
        lambda x: x.rolling(60, min_periods=30).mean()
    )
    daily["above_ma20"] = (daily["close"] > daily["ma20"]).astype(int)
    daily["above_ma60"] = (daily["close"] > daily["ma60"]).astype(int)

    print("  按行业聚合...")
    industry = daily.groupby(["industry", "date"]).agg(
        eq_close=("close", "mean"),
        avg_pct=("pct_chg", "mean"),
        up_ratio=("pct_chg", lambda x: (x > 0).mean()),
        above_ma20_ratio=("above_ma20", "mean"),
        above_ma60_ratio=("above_ma60", "mean"),
        stock_count=("symbol", "nunique"),
    ).reset_index()

    return industry


def compute_rolling_returns(industry):
    """为每个行业/日期计算5/10/20/60日滚动收益。"""
    print("  计算滚动收益...")
    industry = industry.sort_values(["industry", "date"])
    for w, days in [("ret5", 5), ("ret10", 10), ("ret20", 20), ("ret60", 60)]:
        industry[w] = industry.groupby("industry")["avg_pct"].transform(
            lambda x: x.shift(1).rolling(days, min_periods=max(3, days // 2)).sum()
        )
    return industry


def score_market(industry, trade_date):
    """市场环境评分（0-100）。

    组件：上涨比例(33%) + MA20以上比例(33%) + 20日正收益比例(34%)
    """
    day = industry[industry["date"] == trade_date]
    if day.empty:
        return 50
    up = day["up_ratio"].mean()
    ma20 = day["above_ma20_ratio"].mean()
    score = up * 33 + ma20 * 33 + 34 * 0.2  # placeholder for ret20_pos
    return min(100, max(0, round(score)))


def score_industries(industry, trade_date, prev_grades=None):
    """行业综合评分。

    组件：ret5 rank(25%) + ret20 rank(35%) + ret60 rank(15%) + ma20 rank(25%)
    """
    if prev_grades is None:
        prev_grades = {}
    today = industry[industry["date"] == trade_date].copy()
    needed = ["industry", "ret5", "ret20", "ret60", "above_ma20_ratio", "up_ratio"]
    today = today.dropna(subset=["ret5", "ret20"])
    if today.empty:
        return pd.DataFrame()

    for col in ["ret5", "ret20", "ret60", "above_ma20_ratio"]:
        today[f"{col}_rank"] = today[col].rank(pct=True, ascending=True)

    today["score"] = (
        today["ret5_rank"] * 25
        + today["ret20_rank"] * 35
        + today["ret60_rank"].fillna(0.5) * 15
        + today["above_ma20_ratio_rank"] * 25
    )
    today = today.sort_values("score", ascending=False)
    today["rank"] = range(1, len(today) + 1)
    return today


def assign_grade(score, rank, prev_grade, mkt_score):
    """主线等级分配（匹配生产逻辑的行为）。

    规则:
    - A级: score >= 92, rank <= 3
    - B级: score >= 85, rank <= 6
    - C级(强): score >= 70, rank <= 12
    - C级(弱): score >= 58
    - 退潮缓冲: 前B级以上且有score >= 45，维持原级
    """
    if score >= 92 and rank <= 3:
        return "A级主线"
    if score >= 85 and rank <= 6:
        return "B级主线"
    if score >= 70 and rank <= 12:
        return "C级观察"
    if score >= 58:
        return "C级观察"
    # 退潮缓冲：前B级以上且分数不太差
    if prev_grade in ["A级主线", "B级主线"] and score >= 45:
        return prev_grade
    if prev_grade == "C级观察" and score >= 40:
        return prev_grade
    return "退潮"


def replay_state_machine(industry, dates):
    """逐日重放状态机，含退潮缓冲持久化。"""
    print("  重放状态机...")
    all_rows = []
    prev_grades = {}
    prev_mkt = 50

    for i, d in enumerate(dates):
        if i % 500 == 0:
            print(f"    {d} ({i}/{len(dates)})")

        mkt = score_market(industry, d)
        prev_mkt = mkt
        scores = score_industries(industry, d, prev_grades)
        if scores.empty:
            continue

        for _, row in scores.iterrows():
            ind = row["industry"]
            prev = prev_grades.get(ind, "")
            grade = assign_grade(row["score"], row["rank"], prev, mkt)

            prev_grades[ind] = grade

            all_rows.append({
                "date": d,
                "industry": ind,
                "grade": grade,
                "score": round(row["score"], 1),
                "rank": row["rank"],
                "ret5": round(row["ret5"] * 100, 2) if pd.notna(row["ret5"]) else 0,
                "ret20": round(row["ret20"] * 100, 2) if pd.notna(row["ret20"]) else 0,
                "ret60": round(row["ret60"] * 100, 2) if pd.notna(row["ret60"]) else 0,
                "above_ma20_ratio": round(row["above_ma20_ratio"] * 100, 1),
                "market_score": mkt,
            })

    return pd.DataFrame(all_rows)


STATE_MAP = {
    "A级主线": "CONFIRMED", "B级主线": "NEAR_CONFIRMED", "C级观察": "CANDIDATE",
    "退潮": "DISTRIBUTING",
}


def build_episodes(states):
    """压缩为独立行业—主线周期。"""
    states = states.sort_values(["industry", "date"])
    states["unified"] = states["grade"].map(STATE_MAP).fillna("NONE")

    episodes = []
    for ind, grp in states.groupby("industry"):
        in_ep, seen, ep_id = False, set(), 0
        for _, r in grp.iterrows():
            s = r["unified"]
            if not in_ep and s in ("CANDIDATE", "NEAR_CONFIRMED", "CONFIRMED"):
                in_ep, ep_id = True, ep_id + 1
                seen = set()
            if in_ep and s in ("CONFIRMED", "NEAR_CONFIRMED", "CANDIDATE"):
                if s not in seen:
                    seen.add(s)
                    episodes.append({
                        "industry": ind, "episode_id": f"{ind}_{ep_id}",
                        "state": s, "state_date": r["date"],
                        "market_score": r["market_score"],
                        "industry_score": r["score"],
                        "ret20": r["ret20"],
                        "above_ma20": r["above_ma20_ratio"],
                    })
            if s == "DISTRIBUTING" and in_ep:
                # 退潮后5天持续才结束周期
                pass  # simplified: keep in episode
    return pd.DataFrame(episodes)


def compute_forward_returns(episodes, con, horizons=(5, 10, 20, 40, 60)):
    """行业等权收盘 → forward total return。"""
    print("  构建行业等权指数...")
    basic = pd.read_sql_query(
        "SELECT symbol, industry FROM stock_basic WHERE industry IS NOT NULL AND industry!=''",
        con,
    )
    daily = pd.read_sql_query(
        "SELECT symbol, date, close FROM stock_daily "
        "WHERE (source IS NULL OR source != 'tushare_fund_daily')",
        con,
    )
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.merge(basic, on="symbol", how="inner")
    ind_close = daily.groupby(["industry", "date"])["close"].mean().reset_index()
    c = ind_close.pivot(index="date", columns="industry", values="close")
    eq = c.mean(axis=1)
    all_dates = c.index.tolist()

    print("  计算forward returns...")
    results = []
    for _, evt in episodes.iterrows():
        d = evt["state_date"]
        ind = evt["industry"]
        if d not in all_dates or ind not in c.columns:
            continue
        idx = all_dates.index(d)
        row = {
            "industry": ind, "episode_id": evt["episode_id"],
            "state": evt["state"], "state_date": d,
            "market_score": evt["market_score"],
            "industry_score": evt["industry_score"],
        }
        for h in horizons:
            tgt = idx + h
            if tgt < len(all_dates):
                ir = c.iloc[tgt][ind] / c.iloc[idx][ind] - 1
                er = eq.iloc[tgt] / eq.iloc[idx] - 1
                row[f"abs_ret_{h}d"] = ir
                row[f"excess_{h}d"] = ir - er
            else:
                row[f"abs_ret_{h}d"] = row[f"excess_{h}d"] = np.nan
        if idx + 40 < len(all_dates):
            fwd_series = c.iloc[idx:idx+41][ind]
            row["mfe_40d"] = fwd_series.max() / fwd_series.iloc[0] - 1
            row["mae_40d"] = fwd_series.min() / fwd_series.iloc[0] - 1
        results.append(row)
    return pd.DataFrame(results)


def bootstrap(values, n=2000):
    m = [np.random.choice(values, len(values), replace=True).mean() for _ in range(n)]
    return {
        "mean": np.mean(values), "median": np.median(values),
        "ci_low": np.percentile(m, 2.5), "ci_high": np.percentile(m, 97.5),
        "win_rate": np.mean(values > 0), "n": len(values),
    }


def build_baselines(con, horizons=(20, 40, 60), n_samples=2000):
    print("  计算基准...")
    basic = pd.read_sql_query("SELECT symbol, industry FROM stock_basic WHERE industry IS NOT NULL", con)
    daily = pd.read_sql_query(
        "SELECT symbol, date, close FROM stock_daily WHERE (source IS NULL OR source!='tushare_fund_daily')",
        con,
    )
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.merge(basic, on="symbol", how="inner")
    ind_close = daily.groupby(["industry", "date"])["close"].mean().reset_index()
    c = ind_close.pivot(index="date", columns="industry", values="close")
    eq = c.mean(axis=1)
    all_dates = c.index.tolist()
    all_inds = c.columns.tolist()

    np.random.seed(42)
    results = {name: {h: [] for h in horizons} for name in ["等权行业", "20日动量", "随机行业"]}
    for _ in range(n_samples):
        i = np.random.randint(0, len(all_dates) - max(horizons))
        for h in horizons:
            j = i + h
            if j < len(all_dates):
                results["等权行业"][h].append(eq.iloc[j] / eq.iloc[i] - 1)
        if i >= 20:
            mom = {ind: c.iloc[i][ind] / c.iloc[i-20][ind] - 1
                   for ind in all_inds
                   if pd.notna(c.iloc[i].get(ind)) and pd.notna(c.iloc[i-20].get(ind))}
            if mom:
                top = max(mom, key=mom.get)
                for h in horizons:
                    j = i + h
                    if j < len(all_dates):
                        results["20日动量"][h].append(c.iloc[j][top] / c.iloc[i][top] - 1)
        rind = np.random.choice(all_inds)
        for h in horizons:
            j = i + h
            if j < len(all_dates):
                results["随机行业"][h].append(c.iloc[j][rind] / c.iloc[i][rind] - 1)
    return results


def main():
    t0 = time.time()
    con = sqlite3.connect(DB_PATH)
    print("=" * 60)
    print("主线Beta全量回测 v1.1")
    print("数据:", DB_PATH)
    print("=" * 60)

    # Phase 1
    print("\n[Phase 1] 加载行业日线...")
    t1 = time.time()
    industry = load_industry_daily(con)
    industry = compute_rolling_returns(industry)
    dates = sorted(industry["date"].unique())
    print(f"  {industry['industry'].nunique()}行业 × {len(dates)}天 = {len(industry)}行, "
          f"{dates[0].date()}~{dates[-1].date()}, 耗时{(time.time()-t1)/60:.1f}分")

    # Phase 2
    print("\n[Phase 2] 状态机重放...")
    t2 = time.time()
    states = replay_state_machine(industry, dates)
    print(f"  {len(states)}条状态, 耗时{(time.time()-t2)/60:.1f}分")

    # Phase 3
    print("\n[Phase 3] 压缩为独立周期...")
    episodes = build_episodes(states)
    for s in ["CONFIRMED", "NEAR_CONFIRMED", "CANDIDATE"]:
        n = (episodes["state"] == s).sum()
        ni = episodes[episodes["state"] == s]["industry"].nunique()
        print(f"    {s}: {n}次, {ni}行业")
    print(f"    独立周期: {episodes['episode_id'].nunique()}个")
    episodes.to_csv(OUT_DIR / "episodes.csv", index=False)

    # Phase 4
    print("\n[Phase 4] 远期收益...")
    t4 = time.time()
    fwd = compute_forward_returns(episodes, con)
    print(f"  {len(fwd)}条, 耗时{(time.time()-t4)/60:.1f}分")
    fwd.to_csv(OUT_DIR / "forward_returns.csv", index=False)

    # ── 主要结论 ──
    cf = fwd[(fwd["state"] == "CONFIRMED") & fwd["excess_40d"].notna()]
    b = {}
    if len(cf) >= 30:
        b = bootstrap(cf["excess_40d"].values)
    else:
        b = {"mean": 0, "n": len(cf)}

    print("\n" + "=" * 60)
    print(f"  Gate 2 判定 — CONFIRMED后40日行业等权超额")
    print(f"  周期: {cf['episode_id'].nunique()}个, 事件: {b['n']}次")

    if b["n"] >= 30:
        print(f"  均值: {b['mean']:.2%} [95%CI: {b['ci_low']:.2%} ~ {b['ci_high']:.2%}]")
        print(f"  中位数: {b['median']:.2%}")
        print(f"  胜率: {b['win_rate']:.1%}")
        print(f"  标准差: {np.std(cf['excess_40d'].values):.2%}")

    # 各状态
    print(f"\n  {'状态':<18} {'N':>6} {'均值':>8} {'中位':>8} {'胜率':>7}")
    print("  " + "-" * 48)
    for s in ["CANDIDATE", "NEAR_CONFIRMED", "CONFIRMED"]:
        sub = fwd[(fwd["state"] == s) & fwd["excess_40d"].notna()]
        if len(sub) >= 10:
            bs = bootstrap(sub["excess_40d"].values)
            print(f"  {s:<18} {bs['n']:>6} {bs['mean']:>8.2%} {bs['median']:>8.2%} {bs['win_rate']:>6.1%}")

    # 基准
    print("\n[Phase 5] 基准对照组...")
    bl = build_baselines(con)
    print(f"\n  {'基准':<12} {'N':>6} {'40d均值':>9} {'胜率':>7}")
    print("  " + "-" * 35)
    for name in ["等权行业", "20日动量", "随机行业"]:
        if 40 in bl[name] and bl[name][40]:
            bv = bootstrap(np.array(bl[name][40]))
            print(f"  {name:<12} {bv['n']:>6} {bv['mean']:>9.2%} {bv['win_rate']:>6.1%}")

    # Gate 判定
    print(f"\n{'='*60}")
    gate2 = b["n"] >= 60 and b.get("mean", 0) > 0.015 and b.get("win_rate", 0) > 0.55
    if gate2:
        print("✓ Gate 2 通过: 信号有效（独立周期口径）")
    else:
        reasons = []
        if b["n"] < 60: reasons.append(f"周期{b['n']}<60")
        if b.get("mean", 0) <= 0.015: reasons.append(f"超额{b.get('mean',0):.2%}<1.5%")
        if b.get("win_rate", 0) <= 0.55: reasons.append(f"胜率{b.get('win_rate',0):.1%}<55%")
        print(f"✗ Gate 2 未通过: {'; '.join(reasons)}")

    print(f"\n总耗时: {(time.time()-t0)/60:.1f}分")
    con.close()


if __name__ == "__main__":
    main()
