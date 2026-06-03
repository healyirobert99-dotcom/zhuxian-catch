import numpy as np
import pandas as pd

from ashare_a_plus.config import StrategyConfig
from ashare_a_plus.indicators import add_indicators, generate_a_plus_signals


def _make_prices(symbol: str, start_price: float, breakout: bool) -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", periods=280)
    trend = np.linspace(start_price, start_price * 2.0, len(dates))
    wave = np.sin(np.linspace(0, 18, len(dates))) * 0.4
    close = trend + wave
    close[-60:-5] = np.linspace(close[-60], close[-60] * 1.03, 55)
    if breakout:
        close[-1] = close[-2] * 1.035
    high = close * 1.01
    low = close * 0.99
    open_ = close * 0.995
    volume = np.full(len(dates), 1_000_000.0)
    volume[-10:-1] = 700_000.0
    volume[-1] = 2_000_000.0 if breakout else 800_000.0
    amount = volume * close
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        }
    )


def test_generate_a_plus_signal_uses_mechanical_rules():
    prices = pd.concat(
        [
            _make_prices("000001", 20, breakout=True),
            _make_prices("000002", 10, breakout=False),
        ],
        ignore_index=True,
    )
    config = StrategyConfig(min_amount=1, rs_top_quantile=0.50)
    indicators = add_indicators(prices, config)
    signals = generate_a_plus_signals(indicators, config)

    last = signals[signals["date"] == signals["date"].max()].set_index("symbol")

    assert bool(last.loc["000001", "a_plus_signal"]) is True
    assert bool(last.loc["000002", "a_plus_signal"]) is False
    assert bool(last.loc["000001", "pivot_breakout"]) is True


def test_pivot_uses_prior_high_not_current_high():
    prices = _make_prices("000001", 20, breakout=True)
    config = StrategyConfig(min_amount=1, rs_top_quantile=0.0)
    indicators = add_indicators(prices, config)
    last = indicators.iloc[-1]
    prior_50_high = prices.iloc[-51:-1]["high"].max()

    assert last["pivot_50d"] == prior_50_high
