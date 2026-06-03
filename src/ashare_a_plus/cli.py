from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .backtest import run_backtest
from .config import BacktestConfig, Paths, StrategyConfig
from .contraction_study import (
    LowVolContractionConfig,
    add_low_vol_contraction_signals,
    build_two_stage_execution_samples,
    build_low_vol_validation_samples,
    summarize_by_split,
    summarize_two_stage_execution,
    write_low_vol_validation_report,
    write_two_stage_execution_report,
)
from .data import AkShareDataProvider
from .event_study import EventStudyConfig, build_event_samples, mature_signal_end, summarize_event_samples, write_event_study_report
from .forward_study import ForwardStudyConfig, build_forward_returns, summarize_forward_returns
from .indicators import add_indicators, generate_a_plus_signals
from .report import write_forward_study, write_outputs
from .sqlite_store import SQLiteStore
from .tushare_sync import TushareSync, TushareSyncConfig, token_from_env_or_arg


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share A+ resonance backtester")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="fetch data, generate signals, and run backtest")
    run_parser.add_argument("--start", default="2015-01-01")
    run_parser.add_argument("--end", required=True)
    run_parser.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"])
    run_parser.add_argument("--max-symbols", type=int, default=None)
    run_parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols, e.g. 000001,300750. Bypasses the stock-list endpoint.",
    )
    run_parser.add_argument("--raw-dir", default="data/raw")
    run_parser.add_argument("--processed-dir", default="data/processed")
    run_parser.add_argument("--report-dir", default="reports")
    run_parser.add_argument("--sqlite-db", default="data/a_stock_selector.sqlite3")
    run_parser.add_argument("--source", default="sqlite", choices=["sqlite", "akshare"])
    run_parser.add_argument("--no-cache", action="store_true")

    sync_parser = subparsers.add_parser("sync-tushare", help="sync A-share daily data into local SQLite")
    sync_parser.add_argument("--start", required=True)
    sync_parser.add_argument("--end", required=True)
    sync_parser.add_argument("--db", default="data/a_stock_selector.sqlite3")
    sync_parser.add_argument("--token", default=None, help="Tushare token. Prefer TUSHARE_TOKEN env var.")
    sync_parser.add_argument("--sleep-seconds", type=float, default=0.15)
    sync_parser.add_argument("--retries", type=int, default=3)
    sync_parser.add_argument("--adjusted", action="store_true", help="Also call adj_factor and cache back-adjusted prices.")
    sync_parser.add_argument("--no-adjusted", dest="adjusted", action="store_false", help=argparse.SUPPRESS)
    sync_parser.set_defaults(adjusted=False)
    sync_parser.add_argument("--no-skip-existing", action="store_true", help="Rewrite dates even when local rows already exist.")

    study_parser = subparsers.add_parser(
        "study-forward",
        help="scan A+ signals and summarize their forward returns",
    )
    study_parser.add_argument("--signal-start", default=None, help="Signal window start date, YYYY-MM-DD.")
    study_parser.add_argument("--signal-end", required=True, help="Signal window end date, YYYY-MM-DD.")
    study_parser.add_argument(
        "--months",
        type=int,
        default=3,
        help="Signal window length in calendar months when --signal-start is omitted.",
    )
    study_parser.add_argument("--horizon-days", type=int, default=60, help="Forward holding horizon in trading days.")
    study_parser.add_argument("--cooldown-days", type=int, default=60, help="Min trading days between same-stock samples.")
    study_parser.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"])
    study_parser.add_argument("--max-symbols", type=int, default=None)
    study_parser.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbols, e.g. 000001,300750. Bypasses the stock-list endpoint.",
    )
    study_parser.add_argument("--raw-dir", default="data/raw")
    study_parser.add_argument("--processed-dir", default="data/processed")
    study_parser.add_argument("--report-dir", default="reports")
    study_parser.add_argument("--sqlite-db", default="data/a_stock_selector.sqlite3")
    study_parser.add_argument("--source", default="sqlite", choices=["sqlite", "akshare"])
    study_parser.add_argument("--no-cache", action="store_true")

    event_parser = subparsers.add_parser(
        "event-study",
        help="compare A+ signals with same-day control groups using forward path metrics",
    )
    event_parser.add_argument("--start", default="2024-01-02")
    event_parser.add_argument("--end", default="2026-05-29")
    event_parser.add_argument("--horizon-days", type=int, default=60)
    event_parser.add_argument("--cooldown-days", type=int, default=60)
    event_parser.add_argument("--sqlite-db", default="data/a_stock_selector.sqlite3")
    event_parser.add_argument("--report-dir", default="reports/event_study")
    event_parser.add_argument("--random-seed", type=int, default=42)

    low_vol_parser = subparsers.add_parser(
        "validate-low-vol",
        help="validate low-volatility contraction breakout against same-day control groups",
    )
    low_vol_parser.add_argument("--start", default="2021-01-04")
    low_vol_parser.add_argument("--end", default="2026-05-29")
    low_vol_parser.add_argument("--horizon-days", type=int, default=60)
    low_vol_parser.add_argument("--cooldown-days", type=int, default=60)
    low_vol_parser.add_argument("--sqlite-db", default="data/a_stock_selector.sqlite3")
    low_vol_parser.add_argument("--report-dir", default="reports/low_vol_contraction_validation_5y")
    low_vol_parser.add_argument("--random-seed", type=int, default=42)

    two_stage_parser = subparsers.add_parser(
        "validate-low-vol-two-stage",
        help="validate low-volatility contraction breakout with A+ two-stage execution rules",
    )
    two_stage_parser.add_argument("--start", default="2021-01-04")
    two_stage_parser.add_argument("--end", default="2026-05-29")
    two_stage_parser.add_argument("--max-holding-days", type=int, default=45)
    two_stage_parser.add_argument("--cooldown-days", type=int, default=60)
    two_stage_parser.add_argument("--sqlite-db", default="data/a_stock_selector.sqlite3")
    two_stage_parser.add_argument("--report-dir", default="reports/low_vol_two_stage_execution_5y")

    args = parser.parse_args()
    if args.command == "run":
        run(args)
    elif args.command == "sync-tushare":
        sync_tushare(args)
    elif args.command == "study-forward":
        study_forward(args)
    elif args.command == "event-study":
        event_study(args)
    elif args.command == "validate-low-vol":
        validate_low_vol(args)
    elif args.command == "validate-low-vol-two-stage":
        validate_low_vol_two_stage(args)


