from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

import analyze_2024_mainline_blind_replay as industry_replay  # noqa: E402
import generate_daily_review as daily_review  # noqa: E402
import validate_mainline_early_detection as validation  # noqa: E402


DEFAULT_REPORT_DIR = BASE / "reports" / "concept_blind_replay_2024_now"
CORE_GROUP = "early_core_env45"

THEMES = {
    "商业航天": ["商业航天"],
    "低空经济": ["低空经济"],
    "创新药": ["创新药", "AI制药（医疗）"],
    "半导体": ["半导体概念", "第三代半导体", "第四代半导体", "AI芯片"],
    "AI主题": ["AIGC概念", "AI应用", "AI智能体", "AI芯片", "多模态AI", "AI语料"],
}


def main() -> None:
    args = parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    requested_start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    concepts = daily_review.load_concept_data(end.date().isoformat())
    if concepts.empty:
        raise SystemExit("No concept_daily data found. Sync concept data first.")
    actual_start = max(requested_start, concepts["date"].min())
    metrics = daily_review.compute_concept_lifecycle_metrics(concepts)
    lifecycle = validation.build_daily_lifecycle_frame(metrics)
    lifecycle = lifecycle[(lifecycle["date"] >= actual_start) & (lifecycle["date"] <= end)].copy()
    if lifecycle.empty:
        raise SystemExit("Concept lifecycle is empty after filtering. Need longer concept history.")

    samples = validation.build_validation_samples(lifecycle, args.cooldown_days, args.random_seed)
    samples = samples[(samples["date"] >= actual_start) & (samples["date"] <= end)].copy()
    samples = add_theme(samples)
    samples.to_csv(report_dir / "concept_validation_samples.csv", index=False)

    summary = validation.summarize_samples(samples)
    summary.to_csv(report_dir / "concept_group_summary.csv", index=False)

    core = samples[samples["group"] == CORE_GROUP].copy()
    core.to_csv(report_dir / "concept_blind_replay_signals.csv", index=False)

    theme_replay = build_theme_replay(core)
    theme_replay.to_csv(report_dir / "concept_theme_replay.csv", index=False)

    theme_summary = summarize_themes(core)
    theme_summary.to_csv(report_dir / "concept_theme_summary.csv", index=False)

    major = core[core["peak_ret_60d"].fillna(core["peak_ret_40d"]) >= args.major_peak].copy()
    major = major.sort_values(["date", "industry"])
    major.to_csv(report_dir / "concept_major_captures.csv", index=False)

    report = render_report(
        requested_start=requested_start,
        actual_start=actual_start,
        end=end,
        samples=samples,
        core=core,
        summary=summary,
        theme_summary=theme_summary,
        theme_replay=theme_replay,
        major=major,
        major_peak=args.major_peak,
    )
    out = report_dir / "concept_blind_replay_report_2024_now.md"
    out.write_text(report, encoding="utf-8")
    print(out)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay early mainline detection on Eastmoney concept boards.")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-06-01")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--cooldown-days", type=int, default=20)
    parser.add_argument("--random-seed", type=int, default=2026)
    parser.add_argument("--major-peak", type=float, default=0.10)
    return parser.parse_args()


