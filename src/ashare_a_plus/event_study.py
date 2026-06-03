from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class EventStudyConfig:
    horizon_days: int = 60
    cooldown_days: int = 60
    random_seed: int = 42


def mature_signal_end(dates: Iterable[pd.Timestamp], horizon_days: int) -> pd.Timestamp:
    ordered = pd.Series(pd.to_datetime(list(dates))).drop_duplicates().sort_values().reset_index(drop=True)
    if len(ordered) <= horizon_days:
        raise ValueError("not enough trading days for requested horizon")
    return ordered.iloc[-horizon_days - 1]


def build_event_samples(
    signals: pd.DataFrame,
    config: EventStudyConfig,
    groups: tuple[str, ...] = ("a_plus", "random", "rs_top", "breakout"),
) -> pd.DataFrame:
    df = signals.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["row_idx"] = df.groupby("symbol").cumcount()
    df["random_pool"] = (
        df["enough_history"].fillna(False)
        & df["liquid"].fillna(False)
        & df["close"].notna()
        & df["open"].notna()
    )
    df["rs_control_pool"] = (
        df["random_pool"]
        & df["rs_top_20pct"].fillna(False)
        & ~df["a_plus_signal"].fillna(False)
    )
    df["breakout_control_pool"] = (
        df["random_pool"]
        & df["pivot_breakout"].fillna(False)
        & ~df["a_plus_signal"].fillna(False)
    )

    a_plus_rows = _deduplicate_signals(df[df["a_plus_signal"].fillna(False)], config.cooldown_days)
    if a_plus_rows.empty:
        return pd.DataFrame()

    samples = []
    rng = np.random.default_rng(config.random_seed)
    by_date = {date: frame for date, frame in df.groupby("date", sort=False)}
    by_symbol = {symbol: frame.reset_index(drop=True) for symbol, frame in df.groupby("symbol", sort=False)}

    for _, signal in a_plus_rows.iterrows():
        date = signal["date"]
        samples.append(_sample_from_signal(by_symbol, signal, "a_plus", "A+体系", config.horizon_days))

        date_frame = by_date[date]
        if "random" in groups:
            row = _pick_control(date_frame, "random_pool", rng, exclude_symbol=signal["symbol"])
            samples.append(_sample_from_signal(by_symbol, row, "random", "全市场随机", config.horizon_days))
        if "rs_top" in groups:
            row = _pick_control(date_frame, "rs_control_pool", rng, exclude_symbol=signal["symbol"])
            samples.append(_sample_from_signal(by_symbol, row, "rs_top", "RS前20%非A+", config.horizon_days))
        if "breakout" in groups:
            row = _pick_control(date_frame, "breakout_control_pool", rng, exclude_symbol=signal["symbol"])
            samples.append(_sample_from_signal(by_symbol, row, "breakout", "50日突破非A+", config.horizon_days))

    out = pd.DataFrame([row for row in samples if row is not None])
    return out.sort_values(["group", "signal_date", "symbol"]).reset_index(drop=True)