def run(args: argparse.Namespace) -> None:
    paths = Paths(
        raw_dir=Path(args.raw_dir),
        processed_dir=Path(args.processed_dir),
        report_dir=Path(args.report_dir),
    )
    paths.ensure()

    strategy_config = StrategyConfig()
    backtest_config = BacktestConfig()
    if args.source == "sqlite":
        symbols = [symbol.strip().zfill(6) for symbol in args.symbols.split(",") if symbol.strip()] if args.symbols else None
        prices = SQLiteStore(Path(args.sqlite_db)).load_history(args.start, args.end, symbols=symbols)
        stock_list = prices[["symbol", "name"]].drop_duplicates("symbol") if not prices.empty else pd.DataFrame()
    else:
        provider = AkShareDataProvider(paths.raw_dir)
        if args.symbols:
            symbols = [symbol.strip().zfill(6) for symbol in args.symbols.split(",") if symbol.strip()]
            stock_list = pd.DataFrame({"symbol": symbols, "name": symbols})
        else:
            stock_list = provider.load_stock_list(use_cache=not args.no_cache)
        if args.max_symbols:
            stock_list = stock_list.head(args.max_symbols)
        histories = provider.load_many_histories(
            stock_list["symbol"],
            start=args.start,
            end=args.end,
            adjust=args.adjust,
            use_cache=not args.no_cache,
        )
        prices = histories.merge(stock_list, on="symbol", how="left")

    if args.source == "sqlite" and args.max_symbols and not prices.empty:
        keep_symbols = prices["symbol"].drop_duplicates().head(args.max_symbols)
        prices = prices[prices["symbol"].isin(keep_symbols)].copy()
        stock_list = prices[["symbol", "name"]].drop_duplicates("symbol")

    if prices.empty:
        raise SystemExit("No history data loaded. Check SQLite cache, network access, or symbol list.")

    indicators = add_indicators(prices, strategy_config)
    signals = generate_a_plus_signals(indicators, strategy_config)
    trades, equity_curve = run_backtest(signals, backtest_config)

    signals.to_csv(paths.processed_dir / "signals.csv", index=False)
    indicators.to_csv(paths.processed_dir / "indicators.csv", index=False)
    write_outputs(paths.report_dir, paths.processed_dir, signals, trades, equity_curve)

    print(f"source: {args.source}")
    print(f"symbols: {stock_list['symbol'].nunique() if not stock_list.empty else prices['symbol'].nunique()}")
    print(f"rows: {len(signals)}")
    print(f"signals: {int(signals['a_plus_signal'].sum())}")
    print(f"trades: {len(trades)}")
    print(f"report: {paths.report_dir / 'backtest_summary.md'}")


