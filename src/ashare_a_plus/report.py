from __future__ import annotations

from pathlib import Path

import pandas as pd


def write_outputs(
    report_dir: Path,
    processed_dir: Path,
    signals: pd.DataFrame,
    trades: pd.DataFrame,
    equity_curve: pd.DataFrame,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    signals.to_csv(report_dir / "signals.csv", index=False)
    trades.to_csv(report_dir / "trades.csv", index=False)
    equity_curve.to_csv(report_dir / "equity_curve.csv", index=False)
    signals.to_csv(processed_dir / "signals.csv", index=False)
    save_summary(report_dir, trades, equity_curve, signals)


def save_summary(report_dir: Path, trades: pd.DataFrame, equity_curve: pd.DataFrame, signals: pd.DataFrame) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "backtest_summary.md").write_text(_summary_markdown(trades, equity_curve, signals), encoding="utf-8")


def write_forward_study(report_dir: Path, study: pd.DataFrame, summary: dict, horizon_days: int) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    study.to_csv(report_dir / "a_plus_forward_returns.csv", index=False)
    (report_dir / "a_plus_forward_summary.md").write_text(
        _forward_summary_markdown(summary, horizon_days),
        encoding="utf-8",
    )


def _summary_markdown(trades: pd.DataFrame, equity_curve: pd.DataFrame, signals: pd.DataFrame) -> str:
    if equity_curve.empty:
        total_return = annual_return = max_drawdown = 0.0
    else:
        equity = equity_curve["equity"].astype(float)
        total_return = equity.iloc[-1] / equity.iloc[0] - 1 if equity.iloc[0] else 0.0
        dates = pd.to_datetime(equity_curve["date"])
        years = max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 1 / 365.25)
        annual_return = (1 + total_return) ** (1 / years) - 1
        max_drawdown = (equity / equity.cummax() - 1).min()

    if trades.empty:
        win_rate = avg_return = avg_holding = profit_factor = 0.0
    else:
        returns = trades["return_pct"].astype(float) / 100
        win_rate = (returns > 0).mean()
        avg_return = returns.mean()
        avg_holding = trades["holding_days"].astype(float).mean()
        gross_profit = returns[returns > 0].sum()
        gross_loss = abs(returns[returns < 0].sum())
        profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

    yearly = ""
    if not equity_curve.empty:
        curve = equity_curve.copy()
        curve["date"] = pd.to_datetime(curve["date"])
        curve["year"] = curve["date"].dt.year
        rows = []
        for year, frame in curve.groupby("year"):
            year_ret = frame["equity"].iloc[-1] / frame["equity"].iloc[0] - 1
            rows.append(f"| {year} | {year_ret:.2%} |")
        yearly = "\n".join(rows)

    return f"""# A+ Resonance Backtest Summary

This report is technical research only. It does not guarantee profits and is not direct trading advice.

## Overview

- Total return: {total_return:.2%}
- Annualized return: {annual_return:.2%}
- Max drawdown: {max_drawdown:.2%}
- Trades: {len(trades)}
- Signal rows: {int(signals.get("a_plus_signal", pd.Series(dtype=bool)).sum()) if not signals.empty else 0}
- Win rate: {win_rate:.2%}
- Average trade return: {avg_return:.2%}
- Profit factor: {profit_factor:.2f}
- Average holding days: {avg_holding:.1f}

## Yearly Performance

| Year | Return |
| --- | ---: |
{yearly}

## Rules

- Buy at next open after an A+ signal.
- Initial stop: 7%.
- Take half profit at +15%.
- Take remaining profit at +30%, or use MA trailing exits.
- Maximum holding period: 60 trading days.
"""


def _forward_summary_markdown(summary: dict, horizon_days: int) -> str:
    return f"""# A+ Forward Return Study

This report is technical research only. It does not guarantee profits and is not direct trading advice.

## Overview

- Forward horizon: {horizon_days} trading days
- A+ samples: {summary["samples"]}
- Mature samples: {summary["mature_samples"]}
- Win rate: {summary["win_rate"]:.2%}
- Average return: {summary["avg_return"]:.2%}
- Median return: {summary["median_return"]:.2%}
- Average winning return: {summary["avg_win"]:.2%}
- Average losing return: {summary["avg_loss"]:.2%}
- Profit/loss ratio: {summary["profit_loss_ratio"]:.2f}
- Profit factor: {summary["profit_factor"]:.2f}
- Average max gain during holding window: {summary["avg_max_gain"]:.2%}
- Average max drawdown during holding window: {summary["avg_max_drawdown"]:.2%}

## Method

- Signal: mechanical A+ resonance signal.
- Entry: next trading day's open after the signal.
- Exit: close after the forward horizon.
- Duplicate handling: one signal per stock per cooldown window.
- Immature samples use latest available close and are marked in CSV.
"""
