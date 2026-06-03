import pandas as pd

from ashare_a_plus.forward_study import (
    ForwardStudyConfig,
    build_forward_returns,
    summarize_forward_returns,
)


def test_forward_study_enters_next_open_and_exits_after_horizon():
    dates = pd.bdate_range("2024-01-01", periods=8)
    rows = []
    for i, date in enumerate(dates):
        rows.append(
            {
                "symbol": "000001",
                "name": "测试股",
                "date": date,
                "open": 10 + i,
                "high": 11 + i,
                "low": 9 + i,
                "close": 10 + i,
                "a_plus_signal": i == 0,
                "rs_rank_pct": 1.0,
                "pivot_50d": 10,
            }
        )

    study = build_forward_returns(pd.DataFrame(rows), ForwardStudyConfig(horizon_days=3))

    assert len(study) == 1
    row = study.iloc[0]
    assert row["entry_date"] == "2024-01-02"
    assert row["exit_date"] == "2024-01-04"
    assert row["entry_price"] == 11
    assert row["exit_price"] == 13
    assert row["return_pct"] == (13 / 11 - 1) * 100
    assert bool(row["is_mature"]) is True


def test_forward_study_cooldown_deduplicates_same_stock_signals():
    dates = pd.bdate_range("2024-01-01", periods=12)
    rows = []
    for i, date in enumerate(dates):
        rows.append(
            {
                "symbol": "000001",
                "date": date,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "a_plus_signal": i in [0, 3, 9],
            }
        )

    study = build_forward_returns(pd.DataFrame(rows), ForwardStudyConfig(horizon_days=2, cooldown_days=5))

    assert study["signal_date"].tolist() == ["2024-01-01", "2024-01-12"]


def test_forward_summary_reports_win_rate_and_profit_loss_ratio():
    study = pd.DataFrame(
        {
            "return_pct": [10.0, -5.0, 20.0],
            "max_gain_pct": [12.0, 2.0, 25.0],
            "max_drawdown_pct": [-2.0, -8.0, -3.0],
            "is_mature": [True, True, False],
        }
    )

    summary = summarize_forward_returns(study)

    assert summary["samples"] == 3
    assert summary["mature_samples"] == 2
    assert round(summary["win_rate"], 4) == 0.6667
    assert round(summary["profit_loss_ratio"], 2) == 3.0