def sync_tushare(args: argparse.Namespace) -> None:
    token = token_from_env_or_arg(args.token)
    sync = TushareSync(
        TushareSyncConfig(
            token=token,
            db_path=Path(args.db),
            start=args.start,
            end=args.end,
            sleep_seconds=args.sleep_seconds,
            retries=args.retries,
            adjusted=args.adjusted,
            skip_existing=not args.no_skip_existing,
        )
    )
    stats = sync.sync()
    print("sync complete")
    print(f"db: {args.db}")
    print(f"date range: {stats['min_date']} to {stats['max_date']}")
    print(f"stock count: {stats['stock_count']}")
    print(f"daily symbols: {stats['daily_symbols']}")
    print(f"daily rows: {stats['daily_rows']}")
    print(f"daily_basic date range: {stats['daily_basic_min_date']} to {stats['daily_basic_max_date']}")
    print(f"daily_basic symbols: {stats['daily_basic_symbols']}")
    print(f"daily_basic rows: {stats['daily_basic_rows']}")
    print(f"synced days: {stats['synced_days']}")
    print(f"synced rows: {stats['synced_rows']}")
    print(f"synced daily_basic rows: {stats['synced_basic_rows']}")


def _load_prices_for_study(args: argparse.Namespace, stock_list: pd.DataFrame, data_start: str, data_end: str, paths: Paths) -> tuple[pd.DataFrame, pd.DataFrame]:
    if args.source == "sqlite":
        symbols = [symbol.strip().zfill(6) for symbol in args.symbols.split(",") if symbol.strip()] if args.symbols else None
        prices = SQLiteStore(Path(args.sqlite_db)).load_history(data_start, data_end, symbols=symbols)
        stock_list = prices[["symbol", "name"]].drop_duplicates("symbol") if not prices.empty else pd.DataFrame()
        return prices, stock_list

    provider = AkShareDataProvider(paths.raw_dir)
    if args.symbols:
        symbols = [symbol.strip().zfill(6) for symbol in args.symbols.split(",") if symbol.strip()]
        stock_list = pd.DataFrame({"symbol": symbols, "name": symbols})
    else:
        stock_list = provider.load_stock_list(use_cache=not args.no_cache)
    if args.max_symbols:
        stock_list = stock_list.head(args.max_symbols)

    histories = provider.load_many_histories(
        stock_list["symbol"],
        start=data_start,
        end=data_end,
        adjust=args.adjust,
        use_cache=not args.no_cache,
    )
    prices = histories.merge(stock_list, on="symbol", how="left") if not histories.empty else pd.DataFrame()
    return prices, stock_list


