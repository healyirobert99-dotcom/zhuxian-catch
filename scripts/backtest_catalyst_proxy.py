"""
催化剂层代理回测验证

由于 catalyst_titles.csv 无历史数据，且 concept_daily(THS) 只有 pct_change 有值，
改用**概念涨跌幅绝对值**作为市场关注度代理：

催化代理定义：信号日当天，关联概念的 |涨跌幅| >= N%（默认 3%）。
逻辑：概念大幅波动 = 市场对该方向有实质性关注（媒体催化 → 关注 → 价格波动）。
"""

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.stats as stats

BASE = Path(__file__).resolve().parents[1]
DB_PATH = BASE / "data" / "a_stock_selector.sqlite3"
SAMPLES_PATH = (
    BASE / "reports" / "mainline_early_detection_validation_5y" / "early_mainline_samples.csv"
)
OUT_DIR = BASE / "reports" / "catalyst_proxy_backtest"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── 数据加载 ────────────────────────────────────────────

def load_samples() -> pd.DataFrame:
    samples = pd.read_csv(SAMPLES_PATH, parse_dates=["date"])
    return samples[samples["group"] == "early_core_env45"].copy()


def load_concept_name_to_code() -> dict[str, str]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT ts_code, name FROM concept_basic").fetchall()
    return {row[1]: row[0] for row in rows}


def load_industry_concept_map() -> dict[str, list[str]]:
    with sqlite3.connect(DB_PATH) as con:
        industries = [
            r[0] for r in con.execute(
                "SELECT DISTINCT industry FROM stock_basic WHERE industry IS NOT NULL AND industry != ''"
            ).fetchall()
        ]
        concept_names = [
            r[0] for r in con.execute(
                "SELECT DISTINCT name FROM concept_basic"
            ).fetchall()
        ]
    mapping: dict[str, list[str]] = {}
    for ind in industries:
        short = ind.replace("行业", "").replace("板块", "")
        matches = []
        for cn in concept_names:
            if len(short) >= 3 and short in cn:
                matches.append(cn)
            elif ind == cn:
                matches.append(cn)
            elif cn.startswith(short) or cn.endswith(short):
                if len(short) >= 3:
                    matches.append(cn)
        mapping[ind] = list(dict.fromkeys(matches))
    return mapping


def load_concept_pct_data(ts_codes: list[str]) -> pd.DataFrame:
    """加载概念 pct_change 历史。"""
    if not ts_codes:
        return pd.DataFrame()
    placeholders = ",".join(["?"] * len(ts_codes))
    query = f"""
        SELECT cd.trade_date AS date, cd.ts_code, cd.pct_change
        FROM concept_daily cd
        WHERE cd.ts_code IN ({placeholders})
          AND cd.pct_change IS NOT NULL AND cd.pct_change != ''
        ORDER BY cd.ts_code, cd.trade_date
    """
    with sqlite3.connect(DB_PATH) as con:
        frame = pd.read_sql_query(query, con, params=ts_codes)
    frame["date"] = pd.to_datetime(frame["date"])
    frame["pct_change"] = pd.to_numeric(frame["pct_change"], errors="coerce")
    frame["abs_pct"] = frame["pct_change"].abs()
    return frame


# ── 催化代理标记 ────────────────────────────────────────

def enrich_signals_with_catalyst_proxy(
    signals: pd.DataFrame,
    concept_pct_data: pd.DataFrame,
    industry_to_concept_names: dict[str, list[str]],
    concept_name_to_code: dict[str, str],
    abs_pct_threshold: float = 3.0,
) -> pd.DataFrame:
    """标记每条信号是否有概念层面的大幅波动（催化代理）。"""
    result = signals.copy()
    result["catalyst_proxy"] = False
    result["surge_concepts"] = ""
    result["max_abs_pct"] = np.nan

    if concept_pct_data.empty:
        return result

    # 按日期建索引
    pct_by_date = concept_pct_data.groupby("date")

    # 预计算每个行业的 ts_code 列表
    industry_to_codes: dict[str, list[str]] = {}
    for ind, cns in industry_to_concept_names.items():
        codes = [concept_name_to_code.get(cn) for cn in cns]
        codes = [c for c in codes if c is not None]
        industry_to_codes[ind] = codes

    for idx, row in result.iterrows():
        sig_date = row["date"]
        codes = industry_to_codes.get(row["industry"], [])
        if not codes or sig_date not in pct_by_date.groups:
            continue

        day_pct = pct_by_date.get_group(sig_date)
        matched = day_pct[day_pct["ts_code"].isin(codes)]
        surged = matched[matched["abs_pct"] >= abs_pct_threshold]

        if not surged.empty:
            result.at[idx, "catalyst_proxy"] = True
            result.at[idx, "surge_concepts"] = "、".join(
                surged["ts_code"].head(5).tolist()
            )
            result.at[idx, "max_abs_pct"] = surged["abs_pct"].max()

    return result


