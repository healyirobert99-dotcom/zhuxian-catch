from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .event_study import EventStudyConfig, mature_signal_end, summarize_event_samples
from .indicators import add_relative_strength


@dataclass(frozen=True)
class LowVolContractionConfig:
    rs_min_rank: float = 0.70
    contraction_ratio: float = 0.70
    volume_dry_ratio: float = 1.00
    min_breakout_volume: float = 1.00
    max_breakout_volume: float = 2.00
    max_distance_to_pivot: float = 0.05
    min_listing_days: int = 250
    min_amount: float = 20_000_000.0
    limit_up_open_threshold: float = 0.095


def add_low_vol_contraction_signals(indicators: pd.DataFrame, config: LowVolContractionConfig) -> pd.DataFrame:
    df = indicators.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    grouped = df.groupby("symbol", group_keys=False)

    df = add_relative_strength(df, _RelativeStrengthConfig(config.rs_min_rank))
    df["pivot_20d"] = grouped["high"].transform(lambda s: s.rolling(20).max().shift(1))
    df["prior_high_20d"] = grouped["high"].transform(lambda s: s.shift(1).rolling(20).max())
    df["prior_low_20d"] = grouped["low"].transform(lambda s: s.shift(1).rolling(20).min())
    df["prior_high_60d"] = grouped["high"].transform(lambda s: s.shift(1).rolling(60).max())
    df["prior_low_60d"] = grouped["low"].transform(lambda s: s.shift(1).rolling(60).min())
    df["prior_range_20d"] = df["prior_high_20d"] / df["prior_low_20d"] - 1
    df["prior_range_60d"] = df["prior_high_60d"] / df["prior_low_60d"] - 1
    df["prior_avg_volume_10d"] = grouped["volume"].transform(lambda s: s.shift(1).rolling(10).mean())
    df["prior_avg_volume_50d"] = grouped["volume"].transform(lambda s: s.shift(1).rolling(50).mean())

    df["seg1_range"] = _window_range(grouped, high_shift=41, low_shift=41, window=20)
    df["seg2_range"] = _window_range(grouped, high_shift=21, low_shift=21, window=20)
    df["seg3_range"] = _window_range(grouped, high_shift=1, low_shift=1, window=20)
    df["multi_stage_contracting"] = (df["seg3_range"] < df["seg2_range"] * 0.90) & (
        df["seg2_range"] < df["seg1_range"] * 1.05
    )

    enough_history = df["listed_days"] >= config.min_listing_days
    liquid = df["amount"] >= config.min_amount
    strong = df["rs_rank_pct"] >= config.rs_min_rank
    trend_ok = df["close"] > df["sma50"]
    volatility_contracting = df["prior_range_20d"] < df["prior_range_60d"] * config.contraction_ratio
    volume_dry = df["prior_avg_volume_10d"] < df["prior_avg_volume_50d"] * config.volume_dry_ratio
    pivot_breakout_20d = df["close"] > df["pivot_20d"]
    moderate_volume = (df["volume"] > df["prior_avg_volume_50d"] * config.min_breakout_volume) & (
        df["volume"] < df["prior_avg_volume_50d"] * config.max_breakout_volume
    )
    pivot_distance = df["close"] / df["pivot_20d"] - 1
    actionable_distance = pivot_distance <= config.max_distance_to_pivot

    base_pool = enough_history & liquid & df["open"].notna() & df["close"].notna()
    df["base_pool"] = base_pool
    df["strong_pool"] = base_pool & strong & trend_ok
    df["ordinary_breakout_signal"] = df["strong_pool"] & pivot_breakout_20d
    df["low_vol_contraction_signal"] = (
        df["strong_pool"]
        & volatility_contracting
        & volume_dry
        & pivot_breakout_20d
        & moderate_volume
        & actionable_distance
    )

    df["low_vol_signal_reason"] = np.where(
        df["low_vol_contraction_signal"],
        "RS top 30% + close>SMA50 + 20d range contraction + volume dry-up + 20d pivot breakout + moderate volume",
        "",
    )
    df["pivot_distance_pct"] = pivot_distance * 100
    df["breakout_volume_multiple_50d"] = df["volume"] / df["prior_avg_volume_50d"]
    return df