def add_theme(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    mapping = {}
    for theme, concepts in THEMES.items():
        for concept in concepts:
            mapping[concept] = theme
    out["theme"] = out["industry"].map(mapping).fillna("其他")
    return out


def build_theme_replay(signals: pd.DataFrame) -> pd.DataFrame:
    target = signals[signals["theme"] != "其他"].copy()
    cols = [
        "date",
        "theme",
        "industry",
        "signal_type",
        "mainline_grade",
        "stage",
        "env_score",
        "ret20",
        "above20",
        "drawdown60",
        "peak_ret_40d",
        "days_to_peak_40d",
        "peak_ret_60d",
        "days_to_peak_60d",
        "ret_60d",
        "excess_60d",
    ]
    return target[cols].sort_values(["theme", "date", "industry"])


def summarize_themes(signals: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for theme, frame in signals.groupby("theme"):
        if theme == "其他":
            continue
        row = {"theme": theme, "samples": len(frame)}
        for horizon in [20, 40, 60]:
            valid = frame.dropna(subset=[f"peak_ret_{horizon}d"])
            row[f"avg_peak_ret_{horizon}d"] = valid[f"peak_ret_{horizon}d"].mean() if not valid.empty else np.nan
            row[f"median_peak_ret_{horizon}d"] = valid[f"peak_ret_{horizon}d"].median() if not valid.empty else np.nan
            row[f"avg_days_to_peak_{horizon}d"] = valid[f"days_to_peak_{horizon}d"].mean() if not valid.empty else np.nan
            row[f"avg_ret_{horizon}d"] = valid[f"ret_{horizon}d"].mean() if not valid.empty else np.nan
            row[f"avg_excess_{horizon}d"] = valid[f"excess_{horizon}d"].mean() if not valid.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows).sort_values("theme")


def render_report(
    *,
    requested_start: pd.Timestamp,
    actual_start: pd.Timestamp,
    end: pd.Timestamp,
    samples: pd.DataFrame,
    core: pd.DataFrame,
    summary: pd.DataFrame,
    theme_summary: pd.DataFrame,
    theme_replay: pd.DataFrame,
    major: pd.DataFrame,
    major_peak: float,
) -> str:
    return f"""# 概念板块早期主线盲测复盘

请求区间：{requested_start.date()} 至 {end.date()}

实际可用区间：{actual_start.date()} 至 {end.date()}

## 一句话结论

接入概念板块后，对商业航天、低空经济、创新药、半导体、AI芯片这类主题的识别明显比粗行业近似更直接；但东方财富概念板块本地可用历史从 2024-12-20 开始，经过 60 日生命周期计算后，真正可验证的早期信号主要集中在 2025 年以后。

## 验证口径

- 规则仍然是封版早期主线：`企稳重估 / 重新升温 / C级结构修复 + 环境分 >=45`。
- 对象从传统行业改为东方财富概念板块。
- 信号生成不看未来；峰值空间只在信号出现后用于评估。
- 概念板块宽度使用东方财富涨跌家数比例近似。

## 总体对照

{industry_replay.markdown_table(format_group_summary(summary))}

## 目标概念汇总

{industry_replay.markdown_table(industry_replay.format_theme_summary(theme_summary))}

## 关键概念盲测记录

### 商业航天

{theme_findings(theme_replay, "商业航天")}

### 低空经济

{theme_findings(theme_replay, "低空经济")}

### 创新药

{theme_findings(theme_replay, "创新药")}

### 半导体

{theme_findings(theme_replay, "半导体")}

### AI主题

{theme_findings(theme_replay, "AI主题")}

## 大级别捕捉样本

以下列表的筛选条件是：信号后 60 日内峰值空间不低于 {industry_replay.pct(major_peak)}。这个条件只用于事后归档，不参与信号生成。

{industry_replay.markdown_table(format_major(major[major["theme"] != "其他"]))}

## 和粗行业近似相比

- 商业航天、低空经济、创新药、AI芯片可以被直接观察，不再需要用航空、运输设备、通信设备等粗行业近似。
- 概念维度更适合捕捉主题行情，但也更容易出现短周期轮动和概念过热，因此要继续坚持“优先复核”，不把它当成行动信号。
- 若概念主线和传统行业主线同时出现，例如半导体概念与半导体行业同步修复，才是更强的共振线索。

## 输出文件

- `concept_validation_samples.csv`
- `concept_blind_replay_signals.csv`
- `concept_theme_replay.csv`
- `concept_theme_summary.csv`
- `concept_major_captures.csv`
"""


def theme_findings(theme_replay: pd.DataFrame, theme: str) -> str:
    frame = theme_replay[theme_replay["theme"] == theme].copy()
    if frame.empty:
        return "暂无对应早期核心信号。"
    replay = industry_replay.representative_replay(frame)
    lead = industry_replay.build_theme_lead(theme, frame)
    return f"{lead}\n\n下表按信号出现时间排序，未按未来收益倒排。\n\n{industry_replay.markdown_table(industry_replay.format_replay(replay))}"


def format_group_summary(summary: pd.DataFrame) -> pd.DataFrame:
    groups = ["early_core_env45", "early_mainline", "daily_top10", "ret20_top10", "breadth_top10", "random10"]
    cols = ["group", "samples", "win_rate_40d", "avg_ret_40d", "avg_excess_40d", "avg_peak_ret_40d", "avg_max_drawdown_40d"]
    return industry_replay.display_frame(summary[summary["group"].isin(groups)][cols].copy())


def format_major(frame: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "date",
        "theme",
        "industry",
        "signal_type",
        "env_score",
        "peak_ret_40d",
        "days_to_peak_40d",
        "peak_ret_60d",
        "days_to_peak_60d",
        "ret_60d",
        "excess_60d",
    ]
    if frame.empty:
        return pd.DataFrame(columns=cols)
    return industry_replay.display_frame(frame[cols].sort_values(["date", "theme", "industry"]))


if __name__ == "__main__":
    main()