# ── 回测对比 ────────────────────────────────────────────

def run_backtest(
    signals: pd.DataFrame,
    thresholds: list[float] = [2.0, 3.0, 4.0, 5.0],
    horizons: list[int] = [20, 40, 60],
) -> pd.DataFrame:
    rows = []
    for thresh in thresholds:
        col = f"catalyst_abs{thresh:.0f}"
        if col not in signals.columns:
            continue
        for horizon in horizons:
            excess_col = f"excess_{horizon}d"
            peak_col = f"peak_ret_{horizon}d"
            dd_col = f"max_drawdown_{horizon}d"
            days_col = f"days_to_peak_{horizon}d"

            if excess_col not in signals.columns:
                continue

            with_c = signals[(signals[col]) & signals[excess_col].notna()]
            without_c = signals[(~signals[col]) & signals[excess_col].notna()]

            if len(with_c) < 10 or len(without_c) < 10:
                continue

            wr_w = (with_c[excess_col] > 0).mean()
            wr_wo = (without_c[excess_col] > 0).mean()
            ae_w = with_c[excess_col].mean()
            ae_wo = without_c[excess_col].mean()
            pk_w = with_c[peak_col].mean() if peak_col in with_c.columns else np.nan
            pk_wo = without_c[peak_col].mean() if peak_col in without_c.columns else np.nan
            dd_w = with_c[dd_col].mean() if dd_col in with_c.columns else np.nan
            dd_wo = without_c[dd_col].mean() if dd_col in without_c.columns else np.nan
            dp_w = with_c[days_col].mean() if days_col in with_c.columns else np.nan
            dp_wo = without_c[days_col].mean() if days_col in without_c.columns else np.nan

            t_stat, p_value = np.nan, np.nan
            try:
                t_stat, p_value = stats.ttest_ind(
                    with_c[excess_col].dropna(),
                    without_c[excess_col].dropna(),
                    equal_var=False,
                )
            except Exception:
                pass

            rows.append(
                {
                    "abs_pct_threshold": thresh,
                    "horizon": horizon,
                    "n_with": len(with_c),
                    "wr_with": wr_w,
                    "excess_with": ae_w,
                    "peak_with": pk_w,
                    "dd_with": dd_w,
                    "days_to_peak_with": dp_w,
                    "n_without": len(without_c),
                    "wr_without": wr_wo,
                    "excess_without": ae_wo,
                    "peak_without": pk_wo,
                    "dd_without": dd_wo,
                    "days_to_peak_without": dp_wo,
                    "wr_diff": wr_w - wr_wo,
                    "excess_diff": ae_w - ae_wo,
                    "t_stat": t_stat,
                    "p_value": p_value,
                }
            )
    return pd.DataFrame(rows)


def run_signal_type_breakdown(
    signals: pd.DataFrame, threshold: float = 3.0, horizon: int = 40
) -> pd.DataFrame:
    col = f"catalyst_abs{threshold:.0f}"
    excess_col = f"excess_{horizon}d"
    rows = []
    for sig_type in signals["signal_type"].dropna().unique():
        sub = signals[signals["signal_type"] == sig_type]
        with_c = sub[(sub[col]) & sub[excess_col].notna()]
        without_c = sub[(~sub[col]) & sub[excess_col].notna()]
        if len(with_c) < 5 or len(without_c) < 5:
            continue
        rows.append(
            {
                "signal_type": sig_type,
                "n_with": len(with_c),
                "wr_with": (with_c[excess_col] > 0).mean(),
                "excess_with": with_c[excess_col].mean(),
                "n_without": len(without_c),
                "wr_without": (without_c[excess_col] > 0).mean(),
                "excess_without": without_c[excess_col].mean(),
                "wr_diff": (with_c[excess_col] > 0).mean()
                - (without_c[excess_col] > 0).mean(),
                "excess_diff": with_c[excess_col].mean()
                - without_c[excess_col].mean(),
            }
        )
    return pd.DataFrame(rows)


# ── 报告生成 ────────────────────────────────────────────