def build_low_vol_validation_samples(
    signals: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    event_config: EventStudyConfig,
    low_vol_config: LowVolContractionConfig,
) -> pd.DataFrame:
    df = signals.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["row_idx"] = df.groupby("symbol").cumcount()
    df["_global_idx"] = np.arange(len(df))
    window_mask = (df["date"] >= signal_start) & (df["date"] <= signal_end)
    df["study_signal"] = df["low_vol_contraction_signal"].fillna(False) & window_mask

    focal_rows = _deduplicate_by_symbol(df[df["study_signal"]], event_config.cooldown_days)
    if focal_rows.empty:
        return pd.DataFrame()

    ordinary_pools = _date_index_pools(
        df,
        df["ordinary_breakout_signal"].fillna(False) & ~df["low_vol_contraction_signal"].fillna(False),
    )
    strong_pools = _date_index_pools(
        df,
        df["strong_pool"].fillna(False) & ~df["low_vol_contraction_signal"].fillna(False),
    )
    arrays = _sample_arrays(df)
    trading_dates = df["date"].drop_duplicates().sort_values().reset_index(drop=True)
    date_to_next = {trading_dates.iloc[i]: trading_dates.iloc[i + 1] for i in range(len(trading_dates) - 1)}
    rng = np.random.default_rng(event_config.random_seed)

    samples = []
    for _, signal in focal_rows.iterrows():
        date = signal["date"]
        samples.append(
            _sample_from_index(
                df,
                arrays,
                date_to_next,
                int(signal["_global_idx"]),
                "low_vol",
                "低波动收缩突破",
                event_config.horizon_days,
                low_vol_config.limit_up_open_threshold,
            )
        )
        breakout_control = _pick_control_from_pool(
            df,
            ordinary_pools.get(date),
            rng,
            signal["symbol"],
        )
        samples.append(
            _sample_from_index(
                df,
                arrays,
                date_to_next,
                None if breakout_control is None else int(breakout_control["_global_idx"]),
                "ordinary_breakout",
                "普通强势突破",
                event_config.horizon_days,
                low_vol_config.limit_up_open_threshold,
            )
        )
        strong_random = _pick_control_from_pool(
            df,
            strong_pools.get(date),
            rng,
            signal["symbol"],
        )
        samples.append(
            _sample_from_index(
                df,
                arrays,
                date_to_next,
                None if strong_random is None else int(strong_random["_global_idx"]),
                "strong_random",
                "强势池随机",
                event_config.horizon_days,
                low_vol_config.limit_up_open_threshold,
            )
        )

    out = pd.DataFrame([row for row in samples if row is not None])
    if out.empty:
        return out
    return out.sort_values(["group", "signal_date", "symbol"]).reset_index(drop=True)