def study_forward(args: argparse.Namespace) -> None:
    paths = Paths(
        raw_dir=Path(args.raw_dir),
        processed_dir=Path(args.processed_dir),
        report_dir=Path(args.report_dir),
    )
    paths.ensure()

    signal_end = pd.Timestamp(args.signal_end)
    signal_start = pd.Timestamp(args.signal_start) if args.signal_start else signal_end - pd.DateOffset(months=args.months)
    data_start = (signal_start - pd.DateOffset(days=420)).date().isoformat()
    data_end = (signal_end + pd.DateOffset(days=max(args.horizon_days * 2, 120))).date().isoformat()

    strategy_config = StrategyConfig()
    study_config = ForwardStudyConfig(horizon_days=args.horizon_days, cooldown_days=args.cooldown_days)
    prices, stock_list = _load_prices_for_study(args, pd.DataFrame(), data_start, data_end, paths)
    if args.max_symbols and not prices.empty:
        keep_symbols = prices["symbol"].drop_duplicates().head(args.max_symbols)
        prices = prices[prices["symbol"].isin(keep_symbols)].copy()
        stock_list = prices[["symbol", "name"]].drop_duplicates("symbol")
    if prices.empty:
        raise SystemExit("No history data loaded. Check SQLite cache, network access, or symbol list.")

    indicators = add_indicators(prices, strategy_config)
    signals = generate_a_plus_signals(indicators, strategy_config)
    signal_window = signals[
        (signals["date"] >= signal_start)
        & (signals["date"] <= signal_end)
    ].copy()
    signal_symbols = signal_window.loc[signal_window["a_plus_signal"], "symbol"]
    all_for_study = signals[
        (signals["symbol"].isin(signal_symbols))
        & (signals["date"] >= signal_start)
    ].copy()
    in_signal_window = (all_for_study["date"] >= signal_start) & (all_for_study["date"] <= signal_end)
    all_for_study["a_plus_signal"] = all_for_study["a_plus_signal"] & in_signal_window
    study = build_forward_returns(all_for_study, study_config)
    summary = summarize_forward_returns(study)

    paths.processed_dir.mkdir(parents=True, exist_ok=True)
    signals.to_csv(paths.processed_dir / "signals.csv", index=False)
    signal_window.to_csv(paths.processed_dir / "signal_window.csv", index=False)
    write_forward_study(paths.report_dir, study, summary, args.horizon_days)

    print(f"source: {args.source}")
    print(f"signal window: {signal_start.date()} to {signal_end.date()}")
    print(f"data window: {data_start} to {data_end}")
    print(f"symbols: {stock_list['symbol'].nunique()}")
    print(f"A+ signal rows: {int(signal_window['a_plus_signal'].sum())}")
    print(f"samples: {summary['samples']}")
    print(f"mature samples: {summary['mature_samples']}")
    print(f"win rate: {summary['win_rate']:.2%}")
    print(f"average return: {summary['avg_return']:.2%}")
    print(f"profit/loss ratio: {summary['profit_loss_ratio']:.2f}")
    print(f"report: {paths.report_dir / 'a_plus_forward_summary.md'}")


