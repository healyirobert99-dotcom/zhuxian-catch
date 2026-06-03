from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pandas as pd


@dataclass(frozen=True)
class ForwardStudyConfig:
    horizon_days: int = 60
    cooldown_days: int = 60


def build_forward_returns(signals: pd.DataFrame, config: ForwardStudyConfig) -> pd.DataFrame:
    """Evaluate each A+ signal by its forward horizon return.

    Entry is the next trading day's open. Exit is the close after
    ``horizon_days`` trading days. If the horizon is not available, the latest
    available close is used and the row is marked as immature.
    """
    df = signals.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

    rows: List[dict] = []
    for symbol, group in df.groupby("symbol", sort=False):
        group = group.reset_index(drop=True)
        signal_indices = list(group.index[group["a_plus_signal"].fillna(False)])
        last_taken_idx = -10**9

        for idx in signal_indices:
            if idx - last_taken_idx < config.cooldown_days:
                continue
            entry_idx = idx + 1
            if entry_idx >= len(group):
                continue

            target_idx = entry_idx + config.horizon_days - 1
            matured = target_idx < len(group)
            exit_idx = target_idx if matured else len(group) - 1
            if exit_idx < entry_idx:
                continue

            signal = group.loc[idx]
            entry = group.loc[entry_idx]
            exit_row = group.loc[exit_idx]
            path = group.loc[entry_idx:exit_idx]
            entry_price = float(entry["open"])
            exit_price = float(exit_row["close"])
            if entry_price <= 0:
                continue

            rows.append(
                {
                    "symbol": symbol,
                    "name": signal.get("name", symbol),
                    "signal_date": signal["date"].date().isoformat(),
                    "entry_date": entry["date"].date().isoformat(),
                    "exit_date": exit_row["date"].date().isoformat(),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "return_pct": (exit_price / entry_price - 1) * 100,
                    "max_gain_pct": (float(path["high"].max()) / entry_price - 1) * 100,
                    "max_drawdown_pct": (float(path["low"].min()) / entry_price - 1) * 100,
                    "holding_days": len(path),
                    "is_mature": matured,
                    "rs_rank_pct": signal.get("rs_rank_pct"),
                    "pivot_50d": signal.get("pivot_50d"),
                    "signal_close": signal.get("close"),
                }
            )
            last_taken_idx = idx

    return pd.DataFrame(rows)


def summarize_forward_returns(study: pd.DataFrame) -> dict:
    if study.empty:
        return {
            "samples": 0,
            "mature_samples": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_loss_ratio": 0.0,
            "profit_factor": 0.0,
            "avg_max_gain": 0.0,
            "avg_max_drawdown": 0.0,
        }

    returns = study["return_pct"].astype(float) / 100
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    avg_win = wins.mean() if not wins.empty else 0.0
    avg_loss_abs = abs(losses.mean()) if not losses.empty else 0.0
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())

    return {
        "samples": len(study),
        "mature_samples": int(study["is_mature"].sum()) if "is_mature" in study else len(study),
        "win_rate": float((returns > 0).mean()),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "avg_win": float(avg_win),
        "avg_loss": float(-avg_loss_abs),
        "profit_loss_ratio": float(avg_win / avg_loss_abs) if avg_loss_abs else float("inf") if avg_win > 0 else 0.0,
        "profit_factor": float(gross_profit / gross_loss) if gross_loss else float("inf") if gross_profit > 0 else 0.0,
        "avg_max_gain": float(study["max_gain_pct"].astype(float).mean() / 100),
        "avg_max_drawdown": float(study["max_drawdown_pct"].astype(float).mean() / 100),
    }