def generate_report(
    bt: pd.DataFrame,
    breakdown: pd.DataFrame,
    threshold: float = 3.0,
) -> str:
    lines = [
        "# 催化剂层代理回测验证报告",
        "",
        "## 方法说明",
        "",
        "catalyst_titles.csv 暂无历史数据（无法直接回测催化标题层），",
        "concept_daily（同花顺 THS）仅有 pct_change 字段有值。",
        "",
        "因此用**概念涨跌幅绝对值**作为市场关注度代理变量：",
        "",
        "- 催化代理定义：信号日当天，该行业关联的概念中至少有一个 |涨跌幅| >= N%",
        "- 逻辑链：媒体催化/市场注意力 → 概念价格大幅波动 → 高 |pct_change|",
        "- 关联方式：行业关键词匹配 THS 概念名（45/110 行业有概念映射，共 111 个关联概念）",
        "",
        "## 核心结论（|pct| >= 3%, horizon=40d）",
        "",
    ]

    key = bt[(bt["abs_pct_threshold"] == threshold) & (bt["horizon"] == 40)]
    if not key.empty:
        r = key.iloc[0]
        lines.append("| 指标 | 有催化代理 | 无催化代理 | 差异 |")
        lines.append("| --- | --- | --- | --- |")
        lines.append(f"| 样本量 | {int(r['n_with'])} | {int(r['n_without'])} | — |")
        lines.append(
            f"| 40日超额胜率 | {r['wr_with']:.1%} | {r['wr_without']:.1%} | {r['wr_diff']:+.1%} |"
        )
        lines.append(
            f"| 平均超额 | {r['excess_with']:.2%} | {r['excess_without']:.2%} | {r['excess_diff']:+.2%} |"
        )
        if pd.notna(r.get("peak_with")):
            lines.append(
                f"| 平均峰值 | {r['peak_with']:.2%} | {r['peak_without']:.2%} | "
                f"{(r['peak_with'] - r['peak_without']):+.2%} |"
            )
        if pd.notna(r.get("dd_with")):
            lines.append(
                f"| 平均最大回撤 | {r['dd_with']:.2%} | {r['dd_without']:.2%} | "
                f"{(r['dd_with'] - r['dd_without']):+.2%} |"
            )
        if pd.notna(r.get("p_value")):
            sig = "显著" if r["p_value"] < 0.05 else "不显著"
            lines.append(f"| T检验 p值 | {r['p_value']:.3f} | — | {sig} |")
        lines.append("")

    # 敏感性表
    lines.append("## 不同阈值敏感性分析")
    lines.append("")
    lines.append(
        "| 阈值 | 期限 | 有催化N | 有催化胜率 | 无催化胜率 | 胜率差 | 超额差 | P值 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for _, r in bt.iterrows():
        p_str = f"{r['p_value']:.3f}" if pd.notna(r["p_value"]) else "NA"
        lines.append(
            f"| >=|{r['abs_pct_threshold']:.0f}%| | {int(r['horizon'])}d | "
            f"{int(r['n_with'])} | {r['wr_with']:.1%} | {r['wr_without']:.1%} | "
            f"{r['wr_diff']:+.1%} | {r['excess_diff']:+.2%} | {p_str} |"
        )
    lines.append("")

    # 信号类型拆分
    if not breakdown.empty:
        lines.append(f"## 信号类型拆分（>=|3%|, 40d）")
        lines.append("")
        lines.append(
            "| 信号类型 | 有催化N | 有催化胜率 | 有催化超额 | 无催化N | 无催化胜率 | 无催化超额 | 胜率差 | 超额差 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for _, r in breakdown.iterrows():
            lines.append(
                f"| {r['signal_type']} | {int(r['n_with'])} | {r['wr_with']:.1%} | "
                f"{r['excess_with']:.2%} | {int(r['n_without'])} | {r['wr_without']:.1%} | "
                f"{r['excess_without']:.2%} | {r['wr_diff']:+.1%} | {r['excess_diff']:+.2%} |"
            )
        lines.append("")

    # 解读
    lines.append("## 解读与结论")
    lines.append("")

    if not key.empty:
        diff = key.iloc[0]["wr_diff"]
        ediff = key.iloc[0]["excess_diff"]
        pv = key.iloc[0]["p_value"]
        nw = int(key.iloc[0]["n_with"])
        nwo = int(key.iloc[0]["n_without"])

        if diff > 0.03 and pd.notna(pv) and pv < 0.05:
            lines.append(
                f"**结论：催化有明显正向增量。** 有概念大幅波动的信号 40d 胜率 {diff:+.1%}，"
                f"超额差 {ediff:+.2%}，p={pv:.3f} 统计显著。"
                f"（有催化组 N={nw}，无催化组 N={nwo}）"
            )
            lines.append("")
            lines.append(
                "这意味着：当一个行业出现早期信号时，如果关联概念同时出现大幅价格波动，"
                "该信号的后续表现更好。价格+注意力的组合优于单纯的价格信号。"
            )
        elif diff > 0:
            lines.append(
                f"**结论：催化的正向方向存在但不显著。** 胜率差 {diff:+.1%}，"
                f"超额差 {ediff:+.2%}。有催化组 N={nw}。"
            )
            lines.append("")
            lines.append(
                "方向是正的，目前的样本量不足以得出统计显著结论。"
                "真实催化剂层（新闻标题匹配）比 |pct_change| 代理更精准，效果可能更强。"
            )
        else:
            lines.append(
                f"**结论：概念波动代理无正向增量。** 胜率差 {diff:+.1%}。"
            )
            lines.append("")
            lines.append(
                "|pct_change| 作为代理变量有一个根本问题：它把'好消息'和'坏消息'混在一起。"
                "概念大跌（-5%）也会触发代理标记，但这不代表正向催化。"
                "真实催化剂层通过关键词的 tone（positive/risk）来区分，精度更高。"
            )

    lines.append("")
    lines.append("## 对催化剂层的启示")
    lines.append("")
    lines.append(
        "- **代理变量的根本局限：** |pct_change| 不分方向，大涨和暴跌都算'有催化'。"
        "真实催化剂层通过 tone 标签区分正面催化和情绪风险，精度更高。"
    )
    lines.append(
        "- **下一步：** 如果用带方向的 pct_change（只看正涨幅或负涨幅）能得到更干净的信号，"
        "说明催化剂层的关键价值就在于区分'有人关注'和'有逻辑支撑的关注'。"
    )
    lines.append(
        "- **长期方案不变：** 每日运行 sync_catalysts.py，3~6 个月后做正式回测。"
    )

    return "\n".join(lines)


# ── 主流程 ──────────────────────────────────────────────

def main() -> None:
    print("1. 加载回测样本...")
    signals = load_samples()
    print(f"   early_core_env45: {len(signals)}")

    print("2. 加载概念名称→代码...")
    name_to_code = load_concept_name_to_code()
    print(f"   概念总数: {len(name_to_code)}")

    print("3. 构建行业→概念映射...")
    industry_map = load_industry_concept_map()
    mapped = sum(1 for v in industry_map.values() if v)
    print(f"   有概念映射的行业: {mapped}/{len(industry_map)}")

    print("4. 加载概念 pct_change 数据...")
    all_codes = list(
        dict.fromkeys(
            name_to_code.get(cn)
            for v in industry_map.values()
            for cn in v
            if cn in name_to_code
        )
    )
    print(f"   关联概念 ts_code: {len(all_codes)}")
    pct_data = load_concept_pct_data(all_codes)
    print(f"   概念日线行数: {len(pct_data)}")

    if pct_data.empty:
        print("   [ERROR] 无数据")
        return

    thresholds = [2.0, 3.0, 4.0, 5.0]
    for th in thresholds:
        print(f"5. 标记催化代理 (|pct| >= {th:.0f}%)...")
        signals = enrich_signals_with_catalyst_proxy(
            signals, pct_data, industry_map, name_to_code, abs_pct_threshold=th
        )
        signals[f"catalyst_abs{th:.0f}"] = signals["catalyst_proxy"]
        n = signals[f"catalyst_abs{th:.0f}"].sum()
        print(f"   有催化代理: {n}/{len(signals)} ({n/len(signals)*100:.1f}%)")

    print("6. 运行分组对比...")
    bt = run_backtest(signals, thresholds=thresholds)
    print(f"   有效对比: {len(bt)} 行")
    if not bt.empty:
        print(bt.to_string())

    print("7. 信号类型拆分...")
    breakdown = run_signal_type_breakdown(signals, threshold=3.0)
    if not breakdown.empty:
        print(breakdown.to_string())

    print("8. 生成报告...")
    report = generate_report(bt, breakdown, threshold=3.0)
    (OUT_DIR / "catalyst_proxy_backtest_report.md").write_text(report, encoding="utf-8")

    cols = [
        "date", "industry", "signal_type", "env_score", "grade",
        "ret5", "ret20", "ret40", "ret60",
        "excess_20d", "excess_40d", "excess_60d",
        "peak_ret_40d", "max_drawdown_40d", "days_to_peak_40d",
        "surge_concepts", "max_abs_pct",
    ]
    for th in thresholds:
        cols.append(f"catalyst_abs{th:.0f}")
    signals[[c for c in cols if c in signals.columns]].to_csv(
        OUT_DIR / "catalyst_proxy_signals.csv", index=False
    )

    print(f"\n=== 完成 ===")
    print(report)


if __name__ == "__main__":
    main()
