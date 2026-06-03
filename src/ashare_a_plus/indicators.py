from __future__ import annotations

import numpy as np
import pandas as pd

from .config import StrategyConfig


def add_indicators(prices: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    required = {"symbol", "date", "open", "high", "low", "close", "volume", "amount"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices missing columns: {sorted(missing)}")

    df = prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    grouped = df.groupby("symbol", group_keys=False)

    for window in [5, 10, 20, 50, 150, 200]:
        df[f"sma{window}"] = grouped["close"].transform(lambda s: s.rolling(window).mean())

    df["sma200_20d_ago"] = grouped["sma200"].shift(20)
    df["sma200_rising"] = df["sma200"] > df["sma200_20d_ago"]
    df["high_52w"] = grouped["high"].transform(lambda s: s.rolling(config.high_low_window).max())
    df["low_52w"] = grouped["low"].transform(lambda s: s.rolling(config.high_low_window).min())
    df["ret_120d"] = grouped["close"].pct_change(config.rs_window)
    high_20 = grouped["high"].transform(lambda s: s.rolling(20).max())
    low_20 = grouped["low"].transform(lambda s: s.rolling(20).min())
    high_60 = grouped["high"].transform(lambda s: s.rolling(60).max())
    low_60 = grouped["low"].transform(lambda s: s.rolling(60).min())
    df["range_20d"] = high_20 / low_20 - 1
    df["range_60d"] = high_60 / low_60 - 1
    df["avg_volume_10d"] = grouped["volume"].transform(lambda s: s.rolling(10).mean())
    df["avg_volume_50d"] = grouped["volume"].transform(lambda s: s.rolling(50).mean())
    df["pivot_50d"] = grouped["high"].transform(lambda s: s.rolling(config.pivot_window).max().shift(1))
    df["listed_days"] = grouped.cumcount() + 1
    return df


def add_relative_strength(indicators: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    df = indicators.copy()
    df["rs_rank_pct"] = df.groupby("date")["ret_120d"].rank(pct=True)
    df["rs_top_20pct"] = df["rs_rank_pct"] >= config.rs_top_quantile
    return df


def generate_a_plus_signals(indicators: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    df = add_relative_strength(indicators, config)
    trend_template = (
        (df["close"] > df["sma50"])
        & (df["sma50"] > df["sma150"])
        & (df["sma150"] > df["sma200"])
        & df["sma200_rising"]
    )
    near_high = df["close"] >= df["high_52w"] * (1 - config.max_distance_to_high)
    above_low = df["close"] >= df["low_52w"] * (1 + config.min_distance_from_low)
    vcp_proxy = (
        (df["range_20d"] < df["range_60d"])
        & (df["avg_volume_10d"] < df["avg_volume_50d"])
    )
    pivot_breakout = df["close"] > df["pivot_50d"]
    volume_confirmed = df["volume"] > df["avg_volume_50d"] * config.breakout_volume_multiple
    actionable = ((df["close"] / df["pivot_50d"]) - 1) <= config.max_distance_to_pivot
    enough_history = df["listed_days"] >= config.min_listing_days
    liquid = df["amount"] >= config.min_amount

    df["trend_template"] = trend_template
    df["near_52w_high"] = near_high
    df["above_52w_low"] = above_low
    df["vcp_proxy"] = vcp_proxy
    df["pivot_breakout"] = pivot_breakout
    df["volume_confirmed"] = volume_confirmed
    df["actionable_distance"] = actionable
    df["enough_history"] = enough_history
    df["liquid"] = liquid
    df["a_plus_signal"] = (
        trend_template
        & near_high
        & above_low
        & df["rs_top_20pct"]
        & vcp_proxy
        & pivot_breakout
        & volume_confirmed
        & actionable
        & enough_history
        & liquid
    )
    df["signal_reason"] = np.where(
        df["a_plus_signal"],
        "A+ resonance: trend template + RS top 20% + VCP proxy + 50d pivot breakout",
        "",
    )
    return df
