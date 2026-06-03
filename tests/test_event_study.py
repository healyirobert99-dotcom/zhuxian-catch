import pandas as pd

from ashare_a_plus.event_study import (
    EventStudyConfig,
    build_event_samples,
    mature_signal_end,
    summarize_event_samples,
)


def _rows(symbol, signal=False, rs=True, breakout=True):
    dates = pd.bdate_range("2024-01-01", periods=8)
    rows = []
    for i, date in enumerate(dates):
        rows.append(
            {
                "symbol": symbol,
                "name": symbol,
                "industry": "测试",
                "date": date,
                "open": 10.0,
                "high": 10.0 + i,
                "low": 10.0 - (0.2 if i < 3 else 1.0),
                "close": 10.0 + i * 0.5,
                "amount": 50_000_000,
                "a_plus_signal": signal and i == 0,
                "enough_history": True,
                "liquid": True,
                "rs_top_20pct": rs,
                "pivot_breakout": breakout,
                "rs_rank_pct": 0.9,
                "pivot_50d": 9.5,
            }
        )
    return rows


def test_mature_signal_end_leaves_forward_horizon():
    dates = pd.bdate_range("2024-01-01", periods=10)

    assert mature_signal_end(dates, 3) == dates[-4]


def test_event_samples_measure_path_metrics_and_controls():
    data = pd.DataFrame(
        _rows("000001", signal=True)
        + _rows("000002", signal=False, rs=True, breakout=False)
        + _rows("000003", signal=False, rs=False, breakout=True)
        + _rows("000004", signal=False, rs=False, breakout=False)
    )

    samples = build_event_samples(data, EventStudyConfig(horizon_days=5, cooldown_days=5, random_seed=1))
    a_plus = samples[samples["group"] == "a_plus"].iloc[0]

    assert set(samples["group"]) == {"a_plus", "random", "rs_top", "breakout"}
    assert a_plus["entry_date"] == "2024-01-02"
    assert a_plus["exit_date"] == "2024-01-08"
    assert a_plus["days_to_high"] == 5
    assert bool(a_plus["hit_15"]) is True
    assert bool(a_plus["tp15_before_sl7"]) is True


def test_event_summary_groups_are_readable():
    samples = pd.DataFrame(
        {
            "group": ["a_plus", "a_plus"],
            "group_label": ["A+体系", "A+体系"],
            "return_pct": [10.0, -5.0],
            "max_gain_pct": [20.0, 8.0],
            "max_drawdown_pct": [-3.0, -9.0],
            "days_to_high": [5, 8],
            "hit_10": [True, False],
            "hit_15": [True, False],
            "hit_20": [True, False],
            "hit_30": [False, False],
            "hit_stop_7": [False, True],
            "hit_stop_10": [False, False],
            "tp15_before_sl7": [True, False],
        }
    )

    summary = summarize_event_samples(samples)

    assert summary.iloc[0]["label"] == "A+体系"
    assert summary.iloc[0]["samples"] == 2
    assert summary.iloc[0]["win_rate"] == 0.5
