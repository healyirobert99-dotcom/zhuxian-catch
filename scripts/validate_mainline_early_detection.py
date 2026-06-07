from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

import generate_daily_review as daily_review  # noqa: E402


DEFAULT_REPORT_DIR = BASE / "reports" / "mainline_early_detection_validation_5y"
HORIZONS = (5, 10, 20, 40, 60)
GRADE_RANK = {
    "A级主线": 0,
    "B级主线": 1,
    "C级观察": 2,
    "企稳重估": 3,
    "退潮主线": 4,
    "低频监控": 5,
    "暂不观察": 6,
}

warnings.filterwarnings("ignore", message="Mean of empty slice", category=RuntimeWarning)
warnings.filterwarnings("ignore", message="DataFrameGroupBy.apply operated on the grouping columns", category=FutureWarning)


def main() -> None:
    args = parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    start = pd.to_datetime(args.start).date().isoformat()
    end = pd.to_datetime(args.end).date().isoformat()
    samples = build_yearly_samples(start, end, args.cooldown_days, args.random_seed, report_dir)
    summary = summarize_samples(samples)
    year_summary = summarize_by_year(samples)
    signal_summary = summarize_signal_types(samples)

    samples.to_csv(report_dir / "early_mainline_samples.csv", index=False)
    summary.to_csv(report_dir / "early_mainline_summary.csv", index=False)
    year_summary.to_csv(report_dir / "early_mainline_year_summary.csv", index=False)
    signal_summary.to_csv(report_dir / "early_mainline_signal_type_summary.csv", index=False)
    (report_dir / "early_mainline_validation_report.md").write_text(
        render_report(samples, summary, year_summary, signal_summary, start, end),
        encoding="utf-8",
    )
    print(report_dir / "early_mainline_validation_report.md")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate early mainline detection using historical industry beta.")
    parser.add_argument("--start", default="2021-01-04")
    parser.add_argument("--end", default="2026-06-01")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--cooldown-days", type=int, default=20)
    parser.add_argument("--random-seed", type=int, default=42)
    return parser.parse_args()


def build_yearly_samples(start: str, end: str, cooldown_days: int, random_seed: int, report_dir: Path) -> pd.DataFrame:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    all_samples = []
    for year in range(start_ts.year, end_ts.year + 1):
        period_start = max(start_ts, pd.Timestamp(f"{year}-01-01"))
        period_end = min(end_ts, pd.Timestamp(f"{year}-12-31"))
        if period_start > period_end:
            continue
        history_start = (period_start - pd.DateOffset(months=18)).date().isoformat()
        calc_end = min(end_ts, period_end + pd.DateOffset(days=120)).date().isoformat()
        print(f"validating {year}: {period_start.date()} to {period_end.date()} (history from {history_start}, forward to {calc_end})", flush=True)
        history = daily_review.load_local_price_history(history_start, calc_end)
        prices = daily_review.build_indicators(history)
        metrics = daily_review.compute_industry_lifecycle_metrics(prices)
        lifecycle = build_daily_lifecycle_frame(metrics)
        lifecycle = lifecycle[(lifecycle["date"] >= period_start) & (lifecycle["date"] <= pd.Timestamp(calc_end))].copy()
        year_samples = build_validation_samples(lifecycle, cooldown_days, random_seed + year)
        year_samples = year_samples[(year_samples["date"] >= period_start) & (year_samples["date"] <= period_end)].copy()
        year_samples.to_csv(report_dir / f"early_mainline_samples_{year}.csv", index=False)
        all_samples.append(year_samples)
        print(f"finished {year}: {len(year_samples)} samples", flush=True)
    if not all_samples:
        raise SystemExit("No validation samples generated.")
    return pd.concat(all_samples, ignore_index=True)


