import pandas as pd

from ashare_a_plus.contraction_study import (
    LowVolContractionConfig,
    add_low_vol_contraction_signals,
    build_low_vol_validation_samples,
)
from ashare_a_plus.event_study import EventStudyConfig
from ashare_a_plus.indicators import add_indicators
from ashare_a_plus.config import StrategyConfig


def _history(symbol: str, signal: bool) -> list[dict]:
    dates = pd.bdate_range("2023-01-02", periods=330)
    rows = []
    for i, date in enumerate(dates):
        close = 10 + i * 0.03
        high = close * 1.02
        low = close * 0.98
        volume = 1_000_000
        if i >= 270:
            high = close * 1.005
            low = close * 0.995
            volume = 600_000
        if signal and i == 319:
            close = max(row["high"] for row in rows[-20:]) * 1.01
            high = close * 1.01
            low = close * 0.995
            volume = 1_100_000
        rows.append(
            {
                "symbol": symbol,
                "name": symbol,
                "industry": "测试",
                "date": date,
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": 50_000_000,
            }
        )
    return rows


def test_low_vol_signal_is_fully_mechanical():
    prices = pd.DataFrame(_history("000001", True) + _history("000002", False))
    indicators = add_indicators(prices, StrategyConfig())
    signals = add_low_vol_contraction_signals(indicators, LowVolContractionConfig())
    signal_date = prices.loc[(prices["symbol"] == "000001")].iloc[319]["date"]
    last = signals[signals["date"] == signal_date].set_index("symbol")

    assert bool(last.loc["000001", "low_vol_contraction_signal"]) is True
    assert bool(last.loc["000002", "low_vol_contraction_signal"]) is False
    assert last.loc["000001", "pivot_distance_pct"] <= 5


def test_low_vol_samples_require_next_market_day_entry():
    prices = pd.DataFrame(_history("000001", True) + _history("000002", False) + _history("000003", False))
    indicators = add_indicators(prices, StrategyConfig())
    signals = add_low_vol_contraction_signals(indicators, LowVolContractionConfig())
    signal_date = signals.loc[signals["low_vol_contraction_signal"], "date"].min()

    samples = build_low_vol_validation_samples(
        signals,
        signal_date,
        signal_date,
        EventStudyConfig(horizon_days=5, cooldown_days=60, random_seed=1),
        LowVolContractionConfig(),
    )

    low_vol = samples[samples["group"] == "low_vol"].iloc[0]
    assert low_vol["entry_date"] == (signal_date + pd.offsets.BDay(1)).date().isoformat()
    assert low_vol["exit_date"] == (signal_date + pd.offsets.BDay(5)).date().isoformat()
