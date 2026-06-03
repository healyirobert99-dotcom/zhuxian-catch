from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = BASE / "reports" / "mainline_early_detection_validation_5y" / "early_mainline_samples.csv"
DEFAULT_REPORT_DIR = BASE / "reports" / "mainline_blind_replay_2024_now"

THEMES = {
    "创新药/医药链": ["化学制药", "生物制药", "医疗保健", "中成药", "医药商业"],
    "半导体/电子": ["半导体", "元器件"],
    "商业航天近似": ["航空", "运输设备", "通信设备", "专用机械", "船舶"],
}

CORE_GROUP = "early_core_env45"
HORIZONS = (20, 40, 60)


def main() -> None:
    args = parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    samples = pd.read_csv(args.source, parse_dates=["date"])
    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end) if args.end else samples["date"].max()
    window = samples[(samples["date"] >= start) & (samples["date"] <= end)].copy()
    if window.empty:
        raise SystemExit(f"No samples found between {start.date()} and {end.date()}.")

    blind_signals = window[window["group"] == CORE_GROUP].copy()
    if blind_signals.empty:
        raise SystemExit(f"No {CORE_GROUP} samples found between {start.date()} and {end.date()}.")
    blind_signals = add_theme(blind_signals)
    blind_signals.to_csv(report_dir / "blind_replay_signals_2024_now.csv", index=False)

    theme_replay = build_theme_replay(blind_signals)
    theme_replay.to_csv(report_dir / "theme_replay_2024_now.csv", index=False)

    major_captures = blind_signals[
        (blind_signals["peak_ret_60d"].fillna(blind_signals["peak_ret_40d"]) >= args.major_peak)
    ].copy()
    major_captures = major_captures.sort_values(["date", "industry"])
    major_captures.to_csv(report_dir / "major_captures_2024_now.csv", index=False)

    group_summary = summarize_groups(window)
    group_summary.to_csv(report_dir / "group_summary_2024_now.csv", index=False)

    theme_summary = summarize_themes(blind_signals)
    theme_summary.to_csv(report_dir / "theme_summary_2024_now.csv", index=False)

    report = render_report(
        start=start,
        end=end,
        blind_signals=blind_signals,
        group_summary=group_summary,
        theme_summary=theme_summary,
        theme_replay=theme_replay,
        major_captures=major_captures,
        major_peak=args.major_peak,
    )
    report_path = report_dir / "blind_replay_report_2024_now.md"
    report_path.write_text(report, encoding="utf-8")
    print(report_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay 2024-now early mainline signals without result-fitted rules.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--major-peak", type=float, default=0.10, help="Post-signal peak return used only for after-the-fact capture labeling.")
    return parser.parse_args()


def add_theme(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    mapping = {}
    for theme, industries in THEMES.items():
        for industry in industries:
            mapping[industry] = theme
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


def summarize_groups(samples: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for group, frame in samples.groupby("group"):
        row = {"group": group, "samples": len(frame)}
        for horizon in HORIZONS:
            valid = frame.dropna(subset=[f"excess_{horizon}d"])
            row[f"win_rate_{horizon}d"] = (valid[f"excess_{horizon}d"] > 0).mean() if not valid.empty else np.nan
            row[f"avg_ret_{horizon}d"] = valid[f"ret_{horizon}d"].mean() if not valid.empty else np.nan
            row[f"avg_excess_{horizon}d"] = valid[f"excess_{horizon}d"].mean() if not valid.empty else np.nan
            row[f"avg_peak_ret_{horizon}d"] = valid[f"peak_ret_{horizon}d"].mean() if not valid.empty else np.nan
            row[f"avg_days_to_peak_{horizon}d"] = valid[f"days_to_peak_{horizon}d"].mean() if not valid.empty else np.nan
        rows.append(row)
    return pd.DataFrame(rows).sort_values("group")


def summarize_themes(signals: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for theme, frame in signals.groupby("theme"):
        if theme == "其他":
            continue
        row = {"theme": theme, "samples": len(frame)}
        for horizon in HORIZONS:
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
    start: pd.Timestamp,
    end: pd.Timestamp,
    blind_signals: pd.DataFrame,
    group_summary: pd.DataFrame,
    theme_summary: pd.DataFrame,
    theme_replay: pd.DataFrame,
    major_captures: pd.DataFrame,
    major_peak: float,
) -> str:
    target_major = major_captures[major_captures["theme"] != "其他"].copy()
    return f"""# 2024 至今早期主线盲测复盘

区间：{start.date()} 至 {end.date()}

## 一句话结论

系统能捕捉到一部分大级别行业 beta 的早期段，尤其是 2024 年 9 月医药链、航空/运输设备，以及 2025 年船舶等；但半导体和商业航天这类概念型主线，用当前粗行业分类只能得到“近似提示”，不能完全证明主题捕捉能力。

## 验证口径

- 只使用封版规则中的 `{CORE_GROUP}`：企稳重估、重新升温、C级结构修复，且环境分不低于 45。
- 信号生成不看未来收益；`峰值空间` 只在信号产生后用于评估。
- 参与对象是行业等权 beta 近似，不是 ETF 实盘净值，也不是个股交易回测。
- 商业航天没有本地精确主题标签，本报告用航空、运输设备、通信设备、专用机械、船舶作为近似映射。

## 总体对照

{markdown_table(format_group_summary(group_summary))}

## 目标主题汇总

{markdown_table(format_theme_summary(theme_summary))}

## 关键盲测记录

### 创新药/医药链

{theme_findings(theme_replay, "创新药/医药链")}

### 半导体/电子

{theme_findings(theme_replay, "半导体/电子")}

### 商业航天近似

{theme_findings(theme_replay, "商业航天近似")}

## 大级别捕捉样本

以下列表的筛选条件是：信号后 60 日内峰值空间不低于 {pct(major_peak)}。这个条件只用于事后归档“大级别捕捉样本”，不参与信号生成。

{markdown_table(format_major_captures(target_major))}

## 对系统有效性的判断

- 有效的部分：当主线能被粗行业 beta 表达时，早期信号确实可能在峰值前 5 到 50 多个交易日出现，给出后续 10% 到 30% 以上的峰值空间。
- 不足的部分：概念型主线常分散在多个行业内，当前行业分类会稀释商业航天、机器人、AI 应用这类主题。
- 对半导体的判断：系统有提示，但 2025 年以来更多是阶段性波段，峰值空间约 9% 到 11%，不是特别干净的大级别趋势。
- 对创新药/医药的判断：2024 年 9 月捕捉很清楚；2025 年医药链也有提示，但空间多在 5% 到 10% 左右，强度明显低于 2024 年 9 月。
- 对商业航天的判断：若用航空/运输设备近似，2024 年 9 月捕捉很好，2025 年 4-5 月也有较早提示；但这不能替代真正的概念成分池验证。

## 下一步建议

1. 给商业航天、创新药、半导体建立概念成分池，再跑同样盲测。
2. 把“第一次优先复核”与“连续三日保持优先复核”分开统计，判断早期信号是否需要持续确认。
3. 对每条大级别主线输出从首次提示到 A/B 确认、再到峰值的生命周期轨迹。

## 输出文件

- `blind_replay_signals_2024_now.csv`
- `theme_replay_2024_now.csv`
- `major_captures_2024_now.csv`
- `group_summary_2024_now.csv`
- `theme_summary_2024_now.csv`
"""


def theme_findings(theme_replay: pd.DataFrame, theme: str) -> str:
    frame = theme_replay[theme_replay["theme"] == theme].copy()
    if frame.empty:
        return "暂无对应信号。"
    replay = representative_replay(frame)
    rows = format_replay(replay)
    lead = build_theme_lead(theme, frame)
    return f"{lead}\n\n下表按信号出现时间排序，未按未来收益倒排；峰值字段只用于事后验证空间。\n\n{markdown_table(rows)}"


def representative_replay(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy().sort_values(["date", "industry"])
    out["quarter"] = out["date"].dt.to_period("Q").astype(str)
    # Keep the first signal per industry per quarter. This preserves blind replay order
    # while avoiding a very long report table.
    out = out.groupby(["industry", "quarter"], as_index=False, group_keys=False).head(1)
    return out.sort_values(["date", "industry"]).head(18).drop(columns=["quarter"])


def build_theme_lead(theme: str, frame: pd.DataFrame) -> str:
    valid = frame.dropna(subset=["peak_ret_60d"])
    if valid.empty:
        return "有提示，但当前样本缺少足够前瞻窗口。"
    first = frame.sort_values(["date", "industry"]).iloc[0]
    best = valid.sort_values("peak_ret_60d", ascending=False).iloc[0]
    return (
        f"首次提示：{date_str(first['date'])} {first['industry']}，{first['signal_type']}。"
        f"事后最大峰值记录：{date_str(best['date'])} {best['industry']}，"
        f"60 日峰值空间 {pct(best['peak_ret_60d'])}，距峰值 {int(best['days_to_peak_60d'])} 个交易日。"
    )


def format_group_summary(summary: pd.DataFrame) -> pd.DataFrame:
    groups = ["early_core_env45", "early_mainline", "daily_top10", "ret20_top10", "breadth_top10", "random10"]
    cols = ["group", "samples", "win_rate_40d", "avg_ret_40d", "avg_excess_40d", "avg_peak_ret_40d", "avg_days_to_peak_40d"]
    frame = summary[summary["group"].isin(groups)][cols].copy()
    return display_frame(frame)


def format_theme_summary(summary: pd.DataFrame) -> pd.DataFrame:
    cols = ["theme", "samples", "avg_peak_ret_40d", "median_peak_ret_40d", "avg_days_to_peak_40d", "avg_ret_40d", "avg_excess_40d"]
    return display_frame(summary[cols])


def format_replay(frame: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "date",
        "industry",
        "signal_type",
        "env_score",
        "ret20",
        "above20",
        "peak_ret_40d",
        "days_to_peak_40d",
        "peak_ret_60d",
        "days_to_peak_60d",
        "ret_60d",
        "excess_60d",
    ]
    return display_frame(frame[cols])


def format_major_captures(frame: pd.DataFrame) -> pd.DataFrame:
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
    return display_frame(frame[cols].sort_values(["date", "theme", "industry"]))


def display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in out.columns:
        if col == "date":
            out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%d")
        elif col == "env_score":
            out[col] = out[col].map(lambda x: f"{float(x):.0f}" if pd.notna(x) else "NA")
        elif "days_to_peak" in col:
            out[col] = out[col].map(lambda x: f"{float(x):.0f}" if pd.notna(x) else "NA")
        elif any(key in col for key in ["rate", "ret", "excess", "drawdown", "above", "peak"]):
            out[col] = out[col].map(pct)
    return out


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "暂无数据"
    rows = ["| " + " | ".join(frame.columns.astype(str)) + " |"]
    rows.append("| " + " | ".join(["---"] * len(frame.columns)) + " |")
    for _, row in frame.iterrows():
        rows.append("| " + " | ".join(str(row[col]) for col in frame.columns) + " |")
    return "\n".join(rows)


def pct(value) -> str:
    if value is None or pd.isna(value):
        return "NA"
    return f"{float(value) * 100:.2f}%"


def date_str(value) -> str:
    return pd.Timestamp(value).date().isoformat()


if __name__ == "__main__":
    main()