def event_study(args: argparse.Namespace) -> None:
    db = Path(args.sqlite_db)
    report_dir = Path(args.report_dir)
    start = pd.Timestamp(args.start)
    requested_end = pd.Timestamp(args.end)
    store = SQLiteStore(db)
    print("loading local SQLite history...", flush=True)
    prices = store.load_history(start.date().isoformat(), requested_end.date().isoformat())
    if prices.empty:
        raise SystemExit("No history data loaded from SQLite cache.")

    trading_dates = prices["date"].drop_duplicates().sort_values()
    auto_end = mature_signal_end(trading_dates, args.horizon_days)
    signal_end = min(requested_end, auto_end)
    print(f"loaded rows: {len(prices)}")
    print(f"symbols: {prices['symbol'].nunique()}")
    print(f"requested signal window: {start.date()} to {requested_end.date()}")
    print(f"mature signal window: {start.date()} to {signal_end.date()}")

    indicators = add_indicators(prices, StrategyConfig())
    signals = generate_a_plus_signals(indicators, StrategyConfig())
    window_mask = (signals["date"] >= start) & (signals["date"] <= signal_end)
    study_signals = signals.copy()
    study_signals["a_plus_signal"] = study_signals["a_plus_signal"] & window_mask
    samples = build_event_samples(
        study_signals,
        EventStudyConfig(
            horizon_days=args.horizon_days,
            cooldown_days=args.cooldown_days,
            random_seed=args.random_seed,
        ),
    )
    if samples.empty:
        raise SystemExit("No mature A+ event samples found.")

    summary = summarize_event_samples(samples)
    write_event_study_report(report_dir, samples, summary, start, signal_end, args.horizon_days)

    print(f"A+ events: {len(samples[samples['group'] == 'a_plus'])}")
    for row in summary.itertuples():
        print(
            f"{row.label}: samples={row.samples}, win={row.win_rate:.2%}, "
            f"avg_return={row.avg_return:.2%}, avg_max_gain={row.avg_max_gain:.2%}, "
            f"avg_drawdown={row.avg_max_drawdown:.2%}, avg_days_to_high={row.avg_days_to_high:.1f}"
        )
    print(f"report: {report_dir / 'event_study_report.md'}")


def validate_low_vol(args: argparse.Namespace) -> None:
    db = Path(args.sqlite_db)
    report_dir = Path(args.report_dir)
    start = pd.Timestamp(args.start)
    requested_end = pd.Timestamp(args.end)
    store = SQLiteStore(db)
    prices = store.load_history(start.date().isoformat(), requested_end.date().isoformat())
    if prices.empty:
        raise SystemExit("No history data loaded from SQLite cache.")

    trading_dates = prices["date"].drop_duplicates().sort_values()
    auto_end = mature_signal_end(trading_dates, args.horizon_days)
    signal_end = min(requested_end, auto_end)
    print(f"loaded rows: {len(prices)}", flush=True)
    print(f"symbols: {prices['symbol'].nunique()}", flush=True)
    print(f"requested signal window: {start.date()} to {requested_end.date()}", flush=True)
    print(f"mature signal window: {start.date()} to {signal_end.date()}", flush=True)

    strategy_config = StrategyConfig()
    low_vol_config = LowVolContractionConfig()
    print("computing indicators...", flush=True)
    indicators = add_indicators(prices, strategy_config)
    print("generating low-vol contraction signals...", flush=True)
    signals = add_low_vol_contraction_signals(indicators, low_vol_config)
    window_mask = (signals["date"] >= start) & (signals["date"] <= signal_end)
    signal_rows = signals[window_mask & signals["low_vol_contraction_signal"]].copy()
    print(f"low-vol raw signal rows before cooldown: {len(signal_rows)}", flush=True)

    event_config = EventStudyConfig(
        horizon_days=args.horizon_days,
        cooldown_days=args.cooldown_days,
        random_seed=args.random_seed,
    )
    print("building validation samples and same-day controls...", flush=True)
    samples = build_low_vol_validation_samples(signals, start, signal_end, event_config, low_vol_config)
    if samples.empty:
        raise SystemExit("No mature low-vol contraction samples found.")

    summary = summarize_event_samples(samples)
    splits = {
        "train_2021_2023": ("2021-01-04", "2023-12-29"),
        "validation_2024": ("2024-01-01", "2024-12-31"),
        "test_2025_2026": ("2025-01-01", signal_end.date().isoformat()),
    }
    split_summary = summarize_by_split(samples, splits)
    print("writing report files...", flush=True)
    write_low_vol_validation_report(
        report_dir,
        signal_rows,
        samples,
        summary,
        split_summary,
        start,
        signal_end,
        args.horizon_days,
        splits,
    )

    print(f"low-vol raw signal rows: {len(signal_rows)}")
    print(f"low-vol mature events: {len(samples[samples['group'] == 'low_vol'])}")
    for row in summary.itertuples():
        print(
            f"{row.label}: samples={row.samples}, win={row.win_rate:.2%}, "
            f"avg_return={row.avg_return:.2%}, pf={row.profit_factor:.2f}, "
            f"avg_drawdown={row.avg_max_drawdown:.2%}, stop7={row.stop_7_rate:.2%}"
        )
    print(f"report: {report_dir / 'low_vol_validation_report.md'}")