def build_daily_lifecycle_frame(metrics: pd.DataFrame) -> pd.DataFrame:
    df = metrics.copy().sort_values(["industry", "date"])
    rank_cols = [
        "ret5",
        "ret10",
        "ret20",
        "ret60",
        "excess5",
        "excess20",
        "excess60",
        "above20",
        "amount5_60",
    ]
    for col in rank_cols:
        df[f"{col}_rank"] = df.groupby("date")[col].rank(pct=True)
    df["stage_score"] = (
        df["ret5_rank"].fillna(0) * 10
        + df["ret20_rank"].fillna(0) * 22
        + df["ret60_rank"].fillna(0) * 14
        + df["excess20_rank"].fillna(0) * 18
        + df["excess60_rank"].fillna(0) * 10
        + df["above20_rank"].fillna(0) * 16
        + df["amount5_60_rank"].fillna(0) * 10
    )
    df["stage"] = df.apply(validation_stage, axis=1)
    df["mainline_grade"] = df.apply(validation_grade, axis=1)
    df["status_explanation"] = df["stage"]
    df["env_score"] = df["date"].map(compute_market_scores(df))
    df["env_bucket"] = df["env_score"].map(environment_bucket)
    grouped = df.groupby("industry", group_keys=False)
    for col in ["mainline_grade", "stage", "stage_score", "ret5", "ret20", "ret60", "above20", "drawdown60"]:
        df[f"prev_{col}"] = grouped[col].shift(1)
    df["prev_grade_rank"] = df["prev_mainline_grade"].map(GRADE_RANK).fillna(6)
    df["grade_rank"] = df["mainline_grade"].map(GRADE_RANK).fillna(6)
    df["improved"] = df["grade_rank"] < df["prev_grade_rank"]
    df["year"] = df["date"].dt.year
    return df


def compute_market_scores(frame: pd.DataFrame) -> dict[pd.Timestamp, float]:
    scores = {}
    for date, snap in frame.groupby("date"):
        pos20 = (snap["ret20"] > 0).mean()
        above20 = snap["above20"].mean()
        ret20_med = snap["ret20"].median()
        amount_med = snap["amount5_60"].median()
        new_high_advantage = snap["new_high20"].sum() > snap["new_low20"].sum()
        score = 0
        score += 20 if pos20 >= 0.55 else 10 if pos20 >= 0.45 else 3
        score += 25 if above20 >= 0.55 else 15 if above20 >= 0.45 else 5
        score += 15 if new_high_advantage else 5
        score += 20 if ret20_med > 0 else 8
        score += 20 if amount_med > 1.05 else 10 if amount_med > 0.95 else 3
        scores[pd.Timestamp(date)] = float(score)
    return scores


def environment_bucket(score: float) -> str:
    if pd.isna(score):
        return "未知"
    if score >= 70:
        return "进攻"
    if score >= 55:
        return "中性偏强"
    if score >= 45:
        return "偏观察"
    if score >= 30:
        return "弱观察"
    return "防守"


def validation_stage(row: pd.Series) -> str:
    ret5 = safe_float(row.get("ret5"))
    ret20 = safe_float(row.get("ret20"))
    ret60 = safe_float(row.get("ret60"))
    excess20 = safe_float(row.get("excess20"))
    above20 = safe_float(row.get("above20"))
    drawdown20 = safe_float(row.get("drawdown20"))
    drawdown60 = safe_float(row.get("drawdown60"))
    amount5_60 = safe_float(row.get("amount5_60"))

    if ret5 < -0.06 or drawdown20 < -0.10 or above20 < 0.20:
        return "退潮风险"
    if ret20 > 0.03 and excess20 > 0 and above20 >= 0.60 and drawdown60 > -0.10:
        return "确认延续"
    if ret5 > 0.03 and amount5_60 > 1.05 and above20 >= 0.40:
        return "加速升温"
    if ret20 > -0.03 and above20 >= 0.40 and drawdown60 > -0.15:
        return "结构修复"
    if ret5 > 0 and ret20 < 0 and ret60 < 0:
        return "弱势反弹"
    return "暂不观察"


