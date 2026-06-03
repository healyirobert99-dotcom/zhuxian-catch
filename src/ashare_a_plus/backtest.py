from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import pandas as pd

from .config import BacktestConfig


@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    remaining_fraction: float
    stop_price: float
    target1_done: bool = False
    target2_done: bool = False
    days_held: int = 0
    entry_reason: str = ""


def run_backtest(signals: pd.DataFrame, config: BacktestConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = signals.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
    dates = list(df["date"].drop_duplicates().sort_values())
    by_date = {date: frame.set_index("symbol") for date, frame in df.groupby("date")}

    cash = config.initial_cash
    positions: Dict[str, Position] = {}
    trades: List[dict] = []
    equity_rows: List[dict] = []

    pending_signals: List[str] = []

    for date in dates:
        day = by_date[date]

        # Execute signals generated on the prior trading day at today's open.
        for symbol in list(pending_signals):
            if symbol in positions or symbol not in day.index:
                continue
            if len(positions) >= config.max_positions:
                break
            row = day.loc[symbol]
            open_price = float(row["open"])
            if open_price <= 0:
                continue
            mark_equity = _mark_to_market(cash, positions, day)
            budget = mark_equity * config.position_fraction
            buy_price = open_price * (1 + config.slippage)
            gross_shares = budget / buy_price
            cost = gross_shares * buy_price * (1 + config.buy_fee)
            if cost > cash:
                gross_shares = cash / (buy_price * (1 + config.buy_fee))
                cost = gross_shares * buy_price * (1 + config.buy_fee)
            if gross_shares <= 0:
                continue
            cash -= cost
            positions[symbol] = Position(
                symbol=symbol,
                entry_date=date,
                entry_price=buy_price,
                shares=gross_shares,
                remaining_fraction=1.0,
                stop_price=buy_price * (1 - config.stop_loss),
                entry_reason=str(row.get("signal_reason", "")),
            )
        pending_signals = []

        for symbol, pos in list(positions.items()):
            if symbol not in day.index:
                continue
            row = day.loc[symbol]
            pos.days_held += 1
            exits = _exit_decisions(pos, row, date, config)
            for exit_fraction, exit_price, reason in exits:
                sell_shares = pos.shares * exit_fraction
                if sell_shares <= 0:
                    continue
                proceeds = sell_shares * exit_price * (1 - config.sell_fee - config.stamp_tax)
                cash += proceeds
                trades.append(
                    {
                        "symbol": symbol,
                        "entry_date": pos.entry_date.date().isoformat(),
                        "exit_date": date.date().isoformat(),
                        "entry_price": pos.entry_price,
                        "exit_price": exit_price,
                        "shares": sell_shares,
                        "exit_fraction": exit_fraction,
                        "return_pct": (exit_price / pos.entry_price - 1) * 100,
                        "holding_days": pos.days_held,
                        "exit_reason": reason,
                        "entry_reason": pos.entry_reason,
                    }
                )
                pos.remaining_fraction -= exit_fraction
            if pos.remaining_fraction <= 0.0001:
                del positions[symbol]

        equity = _mark_to_market(cash, positions, day)
        equity_rows.append(
            {
                "date": date.date().isoformat(),
                "cash": cash,
                "equity": equity,
                "positions": len(positions),
            }
        )

        signal_rows = day.loc[day.get("a_plus_signal", False) == True]  # noqa: E712
        if not signal_rows.empty:
            slots = max(config.max_positions - len(positions), 0)
            pending_signals = list(signal_rows.sort_values("rs_rank_pct", ascending=False).head(slots).index)

    last_day = by_date[dates[-1]] if dates else pd.DataFrame()
    for symbol, pos in list(positions.items()):
        if symbol not in last_day.index:
            continue
        exit_price = float(last_day.loc[symbol, "close"]) * (1 - config.slippage)
        trades.append(
            {
                "symbol": symbol,
                "entry_date": pos.entry_date.date().isoformat(),
                "exit_date": dates[-1].date().isoformat(),
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "shares": pos.shares * pos.remaining_fraction,
                "exit_fraction": pos.remaining_fraction,
                "return_pct": (exit_price / pos.entry_price - 1) * 100,
                "holding_days": pos.days_held,
                "exit_reason": "end_of_backtest",
                "entry_reason": pos.entry_reason,
            }
        )

    return pd.DataFrame(trades), pd.DataFrame(equity_rows)


def _exit_decisions(pos: Position, row: pd.Series, date: pd.Timestamp, config: BacktestConfig) -> list[tuple[float, float, str]]:
    low = float(row["low"])
    high = float(row["high"])
    close = float(row["close"])
    exits: list[tuple[float, float, str]] = []

    if low <= pos.stop_price:
        return [(pos.remaining_fraction, pos.stop_price * (1 - config.slippage), "initial_stop")]

    if not pos.target1_done and high >= pos.entry_price * (1 + config.first_target):
        pos.target1_done = True
        exits.append((min(0.5, pos.remaining_fraction), pos.entry_price * (1 + config.first_target) * (1 - config.slippage), "target_1"))

    if not pos.target2_done and high >= pos.entry_price * (1 + config.second_target):
        pos.target2_done = True
        exits.append((pos.remaining_fraction - sum(x[0] for x in exits), pos.entry_price * (1 + config.second_target) * (1 - config.slippage), "target_2"))
        return [(f, p, r) for f, p, r in exits if f > 0]

    sold_fraction = sum(x[0] for x in exits)
    remaining_after_targets = max(pos.remaining_fraction - sold_fraction, 0)
    gain = close / pos.entry_price - 1
    if remaining_after_targets > 0 and gain >= 0.15 and close < float(row.get("sma10", close)):
        exits.append((remaining_after_targets, close * (1 - config.slippage), "trailing_ma10"))
    elif remaining_after_targets > 0 and gain >= 0.10 and close < float(row.get("sma5", close)):
        exits.append((min(0.5, remaining_after_targets), close * (1 - config.slippage), "trailing_ma5"))
    elif remaining_after_targets > 0 and pos.days_held >= config.max_holding_days:
        exits.append((remaining_after_targets, close * (1 - config.slippage), "max_holding_days"))

    return [(f, p, r) for f, p, r in exits if f > 0]


def _mark_to_market(cash: float, positions: Dict[str, Position], day: pd.DataFrame) -> float:
    equity = cash
    for symbol, pos in positions.items():
        if symbol in day.index:
            equity += pos.shares * pos.remaining_fraction * float(day.loc[symbol, "close"])
    return equity