def validate_low_vol_two_stage(args: argparse.Namespace) -> None:
    db = Path(args.sqlite_db)
    report_dir = Path(args.report_dir)
    start = pd.Timestamp(args.start)
    requested_end = pd.Timestamp(args.end)
    store = SQLiteStore(db)
    print("loading local SQLite history...", flush=True)
    prices = store.load_history(start.date().isoformat(), requested_end.date().isoformat())
    if prices.empty:
        raise SystemExit("No history data loaded from SQLite cache.")

    trading_dates = prices["date"].drop_duplicates().sort_values()
    auto_end = mature_signal_end(trading_dates, args.max_holding_days + 2)
    signal_end = min(requested_end, auto_end)
    print(f"loaded rows: {len(prices)}", flush=True)
    print(f"symbols: {prices['symbol'].nunique()}", flush=True)
    print(f"mature signal window: {start.date()} to {signal_end.date()}", flush=True)

    print("computing indicators...", flush=True)
    indicators = add_indicators(prices, StrategyConfig())
    print("generating low-vol contraction signals...", flush=True)
    signals = add_low_vol_contraction_signals(indicators, LowVolContractionConfig())
    print("running two-stage execution simulation...", flush=True)
    samples = build_two_stage_execution_samples(
        signals,
        start,
        signal_end,
        cooldown_days=args.cooldown_days,
        max_holding_days=args.max_holding_days,
    )
    if samples.empty:
        raise SystemExit("No mature two-stage samples found.")

    summary = summarize_two_stage_execution(samples)
    splits = {
        "train_2021_2023": ("2021-01-04", "2023-12-29"),
        "validation_2024": ("2024-01-01", "2024-12-31"),
        "test_2025_2026": ("2025-01-01", signal_end.date().isoformat()),
    }
    split_summary = _two_stage_split_summary(samples, splits)
    write_two_stage_execution_report(report_dir, samples, summary, split_summary, start, signal_end, args.max_holding_days)

    row = summary.iloc[0]
    print(
        f"two-stage: samples={int(row.samples)}, win={row.win_rate:.2%}, avg_return={row.avg_return:.2%}, "
        f"pf={row.profit_factor:.2f}, add_rate={row.add_rate:.2%}, stop_rate={row.stop_rate:.2%}, "
        f"avg_holding_days={row.avg_holding_days:.1f}"
    )
    print(f"report: {report_dir / 'two_stage_execution_report.md'}")


def _two_stage_split_summary(samples: pd.DataFrame, splits: dict[str, tuple[str, str]]) -> pd.DataFrame:
    rows = []
    frame = samples.copy()
    frame["signal_date"] = pd.to_datetime(frame["signal_date"])
    for split, (start, end) in splits.items():
        part = frame[(frame["signal_date"] >= pd.Timestamp(start)) & (frame["signal_date"] <= pd.Timestamp(end))]
        if part.empty:
            continue
        summary = summarize_two_stage_execution(part)
        summary.insert(0, "split", split)
        rows.append(summary)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


if __name__ == "__main__":
    main()