def validation_grade(row: pd.Series) -> str:
    stage = row.get("stage")
    score = safe_float(row.get("stage_score"))
    ret20 = safe_float(row.get("ret20"))
    ret60 = safe_float(row.get("ret60"))
    above20 = safe_float(row.get("above20"))
    drawdown60 = safe_float(row.get("drawdown60"))

    if stage == "退潮风险":
        return "退潮主线"
    if stage == "确认延续" and score >= 82 and ret20 > 0 and ret60 > 0 and above20 >= 0.60:
        return "A级主线"
    if stage == "确认延续" and score >= 72 and ret20 > 0 and above20 >= 0.50:
        return "B级主线"
    if stage in ["加速升温", "结构修复"] and score >= 58:
        return "C级观察"
    if stage == "弱势反弹" and drawdown60 > -0.20 and score >= 55:
        return "C级观察"
    if stage == "结构修复" and drawdown60 > -0.12:
        return "企稳重估"
    return "暂不观察"


def build_validation_samples(lifecycle: pd.DataFrame, cooldown_days: int, random_seed: int) -> pd.DataFrame:
    focal = lifecycle.copy()
    focal["signal_type"] = focal.apply(early_signal_type, axis=1)
    focal = focal[focal["signal_type"] != ""].copy()
    early = dedupe_events(focal, "early_mainline", cooldown_days)
    groups = [early]
    groups.append(with_group(early[early["env_score"] >= 45], "early_env45"))
    groups.append(with_group(early[early["env_score"] >= 55], "early_env55"))
    core_types = ["企稳重估", "重新升温", "C级结构修复"]
    groups.append(with_group(early[early["signal_type"].isin(core_types)], "early_core_types"))
    groups.append(with_group(early[(early["signal_type"].isin(core_types)) & (early["env_score"] >= 45)], "early_core_env45"))

    controls = [
        ("daily_top10", lambda d: d.sort_values("daily_ret", ascending=False).head(10)),
        ("ret5_top10", lambda d: d.sort_values("ret5", ascending=False).head(10)),
        ("ret20_top10", lambda d: d.sort_values("ret20", ascending=False).head(10)),
        ("breadth_top10", lambda d: d.sort_values("above20", ascending=False).head(10)),
    ]
    for group, selector in controls:
        chosen = lifecycle.groupby("date", group_keys=False).apply(selector).copy()
        chosen["signal_type"] = group
        groups.append(dedupe_events(chosen, group, cooldown_days))

    rng = np.random.default_rng(random_seed)
    random_rows = []
    for _, frame in lifecycle.groupby("date"):
        if frame.empty:
            continue
        take = min(10, len(frame))
        random_rows.append(frame.iloc[rng.choice(len(frame), size=take, replace=False)].copy())
    random_control = pd.concat(random_rows, ignore_index=True) if random_rows else pd.DataFrame()
    random_control["signal_type"] = "random10"
    groups.append(dedupe_events(random_control, "random10", cooldown_days))

    events = pd.concat(groups, ignore_index=True)
    samples = attach_forward_metrics(events, lifecycle)
    return samples.sort_values(["group", "date", "industry"]).reset_index(drop=True)


def with_group(frame: pd.DataFrame, group: str) -> pd.DataFrame:
    out = frame.copy()
    out["group"] = group
    return out


def early_signal_type(row: pd.Series) -> str:
    grade = row.get("mainline_grade")
    stage = row.get("stage")
    prev_grade = row.get("prev_mainline_grade")
    improved = bool(row.get("improved"))
    ret5 = safe_float(row.get("ret5"))
    ret20 = safe_float(row.get("ret20"))
    above20 = safe_float(row.get("above20"))
    amount5_60 = safe_float(row.get("amount5_60"))
    drawdown60 = safe_float(row.get("drawdown60"))

    if grade in ["A级主线", "B级主线"] and improved and row.get("prev_grade_rank", 6) >= 2:
        return "晋级到A/B"
    if grade == "企稳重估":
        return "企稳重估"
    if prev_grade in ["退潮主线", "低频监控", "暂不观察"] and grade == "C级观察" and ret5 > 0:
        return "重新升温"
    if grade == "C级观察" and ret5 >= 0.03 and above20 >= 0.40 and amount5_60 >= 1.05:
        return "C级加速升温"
    if grade == "C级观察" and stage != "弱势反弹" and ret20 > -0.03 and drawdown60 > -0.15:
        return "C级结构修复"
    return ""