def summarize_event_samples(samples: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for group, frame in samples.groupby("group", sort=False):
        returns = frame["return_pct"].astype(float) / 100
        wins = returns[returns > 0]
        losses = returns[returns < 0]
        avg_win = wins.mean() if not wins.empty else 0.0
        avg_loss_abs = abs(losses.mean()) if not losses.empty else 0.0
        gross_profit = wins.sum()
        gross_loss = abs(losses.sum())
        rows.append(
            {
                "group": group,
                "label": frame["group_label"].iloc[0],
                "samples": len(frame),
                "win_rate": (returns > 0).mean(),
                "avg_return": returns.mean(),
                "median_return": returns.median(),
                "avg_win": avg_win,
                "avg_loss": -avg_loss_abs,
                "profit_loss_ratio": avg_win / avg_loss_abs if avg_loss_abs else np.inf if avg_win > 0 else 0.0,
                "profit_factor": gross_profit / gross_loss if gross_loss else np.inf if gross_profit > 0 else 0.0,
                "avg_max_gain": frame["max_gain_pct"].mean() / 100,
                "median_max_gain": frame["max_gain_pct"].median() / 100,
                "avg_max_drawdown": frame["max_drawdown_pct"].mean() / 100,
                "median_days_to_high": frame["days_to_high"].median(),
                "avg_days_to_high": frame["days_to_high"].mean(),
                "hit_10_rate": frame["hit_10"].mean(),
                "hit_15_rate": frame["hit_15"].mean(),
                "hit_20_rate": frame["hit_20"].mean(),
                "hit_30_rate": frame["hit_30"].mean(),
                "stop_7_rate": frame["hit_stop_7"].mean(),
                "stop_10_rate": frame["hit_stop_10"].mean(),
                "tp15_before_sl7_rate": frame["tp15_before_sl7"].mean(),
            }
        )
    return pd.DataFrame(rows)


def write_event_study_report(
    report_dir: Path,
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    horizon_days: int,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    samples.to_csv(report_dir / "event_samples.csv", index=False)
    summary.to_csv(report_dir / "event_summary.csv", index=False)
    (report_dir / "event_study_report.md").write_text(
        _event_report_markdown(samples, summary, signal_start, signal_end, horizon_days),
        encoding="utf-8",
    )


def _deduplicate_signals(signal_rows: pd.DataFrame, cooldown_days: int) -> pd.DataFrame:
    kept = []
    for _, group in signal_rows.sort_values(["symbol", "date"]).groupby("symbol", sort=False):
        last_idx = -10**9
        for _, row in group.iterrows():
            idx = int(row["row_idx"])
            if idx - last_idx >= cooldown_days:
                kept.append(row)
                last_idx = idx
    return pd.DataFrame(kept)


def _pick_control(date_frame: pd.DataFrame, pool_col: str, rng: np.random.Generator, exclude_symbol: str) -> pd.Series | None:
    pool = date_frame[(date_frame[pool_col]) & (date_frame["symbol"] != exclude_symbol)]
    if pool.empty:
        return None
    idx = int(rng.integers(0, len(pool)))
    return pool.iloc[idx]


def _sample_from_signal(by_symbol: dict[str, pd.DataFrame], signal: pd.Series | None, group: str, label: str, horizon_days: int) -> dict | None:
    if signal is None:
        return None
    symbol = signal["symbol"]
    group_df = by_symbol.get(symbol)
    if group_df is None:
        return None
    signal_idx = int(signal["row_idx"])
    entry_idx = signal_idx + 1
    exit_idx = entry_idx + horizon_days - 1
    if entry_idx >= len(group_df) or exit_idx >= len(group_df):
        return None
    entry = group_df.loc[entry_idx]
    exit_row = group_df.loc[exit_idx]
    path = group_df.loc[entry_idx:exit_idx].copy()
    entry_price = float(entry["open"])
    if entry_price <= 0:
        return None

    high_rel = path["high"].astype(float) / entry_price - 1
    low_rel = path["low"].astype(float) / entry_price - 1
    high_pos = int(high_rel.values.argmax())
    max_gain = float(high_rel.iloc[high_pos])
    max_drawdown = float(low_rel.min())
    hit_10 = high_rel >= 0.10
    hit_15 = high_rel >= 0.15
    hit_20 = high_rel >= 0.20
    hit_30 = high_rel >= 0.30
    hit_stop_7 = low_rel <= -0.07
    hit_stop_10 = low_rel <= -0.10
    first_tp15 = _first_true_position(hit_15)
    first_sl7 = _first_true_position(hit_stop_7)

    return {
        "group": group,
        "group_label": label,
        "symbol": symbol,
        "name": signal.get("name", symbol),
        "industry": signal.get("industry", ""),
        "signal_date": signal["date"].date().isoformat(),
        "entry_date": entry["date"].date().isoformat(),
        "exit_date": exit_row["date"].date().isoformat(),
        "entry_price": entry_price,
        "exit_price": float(exit_row["close"]),
        "return_pct": (float(exit_row["close"]) / entry_price - 1) * 100,
        "max_gain_pct": max_gain * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "days_to_high": high_pos + 1,
        "hit_10": bool(hit_10.any()),
        "hit_15": bool(hit_15.any()),
        "hit_20": bool(hit_20.any()),
        "hit_30": bool(hit_30.any()),
        "hit_stop_7": bool(hit_stop_7.any()),
        "hit_stop_10": bool(hit_stop_10.any()),
        "tp15_before_sl7": first_tp15 is not None and (first_sl7 is None or first_tp15 < first_sl7),
        "first_tp15_day": first_tp15 + 1 if first_tp15 is not None else "",
        "first_sl7_day": first_sl7 + 1 if first_sl7 is not None else "",
        "rs_rank_pct": signal.get("rs_rank_pct", np.nan),
        "pivot_50d": signal.get("pivot_50d", np.nan),
        "signal_close": signal.get("close", np.nan),
    }


def _first_true_position(series: pd.Series) -> int | None:
    values = series.to_numpy()
    positions = np.flatnonzero(values)
    if len(positions) == 0:
        return None
    return int(positions[0])


def _event_report_markdown(
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    horizon_days: int,
) -> str:
    table_rows = []
    for _, row in summary.iterrows():
        table_rows.append(
            "| {label} | {samples} | {win_rate:.2%} | {avg_return:.2%} | {median_return:.2%} | {pl:.2f} | {pf:.2f} | {mg:.2%} | {dd:.2%} | {days:.1f} | {h15:.2%} | {sl7:.2%} | {tp_before:.2%} |".format(
                label=row["label"],
                samples=int(row["samples"]),
                win_rate=row["win_rate"],
                avg_return=row["avg_return"],
                median_return=row["median_return"],
                pl=row["profit_loss_ratio"],
                pf=row["profit_factor"],
                mg=row["avg_max_gain"],
                dd=row["avg_max_drawdown"],
                days=row["avg_days_to_high"],
                h15=row["hit_15_rate"],
                sl7=row["stop_7_rate"],
                tp_before=row["tp15_before_sl7_rate"],
            )
        )

    a_plus = summary[summary["group"] == "a_plus"]
    verdict = "样本不足，暂不下结论。"
    if not a_plus.empty:
        a = a_plus.iloc[0]
        verdict = (
            f"A+ 样本 {int(a['samples'])} 个，60日平均收益 {a['avg_return']:.2%}，"
            f"胜率 {a['win_rate']:.2%}，平均最大涨幅 {a['avg_max_gain']:.2%}，"
            f"平均最大回撤 {a['avg_max_drawdown']:.2%}，平均 {a['avg_days_to_high']:.1f} 天见到区间最高价。"
        )

    top = samples[samples["group"] == "a_plus"].sort_values("max_gain_pct", ascending=False).head(10)
    top_lines = [
        f"- {r.symbol} {r.name}: 最大涨幅 {r.max_gain_pct:.2f}%, 到高点 {int(r.days_to_high)} 天, 60日收益 {r.return_pct:.2f}%"
        for r in top.itertuples()
    ]

    return f"""# A+ 选股体系事件研究报告

## 一句话结论

{verdict}

这份报告验证的是：信号出现后，次日开盘买入，未来 {horizon_days} 个交易日内的收益、最高涨幅、最大回撤，以及到达最高价需要多久。报告不是收益保证，也不是直接交易建议。

## 样本范围

- 信号窗口：{signal_start.date()} 到 {signal_end.date()}
- 持有观察期：{horizon_days} 个交易日
- 买入规则：信号日之后的下一个交易日开盘价
- A+ 去重规则：同一股票 {horizon_days} 个交易日内只取第一次信号
- 对照组：每个 A+ 信号日抽取同日样本，保证市场环境一致

## 核心对比

| 组别 | 样本数 | 60日胜率 | 60日平均收益 | 60日中位收益 | 盈亏比 | Profit Factor | 平均最大涨幅 | 平均最大回撤 | 平均到高点天数 | 触达+15% | 触达-7% | 先+15%后-7% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{chr(10).join(table_rows)}

## 怎么读这些指标

- 60日胜率：到第60个交易日收盘是否赚钱。
- 平均最大涨幅：买入后60日内，曾经最多涨多少，衡量选股后有没有爆发力。
- 平均最大回撤：买入后60日内，曾经最多亏多少，衡量过程风险。
- 平均到高点天数：信号后通常第几天出现未来60日最高价，越短越适合短线，越长越需要耐心。
- 先+15%后-7%：在同一观察期内，是否先涨到+15%，再说止损。这是交易可执行性的重要指标。

## A+ 最大涨幅前10

{chr(10).join(top_lines) if top_lines else '- 无样本'}

## 文件

- 明细：`event_samples.csv`
- 汇总：`event_summary.csv`
"""
