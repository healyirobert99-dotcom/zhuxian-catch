import pandas as pd

from ashare_a_plus.backtest import run_backtest
from ashare_a_plus.config import BacktestConfig


def _base_rows():
    dates = pd.bdate_range("2020-01-01", periods=5)
    rows = []
    for i, date in enumerate(dates):
        rows.append(
            {
                "symbol": "000001",
                "date": date,
                "open": 100 + i,
                "high": 101 + i,
                "low": 99 + i,
                "close": 100 + i,
                "sma5": 95,
                "sma10": 95,
                "a_plus_signal": False,
                "rs_rank_pct": 1.0,
                "signal_reason": "",
            }
        )
    return rows


def test_backtest_buys_next_open_and_stops_out():
    rows = _base_rows()
    rows[0]["a_plus_signal"] = True
    rows[0]["signal_reason"] = "test signal"
    rows[1]["open"] = 100
    rows[1]["close"] = 100
    rows[2]["low"] = 90
    rows[2]["close"] = 92
    df = pd.DataFrame(rows)

    trades, equity = run_backtest(df, BacktestConfig(initial_cash=100_000, max_positions=1))

    assert len(trades) == 1
    assert trades.iloc[0]["entry_date"] == "2020-01-02"
    assert trades.iloc[0]["exit_reason"] == "initial_stop"
    assert len(equity) == 5


def test_backtest_respects_max_positions():
    dates = pd.bdate_range("2020-01-01", periods=4)
    rows = []
    for symbol in ["000001", "000002"]:
        for date in dates:
            rows.append(
                {
                    "symbol": symbol,
                    "date": date,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "sma5": 90,
                    "sma10": 90,
                    "a_plus_signal": date == dates[0],
                    "rs_rank_pct": 1.0 if symbol == "000001" else 0.9,
                    "signal_reason": "test",
                }
            )
    trades, equity = run_backtest(pd.DataFrame(rows), BacktestConfig(initial_cash=100_000, max_positions=1, max_holding_days=2))

    assert equity["positions"].max() == 1
    assert trades["symbol"].nunique() == 1