def dedupe_events(events: pd.DataFrame, group: str, cooldown_days: int) -> pd.DataFrame:
    if events.empty or "date" not in events.columns or "industry" not in events.columns:
        out = events.copy() if not events.empty else events
        out["group"] = group
        return out
    rows = []
    last_seen: dict[str, pd.Timestamp] = {}
    for _, row in events.sort_values(["date", "industry"]).iterrows():
        industry = row["industry"]
        date = row["date"]
        if industry in last_seen and (date - last_seen[industry]).days < cooldown_days:
            continue
        last_seen[industry] = date
        rows.append(row)
    frame = pd.DataFrame(rows)
    frame["group"] = group
    return frame


def attach_forward_metrics(events: pd.DataFrame, lifecycle: pd.DataFrame) -> pd.DataFrame:
    by_industry = {industry: frame.sort_values("date").reset_index(drop=True) for industry, frame in lifecycle.groupby("industry")}
    rows = []
    for _, event in events.iterrows():
        frame = by_industry.get(event["industry"])
        if frame is None:
            continue
        positions = frame.index[frame["date"] == event["date"]].tolist()
        if not positions:
            continue
        pos = positions[0]
        row = event.to_dict()
        current_index = safe_float(frame.loc[pos, "ind_index"])
        current_market = safe_float(frame.loc[pos, "mkt_cum"])
        for horizon in HORIZONS:
            if pos + horizon >= len(frame):
                row[f"ret_{horizon}d"] = np.nan
                row[f"mkt_ret_{horizon}d"] = np.nan
                row[f"excess_{horizon}d"] = np.nan
                row[f"max_drawdown_{horizon}d"] = np.nan
                row[f"peak_ret_{horizon}d"] = np.nan
                row[f"days_to_peak_{horizon}d"] = np.nan
                continue
            future = frame.loc[pos + horizon]
            window = frame.loc[pos + 1 : pos + horizon].copy()
            path_ret = window["ind_index"] / current_index - 1
            row[f"ret_{horizon}d"] = safe_float(future["ind_index"]) / current_index - 1
            row[f"mkt_ret_{horizon}d"] = safe_float(future["mkt_cum"]) / current_market - 1
            row[f"excess_{horizon}d"] = row[f"ret_{horizon}d"] - row[f"mkt_ret_{horizon}d"]
            row[f"max_drawdown_{horizon}d"] = float(path_ret.min()) if not path_ret.empty else np.nan
            row[f"peak_ret_{horizon}d"] = float(path_ret.max()) if not path_ret.empty else np.nan
            row[f"days_to_peak_{horizon}d"] = int(path_ret.idxmax() - pos) if not path_ret.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_samples(samples: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for group, frame in samples.groupby("group"):
        row = {"group": group, "samples": len(frame)}
        for horizon in HORIZONS:
            col = f"excess_{horizon}d"
            ret_col = f"ret_{horizon}d"
            dd_col = f"max_drawdown_{horizon}d"
            peak_col = f"peak_ret_{horizon}d"
            valid = frame.dropna(subset=[col])
            row[f"win_rate_{horizon}d"] = (valid[col] > 0).mean() if not valid.empty else np.nan
            row[f"avg_ret_{horizon}d"] = valid[ret_col].mean() if not valid.empty else np.nan
            row[f"avg_excess_{horizon}d"] = valid[col].mean() if not valid.empty else np.nan
            row[f"median_excess_{horizon}d"] = valid[col].median() if not valid.empty else np.nan
            row[f"avg_max_drawdown_{horizon}d"] = valid[dd_col].mean() if not valid.empty else np.nan
            row[f"avg_peak_ret_{horizon}d"] = valid[peak_col].mean() if not valid.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows).sort_values("group")