def summarize_by_split(samples: pd.DataFrame, splits: dict[str, tuple[str, str]]) -> pd.DataFrame:
    rows = []
    frame = samples.copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"])
    for split, (start, end) in splits.items():
        part = frame[(frame["signal_date"] >= pd.Timestamp(start)) & (frame["signal_date"] <= pd.Timestamp(end))]
        if part.empty:
            continue
        summary = summarize_event_samples(part)
        summary.insert(0, "split", split)
        rows.append(summary)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def write_low_vol_validation_report(
    report_dir: Path,
    signals: pd.DataFrame,
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    split_summary: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    horizon_days: int,
    splits: dict[str, tuple[str, str]],
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    signals.to_csv(report_dir / "low_vol_signals.csv", index=False)
    samples.to_csv(report_dir / "validation_samples.csv", index=False)
    summary.to_csv(report_dir / "validation_summary.csv", index=False)
    split_summary.to_csv(report_dir / "split_summary.csv", index=False)
    (report_dir / "low_vol_validation_report.md").write_text(
        _report_markdown(samples, summary, split_summary, signal_start, signal_end, horizon_days, splits),
        encoding="utf-8",
    )


def build_two_stage_execution_samples(
    signals: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    cooldown_days: int = 60,
    max_holding_days: int = 45,
    limit_up_open_threshold: float = 0.095,
) -> pd.DataFrame:
    df = signals.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    df["row_idx"] = df.groupby("symbol").cumcount()
    df["_global_idx"] = np.arange(len(df))
    window_mask = (df["date"] >= signal_start) & (df["date"] <= signal_end)
    focal_rows = _deduplicate_by_symbol(
        df[df["low_vol_contraction_signal"].fillna(False) & window_mask],
        cooldown_days,
    )
    if focal_rows.empty:
        return pd.DataFrame()

    arrays = _execution_arrays(df)
    trading_dates = df["date"].drop_duplicates().sort_values().reset_index(drop=True)
    date_to_next = {trading_dates.iloc[i]: trading_dates.iloc[i + 1] for i in range(len(trading_dates) - 1)}
    rows = [
        _two_stage_trade_from_index(df, arrays, date_to_next, int(row["_global_idx"]), max_holding_days, limit_up_open_threshold)
        for _, row in focal_rows.iterrows()
    ]
    out = pd.DataFrame([row for row in rows if row is not None])
    if out.empty:
        return out
    return out.sort_values(["signal_date", "symbol"]).reset_index(drop=True)


def summarize_two_stage_execution(samples: pd.DataFrame) -> pd.DataFrame:
    if samples.empty:
        return pd.DataFrame()
    returns = samples["return_on_planned_pct"].astype(float) / 100
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    avg_win = wins.mean() if not wins.empty else 0.0
    avg_loss_abs = abs(losses.mean()) if not losses.empty else 0.0
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    return pd.DataFrame(
        [
            {
                "samples": len(samples),
                "win_rate": (returns > 0).mean(),
                "avg_return": returns.mean(),
                "median_return": returns.median(),
                "avg_win": avg_win,
                "avg_loss": -avg_loss_abs,
                "profit_loss_ratio": avg_win / avg_loss_abs if avg_loss_abs else np.inf if avg_win > 0 else 0.0,
                "profit_factor": gross_profit / gross_loss if gross_loss else np.inf if gross_profit > 0 else 0.0,
                "add_rate": samples["added"].mean(),
                "stop_rate": samples["exit_reason"].astype(str).str.contains("stop").mean(),
                "ma_exit_rate": samples["exit_reason"].astype(str).str.contains("ma").mean(),
                "timeout_rate": (samples["exit_reason"] == "timeout").mean(),
                "avg_holding_days": samples["holding_days"].mean(),
                "avg_max_gain": samples["max_gain_pct"].mean() / 100,
                "avg_max_drawdown": samples["max_drawdown_pct"].mean() / 100,
            }
        ]
    )


def write_two_stage_execution_report(
    report_dir: Path,
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    split_summary: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    max_holding_days: int,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    samples.to_csv(report_dir / "two_stage_execution_samples.csv", index=False)
    summary.to_csv(report_dir / "two_stage_execution_summary.csv", index=False)
    split_summary.to_csv(report_dir / "two_stage_execution_split_summary.csv", index=False)
    (report_dir / "two_stage_execution_report.md").write_text(
        _two_stage_report_markdown(samples, summary, split_summary, signal_start, signal_end, max_holding_days),
        encoding="utf-8",
    )


class _RelativeStrengthConfig:
    def __init__(self, rs_top_quantile: float):
        self.rs_top_quantile = rs_top_quantile


def _window_range(grouped: pd.core.groupby.generic.DataFrameGroupBy, high_shift: int, low_shift: int, window: int) -> pd.Series:
    high = grouped["high"].transform(lambda s: s.shift(high_shift).rolling(window).max())
    low = grouped["low"].transform(lambda s: s.shift(low_shift).rolling(window).min())
    return high / low - 1


def _deduplicate_by_symbol(signal_rows: pd.DataFrame, cooldown_days: int) -> pd.DataFrame:
    kept = []
    for _, group in signal_rows.sort_values(["symbol", "date"]).groupby("symbol", sort=False):
        last_idx = -10**9
        for _, row in group.iterrows():
            idx = int(row["row_idx"])
            if idx - last_idx >= cooldown_days:
                kept.append(row)
                last_idx = idx
    return pd.DataFrame(kept)


def _date_index_pools(df: pd.DataFrame, mask: pd.Series) -> dict[pd.Timestamp, np.ndarray]:
    pools = {}
    for date, frame in df[mask].groupby("date", sort=False):
        pools[date] = frame["_global_idx"].to_numpy(dtype=int)
    return pools


def _pick_control_from_pool(df: pd.DataFrame, pool: np.ndarray | None, rng: np.random.Generator, exclude_symbol: str) -> pd.Series | None:
    if pool is None or len(pool) == 0:
        return None
    for _ in range(5):
        idx = int(pool[int(rng.integers(0, len(pool)))])
        row = df.iloc[idx]
        if row["symbol"] != exclude_symbol:
            return row
    filtered = [int(idx) for idx in pool if df.iloc[int(idx)]["symbol"] != exclude_symbol]
    if not filtered:
        return None
    return df.iloc[int(filtered[int(rng.integers(0, len(filtered)))])]


def _sample_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "symbol": df["symbol"].to_numpy(),
        "date": df["date"].to_numpy(),
        "open": df["open"].to_numpy(dtype=float),
        "high": df["high"].to_numpy(dtype=float),
        "low": df["low"].to_numpy(dtype=float),
        "close": df["close"].to_numpy(dtype=float),
    }


def _execution_arrays(df: pd.DataFrame) -> dict[str, np.ndarray]:
    arrays = _sample_arrays(df)
    arrays["sma5"] = df["sma5"].to_numpy(dtype=float)
    arrays["sma10"] = df["sma10"].to_numpy(dtype=float)
    arrays["pivot_20d"] = df["pivot_20d"].to_numpy(dtype=float)
    return arrays


def _two_stage_trade_from_index(
    df: pd.DataFrame,
    arrays: dict[str, np.ndarray],
    date_to_next: dict[pd.Timestamp, pd.Timestamp],
    signal_idx: int,
    max_holding_days: int,
    limit_up_open_threshold: float,
) -> dict | None:
    symbol = arrays["symbol"][signal_idx]
    entry_idx = signal_idx + 1
    max_exit_idx = entry_idx + max_holding_days - 1
    if entry_idx >= len(df) or max_exit_idx >= len(df):
        return None
    if arrays["symbol"][entry_idx] != symbol or arrays["symbol"][max_exit_idx] != symbol:
        return None

    signal_date = pd.Timestamp(arrays["date"][signal_idx])
    entry_date = pd.Timestamp(arrays["date"][entry_idx])
    expected_entry_date = date_to_next.get(signal_date)
    if expected_entry_date is None or entry_date != expected_entry_date:
        return None

    entry1 = float(arrays["open"][entry_idx])
    signal_close = float(arrays["close"][signal_idx])
    pivot = float(arrays["pivot_20d"][signal_idx])
    if entry1 <= 0 or signal_close <= 0 or not np.isfinite(pivot):
        return None
    if entry1 / signal_close - 1 >= limit_up_open_threshold:
        return None
    if entry1 / pivot - 1 > 0.05:
        return None

    planned = 0.70
    trial_weight = 0.40
    add_weight = 0.30
    position_weight = trial_weight
    avg_cost = entry1
    trial_stop = entry1 * 0.93
    add_trigger = entry1 * 1.05
    added = False
    pending_add = False
    half_reduced = False
    realized = 0.0
    max_gain = -np.inf
    max_drawdown = np.inf
    exit_idx = max_exit_idx
    exit_price = float(arrays["close"][max_exit_idx])
    exit_reason = "timeout"

    for idx in range(entry_idx, max_exit_idx + 1):
        if arrays["symbol"][idx] != symbol:
            break
        open_price = float(arrays["open"][idx])
        high = float(arrays["high"][idx])
        low = float(arrays["low"][idx])
        close = float(arrays["close"][idx])

        if pending_add and not added and idx > entry_idx:
            entry2 = open_price
            avg_cost = (entry1 * trial_weight + entry2 * add_weight) / planned
            position_weight += add_weight
            added = True
            pending_add = False

        ref_cost = avg_cost
        max_gain = max(max_gain, high / ref_cost - 1)
        max_drawdown = min(max_drawdown, low / ref_cost - 1)

        stop_price = avg_cost if added else trial_stop
        if low <= stop_price:
            realized += position_weight * (stop_price / avg_cost - 1)
            exit_idx = idx
            exit_price = stop_price
            exit_reason = "post_add_breakeven_stop" if added else "trial_stop_7"
            position_weight = 0.0
            break

        profit = close / avg_cost - 1
        if added and profit >= 0.15 and close < float(arrays["sma10"][idx]):
            realized += position_weight * (close / avg_cost - 1)
            exit_idx = idx
            exit_price = close
            exit_reason = "ma10_profit_protect"
            position_weight = 0.0
            break
        if added and not half_reduced and profit >= 0.10 and close < float(arrays["sma5"][idx]):
            reduce_weight = position_weight / 2
            realized += reduce_weight * (close / avg_cost - 1)
            position_weight -= reduce_weight
            half_reduced = True

        if not added and not pending_add and close >= add_trigger and close >= pivot:
            pending_add = True

    if position_weight > 0:
        realized += position_weight * (exit_price / avg_cost - 1)

    signal = df.iloc[signal_idx]
    return {
        "symbol": symbol,
        "name": signal.get("name", symbol),
        "industry": signal.get("industry", ""),
        "signal_date": signal_date.date().isoformat(),
        "entry_date": entry_date.date().isoformat(),
        "exit_date": pd.Timestamp(arrays["date"][exit_idx]).date().isoformat(),
        "entry1": entry1,
        "trial_stop": trial_stop,
        "add_trigger": add_trigger,
        "avg_cost": avg_cost,
        "added": added,
        "half_reduced": half_reduced,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "holding_days": exit_idx - entry_idx + 1,
        "return_on_planned_pct": realized / planned * 100,
        "max_gain_pct": max_gain * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "rs_rank_pct": signal.get("rs_rank_pct", np.nan),
        "pivot_20d": pivot,
        "pivot_distance_pct": entry1 / pivot * 100 - 100,
    }


def _sample_from_index(
    df: pd.DataFrame,
    arrays: dict[str, np.ndarray],
    date_to_next: dict[pd.Timestamp, pd.Timestamp],
    signal_idx: int | None,
    group: str,
    label: str,
    horizon_days: int,
    limit_up_open_threshold: float,
) -> dict | None:
    if signal_idx is None:
        return None
    symbol = arrays["symbol"][signal_idx]
    entry_idx = signal_idx + 1
    exit_idx = entry_idx + horizon_days - 1
    if entry_idx >= len(df) or exit_idx >= len(df):
        return None
    if arrays["symbol"][entry_idx] != symbol or arrays["symbol"][exit_idx] != symbol:
        return None
    signal_date = pd.Timestamp(arrays["date"][signal_idx])
    expected_entry_date = date_to_next.get(signal_date)
    entry_date = pd.Timestamp(arrays["date"][entry_idx])
    if expected_entry_date is None or entry_date != expected_entry_date:
        return None

    entry_price = float(arrays["open"][entry_idx])
    signal_close = float(arrays["close"][signal_idx])
    if entry_price <= 0 or signal_close <= 0:
        return None
    if entry_price / signal_close - 1 >= limit_up_open_threshold:
        return None

    high_rel = arrays["high"][entry_idx : exit_idx + 1] / entry_price - 1
    low_rel = arrays["low"][entry_idx : exit_idx + 1] / entry_price - 1
    high_pos = int(high_rel.argmax())
    hit_10 = high_rel >= 0.10
    hit_15 = high_rel >= 0.15
    hit_20 = high_rel >= 0.20
    hit_30 = high_rel >= 0.30
    hit_stop_7 = low_rel <= -0.07
    hit_stop_10 = low_rel <= -0.10
    first_tp15 = _first_true_position(hit_15)
    first_sl7 = _first_true_position(hit_stop_7)
    signal = df.iloc[signal_idx]

    return {
        "group": group,
        "group_label": label,
        "symbol": symbol,
        "name": signal.get("name", symbol),
        "industry": signal.get("industry", ""),
        "signal_date": signal_date.date().isoformat(),
        "entry_date": entry_date.date().isoformat(),
        "exit_date": pd.Timestamp(arrays["date"][exit_idx]).date().isoformat(),
        "entry_price": entry_price,
        "exit_price": float(arrays["close"][exit_idx]),
        "return_pct": (float(arrays["close"][exit_idx]) / entry_price - 1) * 100,
        "max_gain_pct": float(high_rel.max()) * 100,
        "max_drawdown_pct": float(low_rel.min()) * 100,
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
        "signal_close": signal.get("close", np.nan),
        "pivot_20d": signal.get("pivot_20d", np.nan),
        "pivot_distance_pct": signal.get("pivot_distance_pct", np.nan),
        "breakout_volume_multiple_50d": signal.get("breakout_volume_multiple_50d", np.nan),
        "prior_range_20d": signal.get("prior_range_20d", np.nan),
        "prior_range_60d": signal.get("prior_range_60d", np.nan),
        "multi_stage_contracting": signal.get("multi_stage_contracting", False),
    }


def _first_true_position(series: pd.Series | np.ndarray) -> int | None:
    values = series.to_numpy() if hasattr(series, "to_numpy") else series
    positions = np.flatnonzero(values)
    if len(positions) == 0:
        return None
    return int(positions[0])


def _report_markdown(
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    split_summary: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    horizon_days: int,
    splits: dict[str, tuple[str, str]],
) -> str:
    low = summary[summary["group"] == "low_vol"]
    ordinary = summary[summary["group"] == "ordinary_breakout"]
    verdict = "样本不足，暂不下结论。"
    if not low.empty:
        lv = low.iloc[0]
        verdict = (
            f"低波动收缩突破样本 {int(lv['samples'])} 个，{horizon_days}日平均收益 {lv['avg_return']:.2%}，"
            f"胜率 {lv['win_rate']:.2%}，Profit Factor {lv['profit_factor']:.2f}，"
            f"平均最大回撤 {lv['avg_max_drawdown']:.2%}。"
        )
        if not ordinary.empty:
            ob = ordinary.iloc[0]
            verdict += (
                f" 对照的普通强势突破平均收益 {ob['avg_return']:.2%}，"
                f"胜率 {ob['win_rate']:.2%}，Profit Factor {ob['profit_factor']:.2f}。"
            )

    return f"""# 低波动收缩突破验证 1.0

## 一句话结论

{verdict}

这份报告验证的是一个候选交易假设，不是实盘建议。所有样本使用信号日之后的全市场下一交易日开盘价买入，若该股票次日没有正常交易，或开盘较信号日收盘上涨超过 9.5% 近似视为不可买入，则剔除该样本。

## 固定规则

- 强势池：上市满250个交易日、成交额不低于2000万、120日相对强度排名前30%、收盘价在SMA50之上。
- 低波动收缩：信号日前20日振幅 < 信号日前60日振幅 * 0.70。
- 量能收缩：10日均量 < 50日均量。
- 突破：收盘价突破前20日高点。
- 温和放量：当日成交量 > 50日均量，且 < 50日均量 * 2.0。
- 追价限制：收盘价距离20日pivot不超过5%。
- 对照组：同日普通强势突破、同日强势池随机，保证市场环境一致。
- 观察期：买入后 {horizon_days} 个交易日。

## 样本范围

- 信号窗口：{signal_start.date()} 到 {signal_end.date()}
- 分层：{", ".join([f"{k}={v[0]}到{v[1]}" for k, v in splits.items()])}

## 总体对比

{_summary_table(summary)}

## 分层验证

{_split_table(split_summary)}

## 怎么读

- 如果低波动收缩突破只在总体上好，但样本外测试不好，说明大概率是参数或市场阶段偶然性。
- 如果它同时优于普通强势突破和强势池随机，才说明“收缩结构”本身可能贡献了增量信息。
- 重点看平均收益、胜率、Profit Factor、触达-7%、先+15%后-7%。单看胜率不够。

## 文件

- 信号：`low_vol_signals.csv`
- 样本明细：`validation_samples.csv`
- 总体汇总：`validation_summary.csv`
- 分层汇总：`split_summary.csv`
"""


def _summary_table(summary: pd.DataFrame) -> str:
    if summary.empty:
        return "无样本。"
    rows = [
        "| 组别 | 样本数 | 胜率 | 平均收益 | 中位收益 | 盈亏比 | Profit Factor | 平均最大涨幅 | 平均最大回撤 | 触达+15% | 触达-7% | 先+15%后-7% |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in summary.iterrows():
        rows.append(
            f"| {row['label']} | {int(row['samples'])} | {row['win_rate']:.2%} | {row['avg_return']:.2%} | "
            f"{row['median_return']:.2%} | {row['profit_loss_ratio']:.2f} | {row['profit_factor']:.2f} | "
            f"{row['avg_max_gain']:.2%} | {row['avg_max_drawdown']:.2%} | {row['hit_15_rate']:.2%} | "
            f"{row['stop_7_rate']:.2%} | {row['tp15_before_sl7_rate']:.2%} |"
        )
    return "\n".join(rows)


def _split_table(split_summary: pd.DataFrame) -> str:
    if split_summary.empty:
        return "无分层样本。"
    rows = [
        "| 分层 | 组别 | 样本数 | 胜率 | 平均收益 | 中位收益 | Profit Factor | 触达-7% |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in split_summary.iterrows():
        rows.append(
            f"| {row['split']} | {row['label']} | {int(row['samples'])} | {row['win_rate']:.2%} | "
            f"{row['avg_return']:.2%} | {row['median_return']:.2%} | {row['profit_factor']:.2f} | "
            f"{row['stop_7_rate']:.2%} |"
        )
    return "\n".join(rows)


def _two_stage_report_markdown(
    samples: pd.DataFrame,
    summary: pd.DataFrame,
    split_summary: pd.DataFrame,
    signal_start: pd.Timestamp,
    signal_end: pd.Timestamp,
    max_holding_days: int,
) -> str:
    verdict = "样本不足。"
    if not summary.empty:
        row = summary.iloc[0]
        verdict = (
            f"两段式执行样本 {int(row['samples'])} 个，计划仓位口径平均收益 {row['avg_return']:.2%}，"
            f"胜率 {row['win_rate']:.2%}，Profit Factor {row['profit_factor']:.2f}，"
            f"加仓率 {row['add_rate']:.2%}，止损/保本退出率 {row['stop_rate']:.2%}。"
        )
    return f"""# 低波动收缩突破：两段式执行回测

## 一句话结论

{verdict}

这版使用《A+ 两段式试仓、加仓与止损规则》的日线机械近似，不再用固定60日收益作为硬指标。最长观察/持有周期设为 {max_holding_days} 个交易日，期间按止损、加仓、MA5/MA10利润保护、超时退出处理。

## 规则近似

- 试仓：信号后全市场下一交易日开盘买入，使用计划最大系统仓位的40%。
- 可买过滤：次日无交易跳过；次日开盘较信号收盘上涨超过9.5%跳过；开盘距离20日pivot超过5%跳过。
- 初始止损：试仓价下方7%。
- 加仓：收盘达到试仓价+5%，且收盘仍在pivot之上，则下一交易日开盘加30%，总仓位70%。
- 加仓后止损：跌到加权平均成本，全仓退出。
- 利润保护：加仓后盈利超过10%且收盘跌破MA5，减半；盈利超过15%且收盘跌破MA10，清仓。
- 超时退出：最长 {max_holding_days} 个交易日后按收盘退出。

## 样本范围

- 信号窗口：{signal_start.date()} 到 {signal_end.date()}

## 总体结果

{_two_stage_summary_table(summary)}

## 分层结果

{_two_stage_split_table(split_summary)}

## 文件

- 明细：`two_stage_execution_samples.csv`
- 总览：`two_stage_execution_summary.csv`
- 分层：`two_stage_execution_split_summary.csv`
"""


def _two_stage_summary_table(summary: pd.DataFrame) -> str:
    if summary.empty:
        return "无样本。"
    row = summary.iloc[0]
    return "\n".join(
        [
            "| 样本数 | 胜率 | 平均收益 | 中位收益 | 盈亏比 | Profit Factor | 加仓率 | 止损率 | MA退出率 | 超时率 | 平均持有天数 | 平均最大涨幅 | 平均最大回撤 |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            f"| {int(row['samples'])} | {row['win_rate']:.2%} | {row['avg_return']:.2%} | {row['median_return']:.2%} | {row['profit_loss_ratio']:.2f} | {row['profit_factor']:.2f} | {row['add_rate']:.2%} | {row['stop_rate']:.2%} | {row['ma_exit_rate']:.2%} | {row['timeout_rate']:.2%} | {row['avg_holding_days']:.1f} | {row['avg_max_gain']:.2%} | {row['avg_max_drawdown']:.2%} |",
        ]
    )


def _two_stage_split_table(split_summary: pd.DataFrame) -> str:
    if split_summary.empty:
        return "无分层样本。"
    rows = [
        "| 分层 | 样本数 | 胜率 | 平均收益 | 中位收益 | Profit Factor | 加仓率 | 止损率 | 平均持有天数 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, row in split_summary.iterrows():
        rows.append(
            f"| {row['split']} | {int(row['samples'])} | {row['win_rate']:.2%} | {row['avg_return']:.2%} | "
            f"{row['median_return']:.2%} | {row['profit_factor']:.2f} | {row['add_rate']:.2%} | "
            f"{row['stop_rate']:.2%} | {row['avg_holding_days']:.1f} |"
        )
    return "\n".join(rows)