def summarize_by_year(samples: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (year, group), frame in samples.groupby(["year", "group"]):
        valid = frame.dropna(subset=["excess_20d"])
        rows.append(
            {
                "year": int(year),
                "group": group,
                "samples": len(frame),
                "win_rate_20d": (valid["excess_20d"] > 0).mean() if not valid.empty else np.nan,
                "avg_excess_20d": valid["excess_20d"].mean() if not valid.empty else np.nan,
                "avg_excess_40d": frame["excess_40d"].mean(),
                "avg_max_drawdown_40d": frame["max_drawdown_40d"].mean(),
            }
        )
    return pd.DataFrame(rows).sort_values(["year", "group"])


def summarize_signal_types(samples: pd.DataFrame) -> pd.DataFrame:
    focal = samples[samples["group"] == "early_mainline"].copy()
    rows = []
    for signal_type, frame in focal.groupby("signal_type"):
        valid20 = frame.dropna(subset=["excess_20d"])
        valid40 = frame.dropna(subset=["excess_40d"])
        rows.append(
            {
                "signal_type": signal_type,
                "samples": len(frame),
                "win_rate_20d": (valid20["excess_20d"] > 0).mean() if not valid20.empty else np.nan,
                "avg_excess_20d": valid20["excess_20d"].mean() if not valid20.empty else np.nan,
                "win_rate_40d": (valid40["excess_40d"] > 0).mean() if not valid40.empty else np.nan,
                "avg_excess_40d": valid40["excess_40d"].mean() if not valid40.empty else np.nan,
                "avg_peak_ret_40d": valid40["peak_ret_40d"].mean() if not valid40.empty else np.nan,
                "avg_days_to_peak_40d": valid40["days_to_peak_40d"].mean() if not valid40.empty else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values("avg_excess_40d", ascending=False)


def render_report(
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    year_summary: pd.DataFrame,
    signal_summary: pd.DataFrame,
    start: str,
    end: str,
) -> str:
    focal = samples[samples["group"] == "early_mainline"]
    best = signal_summary.head(3)
    return f"""# 早期主线识别历史验证报告

区间：{start} 到 {end}

本报告只验证行业主线识别，不验证个股买点、仓位、止损或交易执行。参与对象按行业等权 beta 近似，用行业内股票中位收益合成行业强弱。

## 一句话结论

{one_line_conclusion(summary, signal_summary)}

## 验证问题

- 系统能否在 A/B 主线完全明牌前，识别未来仍有延续性的行业？
- 早期主线信号相对普通涨幅榜、宽度榜和随机行业是否有优势？
- 哪些早期生命周期状态更值得在日报中重点观察？

## 总体对照

{markdown_table(format_summary(summary))}

## 早期信号类型拆解

{markdown_table(format_signal_summary(signal_summary))}

## 年度表现

{markdown_table(format_year_summary(year_summary))}

## 最值得复核的早期信号

{markdown_table(format_top_events(focal))}

## 读法

- `win_rate_20d/40d`：未来 20/40 个交易日跑赢全市场行业中位基准的概率。
- `avg_excess_20d/40d`：未来 20/40 个交易日相对市场的平均超额收益。
- `avg_peak_ret_40d`：信号后 40 日内行业 beta 的平均最高涨幅，用来判断是否还有可参与空间。
- `avg_days_to_peak_40d`：从信号出现到 40 日窗口内阶段峰值的平均天数，越大越说明识别不算太晚。
- `avg_max_drawdown_40d`：信号后 40 日内平均最大回撤，用来判断早期信号是否太躁。

## 初步观察

{bullet_findings(summary, signal_summary, best)}

## 局限

- 行业 beta 使用本地行业分类和行业中位收益近似，不等同于真实行业 ETF。
- 这不是交易回测，没有考虑买卖点、滑点、仓位和换手成本。
- 2026 年只到本地缓存末日，长周期样本会自然减少。
- 当前验证更关注“方向识别是否有效”，下一步才适合研究 ETF/中军/龙头载体如何表达主线。

## 输出文件

- `early_mainline_samples.csv`
- `early_mainline_summary.csv`
- `early_mainline_year_summary.csv`
- `early_mainline_signal_type_summary.csv`
"""


def format_summary(summary: pd.DataFrame) -> pd.DataFrame:
    cols = ["group", "samples"]
    for horizon in [20, 40, 60]:
        cols.extend([f"win_rate_{horizon}d", f"avg_excess_{horizon}d", f"avg_peak_ret_{horizon}d", f"avg_max_drawdown_{horizon}d"])
    return display_frame(summary[cols])


def format_signal_summary(signal_summary: pd.DataFrame) -> pd.DataFrame:
    cols = ["signal_type", "samples", "win_rate_20d", "avg_excess_20d", "win_rate_40d", "avg_excess_40d", "avg_peak_ret_40d", "avg_days_to_peak_40d"]
    return display_frame(signal_summary[cols])


def format_year_summary(year_summary: pd.DataFrame) -> pd.DataFrame:
    focal = year_summary[year_summary["group"].isin(["early_mainline", "ret20_top10", "daily_top10", "random10"])]
    cols = ["year", "group", "samples", "win_rate_20d", "avg_excess_20d", "avg_excess_40d", "avg_max_drawdown_40d"]
    return display_frame(focal[cols])


def format_top_events(focal: pd.DataFrame) -> pd.DataFrame:
    cols = ["date", "industry", "signal_type", "mainline_grade", "stage", "ret5", "ret20", "above20", "excess_20d", "excess_40d", "peak_ret_40d", "days_to_peak_40d"]
    return display_frame(focal.sort_values("excess_40d", ascending=False).head(20)[cols])


def display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in out.columns:
        if col == "date":
            out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%d")
        elif any(key in col for key in ["rate", "ret", "excess", "drawdown", "above"]):
            out[col] = out[col].map(lambda x: pct(x) if pd.notna(x) else "NA")
        elif col.startswith("avg_days"):
            out[col] = out[col].map(lambda x: f"{x:.1f}" if pd.notna(x) else "NA")
    return out


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "暂无数据"
    rows = ["| " + " | ".join(frame.columns.astype(str)) + " |"]
    rows.append("| " + " | ".join(["---"] * len(frame.columns)) + " |")
    for _, row in frame.iterrows():
        rows.append("| " + " | ".join(str(row[col]) for col in frame.columns) + " |")
    return "\n".join(rows)


def one_line_conclusion(summary: pd.DataFrame, signal_summary: pd.DataFrame) -> str:
    focal = summary[summary["group"] == "early_mainline"]
    if focal.empty:
        return "样本不足，暂无法判断早期主线识别效果。"
    row = focal.iloc[0]
    win40 = row.get("win_rate_40d")
    excess40 = row.get("avg_excess_40d")
    if pd.notna(win40) and pd.notna(excess40) and win40 >= 0.55 and excess40 > 0:
        return "早期主线信号具备继续研究价值：40 日维度相对市场有正超额，且胜率不低。"
    if pd.notna(excess40) and excess40 > 0:
        return "早期主线信号有一定正反馈，但胜率或年度稳定性仍需拆分检查。"
    return "当前早期主线信号整体优势不明显，需要进一步收紧早期定义或增加市场环境过滤。"


def bullet_findings(summary: pd.DataFrame, signal_summary: pd.DataFrame, best: pd.DataFrame) -> str:
    lines = []
    focal = summary[summary["group"] == "early_mainline"]
    if not focal.empty:
        row = focal.iloc[0]
        lines.append(f"- 早期主线样本数 {int(row['samples'])}，20 日平均超额 {pct(row.get('avg_excess_20d'))}，40 日平均超额 {pct(row.get('avg_excess_40d'))}。")
    if not best.empty:
        names = "、".join(best["signal_type"].astype(str).tolist())
        lines.append(f"- 当前排序靠前的早期状态是：{names}。这些状态应在日报中优先复核。")
    controls = summary[~summary["group"].str.startswith("early")].sort_values("avg_excess_40d", ascending=False).head(1)
    if not controls.empty:
        row = controls.iloc[0]
        lines.append(f"- 最强非主线对照组为 {row['group']}，40 日平均超额 {pct(row.get('avg_excess_40d'))}；收窄后的早期主线规则需要持续和它比较。")
    return "\n".join(lines) if lines else "- 暂无足够结论。"


def pct(value) -> str:
    if value is None or pd.isna(value):
        return "NA"
    return f"{float(value) * 100:.2f}%"


def safe_float(value) -> float:
    if value is None or pd.isna(value):
        return np.nan
    return float(value)


if __name__ == "__main__":
    main()
