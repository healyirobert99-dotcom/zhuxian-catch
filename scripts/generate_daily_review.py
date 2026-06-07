from __future__ import annotations

import os
import sqlite3
import sys
import time
import json
import argparse
from pathlib import Path

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from ashare_a_plus.etf_map import format_etf_proxy, lookup_etf_code  # noqa: E402
from ashare_a_plus.sqlite_store import SQLiteStore  # noqa: E402

DB_PATH = BASE / "data" / "a_stock_selector.sqlite3"
REPORT_DIR = BASE / "reports" / "daily_review"
SNAPSHOT_DIR = REPORT_DIR / "snapshots"
LIFECYCLE_CACHE_DIR = REPORT_DIR / "lifecycle_cache"
MARKET_SNAPSHOT_DIR = REPORT_DIR / "market_snapshots"
INDEX_CACHE_DIR = REPORT_DIR / "index_cache"
LIFECYCLE_CACHE_VERSION = 1
MIN_DAILY_ROWS = 3000
MIN_AVG_AMOUNT_20D = 30_000_000
MIN_CONCEPT_DAILY_ROWS = 100
SUSPECT_MISS_RET_60D_THRESHOLD = 0.25
REQUIRED_INDICES = {"上证指数", "沪深300", "中证500", "创业板指"}


FUNDAMENTAL_TAG_TODO = [
    "ROE",
    "EPS",
    "营收同比增速",
    "归母净利润同比增速",
    "扣非净利润同比增速",
    "毛利率",
    "净利率",
    "经营现金流/净利润",
    "资产负债率",
    "PE历史分位",
    "PB历史分位",
    "行业估值分位",
]


GROWTH_INDUSTRY_KEYWORDS = (
    "半导体",
    "元器件",
    "软件",
    "通信",
    "互联网",
    "机器人",
    "电气设备",
    "医疗",
    "生物",
    "化学制药",
    "航空",
    "航天",
    "军工",
)


DEFENSIVE_INDUSTRY_KEYWORDS = (
    "银行",
    "路桥",
    "水力发电",
    "火力发电",
    "煤炭",
    "电力",
    "公用",
    "高速",
    "供气",
)

EXCLUDED_DYNAMIC_CONCEPT_KEYWORDS = (
    "昨日",
    "今日",
    "连板",
    "涨停",
    "跌停",
    "破板",
    "首板",
    "打板",
)


def main() -> None:
    args = parse_args()
    trade_date = args.trade_date or os.environ.get("TRADE_DATE", "20260601")
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    token = os.environ.get("TUSHARE_TOKEN")
    pro = build_tushare_client(token) if token else None

    if args.recent_days:
        dates = recent_cached_trade_dates(args.recent_days, args.end_date or trade_date)
        generate_reports_for_dates(dates, token, pro, use_lifecycle_cache=not args.no_lifecycle_cache)
        return

    generate_daily_report(trade_date, token, pro, use_lifecycle_cache=not args.no_lifecycle_cache)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate A-share mainline research daily reports.")
    parser.add_argument("--trade-date", help="Trade date such as 20260601. Defaults to TRADE_DATE or 20260601.")
    parser.add_argument("--recent-days", type=int, help="Generate reports for the latest N cached trading days.")
    parser.add_argument("--end-date", help="End date for --recent-days. Defaults to --trade-date/TRADE_DATE.")
    parser.add_argument("--no-lifecycle-cache", action="store_true", help="Recompute industry lifecycle instead of using local cache.")
    return parser.parse_args()


def generate_daily_report(
    trade_date: str,
    token: str | None,
    pro=None,
    *,
    all_prices: pd.DataFrame | None = None,
    history: pd.DataFrame | None = None,
    lifecycle_metrics: pd.DataFrame | None = None,
    use_lifecycle_cache: bool = True,
) -> list[Path]:
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    cache_stats = ensure_daily_cache(trade_date, token, pro)

    if all_prices is None or history is None:
        history, daily_basic = load_local_history(report_date)
        all_prices = build_indicators(history)
    else:
        daily_basic = load_daily_basic(report_date)

    snap = all_prices[all_prices["date"] == pd.Timestamp(report_date)].copy()
    snap = snap.merge(daily_basic, on="symbol", how="left")

    market = market_environment(snap, history, pro, trade_date)
    market["cache_stats"] = cache_stats
    market["previous"] = load_previous_market_snapshot(report_date)
    lifecycle = get_industry_lifecycle(all_prices, report_date, lifecycle_metrics, use_lifecycle_cache)
    lifecycle = enrich_industry_view(lifecycle, snap, market["score"])
    concept_cache_stats = ensure_concept_cache(trade_date, token, pro)
    concept_member_stats = ensure_concept_member_cache(trade_date, token)
    concept_lifecycle = build_concept_lifecycle(report_date, market["score"])
    concept_lifecycle = enrich_concept_industry_resonance(concept_lifecycle, lifecycle, report_date)
    market["concept_cache_stats"] = concept_cache_stats
    market["concept_member_stats"] = concept_member_stats
    stocks = stock_observation_pools(snap, lifecycle)
    yesterday_review = build_yesterday_review(report_date, lifecycle)
    recent_review = build_recent_lifecycle_review(report_date, lifecycle, market["score"])
    md = render_report(report_date, market, lifecycle, concept_lifecycle, stocks, yesterday_review, recent_review)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"a_share_daily_review_{report_date}.md"
    out_lifecycle = REPORT_DIR / f"a_share_daily_review_{report_date}_lifecycle.md"
    out.write_text(md, encoding="utf-8")
    out_lifecycle.write_text(md, encoding="utf-8")
    save_lifecycle_snapshot(report_date, lifecycle)
    save_market_snapshot(report_date, market)
    print(out)
    print(out_lifecycle)
    return [out, out_lifecycle]


def generate_reports_for_dates(dates: list[str], token: str | None, pro=None, *, use_lifecycle_cache: bool = True) -> None:
    if not dates:
        raise SystemExit("No cached trading days found for batch report generation.")
    start = (pd.Timestamp(min(dates)) - pd.DateOffset(months=18)).date().isoformat()
    end = max(dates)
    history = load_local_price_history(start, end)
    all_prices = build_indicators(history)
    missing_lifecycle_cache = [date for date in dates if not has_valid_lifecycle_cache(date)]
    lifecycle_metrics = compute_industry_lifecycle_metrics(all_prices) if (missing_lifecycle_cache or not use_lifecycle_cache) else None
    for report_date in sorted(dates):
        trade_date = pd.Timestamp(report_date).strftime("%Y%m%d")
        generate_daily_report(
            trade_date,
            token,
            pro,
            all_prices=all_prices,
            history=history,
            lifecycle_metrics=lifecycle_metrics,
            use_lifecycle_cache=use_lifecycle_cache,
        )


def build_tushare_client(token: str):
    import tushare as ts

    ts.set_token(token)
    return ts.pro_api()


def ensure_daily_cache(trade_date: str, token: str | None, pro=None) -> dict:
    store = SQLiteStore(DB_PATH)
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    daily_count = store.daily_row_count(report_date)
    daily_basic_count = store.daily_basic_row_count(report_date)
    need_daily = daily_count < MIN_DAILY_ROWS
    need_daily_basic = daily_basic_count < MIN_DAILY_ROWS
    if not need_daily and not need_daily_basic:
        return {
            "mode": "cache_hit",
            "daily_rows": daily_count,
            "daily_basic_rows": daily_basic_count,
            "synced_daily_rows": 0,
            "synced_daily_basic_rows": 0,
        }
    if not need_daily and need_daily_basic and not token:
        return {
            "mode": "cache_hit_missing_daily_basic",
            "daily_rows": daily_count,
            "daily_basic_rows": daily_basic_count,
            "synced_daily_rows": 0,
            "synced_daily_basic_rows": 0,
        }
    if not token:
        raise SystemExit(
            f"Local cache for {report_date} is incomplete: stock_daily={daily_count}, "
            f"stock_daily_basic={daily_basic_count}. Set TUSHARE_TOKEN once to sync the missing daily cache."
        )
    pro = pro or build_tushare_client(token)
    synced_daily_rows = 0
    synced_daily_basic_rows = 0
    if need_daily:
        daily = fetch_daily_for_store(pro, trade_date)
        store.upsert_daily(daily)
        synced_daily_rows = len(daily)
    if need_daily_basic:
        daily_basic = fetch_daily_basic_for_store(pro, trade_date)
        store.upsert_daily_basic(daily_basic)
        synced_daily_basic_rows = len(daily_basic)
    return {
        "mode": "incremental_sync",
        "daily_rows": store.daily_row_count(report_date),
        "daily_basic_rows": store.daily_basic_row_count(report_date),
        "synced_daily_rows": synced_daily_rows,
        "synced_daily_basic_rows": synced_daily_basic_rows,
    }


def recent_cached_trade_dates(limit: int, end_date: str) -> list[str]:
    if limit <= 0:
        raise SystemExit("--recent-days must be positive.")
    end = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            """
            select date
            from stock_daily
            where date <= ?
            group by date
            having count(*) >= ?
            order by date desc
            limit ?
            """,
            (end, MIN_DAILY_ROWS, limit),
        ).fetchall()
    return [row[0] for row in rows]


def fetch_daily_for_store(pro, trade_date: str) -> pd.DataFrame:
    daily = pro_query_with_retry(pro.daily, trade_date=trade_date)
    if daily.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "raw_open",
                "raw_high",
                "raw_low",
                "raw_close",
                "adj_factor",
                "pct_chg",
                "source",
            ]
        )
    df = daily.copy()
    df["symbol"] = df["ts_code"].str.slice(0, 6)
    df["date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "pct_chg"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce") * 1000
    df["raw_open"] = df["open"]
    df["raw_high"] = df["high"]
    df["raw_low"] = df["low"]
    df["raw_close"] = df["close"]
    df["adj_factor"] = 1.0
    df["source"] = "tushare_raw"
    return df[
        [
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "raw_open",
            "raw_high",
            "raw_low",
            "raw_close",
            "adj_factor",
            "pct_chg",
            "source",
        ]
    ].dropna(subset=["open", "high", "low", "close", "volume", "amount"])


def fetch_daily_basic_for_store(pro, trade_date: str) -> pd.DataFrame:
    daily_basic = pro_query_with_retry(
        pro.daily_basic,
        trade_date=trade_date,
        fields="ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,ps,total_mv,circ_mv",
    )
    if daily_basic.empty:
        return pd.DataFrame(
            columns=["symbol", "date", "turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"]
        )
    daily_basic = daily_basic.copy()
    daily_basic["symbol"] = daily_basic["ts_code"].str.slice(0, 6)
    daily_basic["date"] = pd.to_datetime(daily_basic["trade_date"]).dt.strftime("%Y-%m-%d")
    for col in ["turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"]:
        daily_basic[col] = pd.to_numeric(daily_basic[col], errors="coerce")
    return daily_basic[["symbol", "date", "turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"]]


def ensure_concept_cache(trade_date: str, token: str | None, pro=None) -> dict:
    store = SQLiteStore(DB_PATH)
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    count = store.concept_daily_row_count(report_date)
    if count >= MIN_CONCEPT_DAILY_ROWS:
        return {"mode": "cache_hit", "concept_daily_rows": count, "synced_concept_daily_rows": 0}
    if not token:
        return {"mode": "missing_no_token", "concept_daily_rows": count, "synced_concept_daily_rows": 0}
    pro = pro or build_tushare_client(token)
    concept_basic = fetch_concept_basic_for_store(pro)
    store.upsert_concept_basic(concept_basic)
    concept_daily = fetch_concept_daily_for_store(pro, trade_date)
    store.upsert_concept_daily(concept_daily)
    return {
        "mode": "incremental_sync",
        "concept_daily_rows": store.concept_daily_row_count(report_date),
        "synced_concept_daily_rows": len(concept_daily),
        "concept_basic_rows": len(concept_basic),
    }


def ensure_concept_member_cache(trade_date: str, token: str | None) -> dict:
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as con:
        count = con.execute("select count(*) from concept_member where trade_date = ?", (report_date,)).fetchone()[0]
    if count > 0:
        return {"mode": "cache_hit", "concept_member_rows": count, "synced_concept_member_rows": 0}
    if not token:
        return {"mode": "missing_no_token", "concept_member_rows": count, "synced_concept_member_rows": 0}
    try:
        sys.path.insert(0, str(BASE / "src"))
        from ashare_a_plus.tushare_sync import TushareSync, default_config

        sync = TushareSync(default_config(token=token, db_path=DB_PATH))
        synced = sync.sync_concept_members(pd.to_datetime(trade_date).strftime("%Y%m%d"))
    except Exception as exc:  # noqa: BLE001
        return {"mode": "sync_failed", "concept_member_rows": count, "synced_concept_member_rows": 0, "error": str(exc)}
    with sqlite3.connect(DB_PATH) as con:
        final_count = con.execute("select count(*) from concept_member where trade_date = ?", (report_date,)).fetchone()[0]
    return {"mode": "incremental_sync", "concept_member_rows": final_count, "synced_concept_member_rows": synced}


def fetch_concept_basic_for_store(pro) -> pd.DataFrame:
    try:
        df = pro_query_with_retry(pro.dc_index, idx_type="概念板块", fields="ts_code,name,idx_type")
    except Exception:
        return pd.DataFrame(columns=["ts_code", "name", "idx_type"])
    if df.empty:
        return pd.DataFrame(columns=["ts_code", "name", "idx_type"])
    df = ensure_columns(df.copy(), ["ts_code", "name", "idx_type"])
    df["idx_type"] = df["idx_type"].fillna("概念板块")
    df = df[df["idx_type"] == "概念板块"].copy()
    return df[["ts_code", "name", "idx_type"]].dropna(subset=["ts_code", "name"])


def fetch_concept_daily_for_store(pro, trade_date: str) -> pd.DataFrame:
    columns = ["ts_code", "trade_date", "pct_change", "turnover_rate", "up_num", "down_num", "total_mv", "leading", "leading_pct"]
    try:
        df = pro_query_with_retry(pro.dc_index, trade_date=trade_date, idx_type="概念板块")
    except Exception:
        return pd.DataFrame(columns=columns)
    return normalize_concept_daily_for_store(df, trade_date)


def normalize_concept_daily_for_store(df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    columns = ["ts_code", "trade_date", "pct_change", "turnover_rate", "up_num", "down_num", "total_mv", "leading", "leading_pct"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    frame = df.copy()
    rename_map = {
        "pct_chg": "pct_change",
        "turnover": "turnover_rate",
        "leading_stock": "leading",
        "leading_stock_name": "leading",
        "leading_change": "leading_pct",
        "leading_pct_chg": "leading_pct",
    }
    frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
    frame = ensure_columns(frame, columns)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"].fillna(trade_date)).dt.strftime("%Y-%m-%d")
    for col in ["pct_change", "turnover_rate", "total_mv", "leading_pct"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    for col in ["up_num", "down_num"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).astype(int)
    return frame[columns].dropna(subset=["ts_code", "trade_date"])


def ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def fetch_today(pro, trade_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pro_query_with_retry(pro.daily, trade_date=trade_date)
    daily["symbol"] = daily["ts_code"].str.slice(0, 6)
    daily["date"] = pd.to_datetime(daily["trade_date"])
    daily = daily.rename(columns={"vol": "volume"})
    daily["amount"] = daily["amount"] * 1000
    daily_basic = pro_query_with_retry(
        pro.daily_basic,
        trade_date=trade_date,
        fields="ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,ps,total_mv,circ_mv",
    )
    daily_basic["symbol"] = daily_basic["ts_code"].str.slice(0, 6)
    return daily, daily_basic


def pro_query_with_retry(func, retries: int = 3, sleep_seconds: int = 4, **kwargs) -> pd.DataFrame:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return func(**kwargs)
        except Exception as exc:
            last_error = exc
            if attempt == retries:
                break
            time.sleep(sleep_seconds * attempt)
    raise last_error


def load_local_history(report_date: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = (pd.Timestamp(report_date) - pd.DateOffset(months=18)).date().isoformat()
    history = load_local_price_history(start, report_date)
    daily_basic = load_daily_basic(report_date)
    return history, daily_basic


def load_local_price_history(start: str, end: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        history = pd.read_sql_query(
            """
            select d.symbol, d.date, d.open, d.high, d.low, d.close, d.volume, d.amount, d.pct_chg, b.name, b.industry
            from stock_daily d join stock_basic b on b.symbol=d.symbol
            where d.date >= ? and d.date <= ?
              and b.is_st=0 and b.is_delist_risk=0 and b.is_suspended=0
            order by d.symbol, d.date
            """,
            con,
            params=[start, end],
            dtype={"symbol": str},
        )
    history["date"] = pd.to_datetime(history["date"])
    return history


def load_daily_basic(report_date: str) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        daily_basic = pd.read_sql_query(
            """
            select symbol, turnover_rate, volume_ratio, pe, pb, ps, total_mv, circ_mv
            from stock_daily_basic
            where date = ?
            """,
            con,
            params=[report_date],
            dtype={"symbol": str},
        )
    return daily_basic


def load_concept_data(trade_date: str) -> pd.DataFrame:
    start = (pd.Timestamp(trade_date) - pd.DateOffset(months=18)).date().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        concepts = pd.read_sql_query(
            """
            select d.ts_code, d.trade_date, d.pct_change, d.turnover_rate, d.up_num, d.down_num,
                   d.total_mv, d.leading, d.leading_pct, b.name, b.idx_type
            from concept_daily d
            join concept_basic b on b.ts_code = d.ts_code
            where d.trade_date >= ? and d.trade_date <= ?
              and b.idx_type = '概念板块'
            order by d.ts_code, d.trade_date
            """,
            con,
            params=[start, trade_date],
        )
    if concepts.empty:
        return concepts
    concepts["date"] = pd.to_datetime(concepts["trade_date"])
    concepts["industry"] = concepts["name"].fillna(concepts["ts_code"])
    dynamic_pattern = "|".join(EXCLUDED_DYNAMIC_CONCEPT_KEYWORDS)
    concepts = concepts[~concepts["industry"].astype(str).str.contains(dynamic_pattern, regex=True)].copy()
    concepts["symbol"] = concepts["ts_code"]
    concepts["pct_change"] = pd.to_numeric(concepts["pct_change"], errors="coerce")
    concepts["daily_ret"] = concepts["pct_change"] / 100
    concepts = concepts.dropna(subset=["daily_ret", "date", "symbol", "industry"]).copy()
    concepts["turnover_rate"] = pd.to_numeric(concepts["turnover_rate"], errors="coerce")
    concepts["up_num"] = pd.to_numeric(concepts["up_num"], errors="coerce").fillna(0)
    concepts["down_num"] = pd.to_numeric(concepts["down_num"], errors="coerce").fillna(0)
    concepts["total_mv"] = pd.to_numeric(concepts["total_mv"], errors="coerce")
    concepts["amount"] = concepts["total_mv"].fillna(0) * 10000 * concepts["turnover_rate"].fillna(0) / 100
    concepts = concepts.sort_values(["symbol", "date"])
    grouped = concepts.groupby("symbol", group_keys=False)
    concepts["close"] = grouped["daily_ret"].transform(lambda s: (1 + s).cumprod())
    concepts["open"] = concepts["close"]
    concepts["high"] = concepts["close"]
    concepts["low"] = concepts["close"]
    concepts["volume"] = concepts["up_num"] + concepts["down_num"]
    concepts["trading_days"] = grouped.cumcount() + 1
    for window in [5, 10, 20, 30, 60, 120]:
        concepts[f"ret_{window}d"] = grouped["close"].pct_change(window)
        concepts[f"sma{window}"] = grouped["close"].transform(lambda s: s.rolling(window).mean())
    concepts["amount_ma20"] = grouped["amount"].transform(lambda s: s.rolling(20).mean())
    concepts["amount_ma50"] = grouped["amount"].transform(lambda s: s.rolling(50).mean())
    concepts["high_20_prev"] = grouped["close"].transform(lambda s: s.shift(1).rolling(20).max())
    concepts["low_20_prev"] = grouped["close"].transform(lambda s: s.shift(1).rolling(20).min())
    concepts["high_60_prev"] = grouped["close"].transform(lambda s: s.shift(1).rolling(60).max())
    concepts["low_60_prev"] = grouped["close"].transform(lambda s: s.shift(1).rolling(60).min())
    concepts["range20_prev"] = concepts["high_20_prev"] / concepts["low_20_prev"] - 1
    concepts["range60_prev"] = concepts["high_60_prev"] / concepts["low_60_prev"] - 1
    concepts["pct_chg_calc"] = concepts["daily_ret"]
    concepts["pct_chg"] = concepts["pct_change"]
    return concepts


def build_indicators(history: pd.DataFrame) -> pd.DataFrame:
    df = history.sort_values(["symbol", "date"]).copy()
    grouped = df.groupby("symbol", group_keys=False)
    df["trading_days"] = grouped.cumcount() + 1
    df["pct_chg_calc"] = grouped["close"].pct_change()
    for window in [5, 10, 20, 30, 60, 120]:
        df[f"ret_{window}d"] = grouped["close"].pct_change(window)
        df[f"sma{window}"] = grouped["close"].transform(lambda s: s.rolling(window).mean())
    df["amount_ma20"] = grouped["amount"].transform(lambda s: s.rolling(20).mean())
    df["amount_ma50"] = grouped["amount"].transform(lambda s: s.rolling(50).mean())
    df["high_20_prev"] = grouped["high"].transform(lambda s: s.shift(1).rolling(20).max())
    df["low_20_prev"] = grouped["low"].transform(lambda s: s.shift(1).rolling(20).min())
    df["high_60_prev"] = grouped["high"].transform(lambda s: s.shift(1).rolling(60).max())
    df["low_60_prev"] = grouped["low"].transform(lambda s: s.shift(1).rolling(60).min())
    df["range20_prev"] = df["high_20_prev"] / df["low_20_prev"] - 1
    df["range60_prev"] = df["high_60_prev"] / df["low_60_prev"] - 1
    df["pivot_20d"] = df["high_20_prev"]
    return df


def market_environment(snap: pd.DataFrame, history: pd.DataFrame, pro, trade_date: str) -> dict:
    up = (snap["pct_chg"] > 0).mean()
    pos20 = (snap["ret_20d"] > 0).mean()
    above20 = (snap["close"] > snap["sma20"]).mean()
    above60 = (snap["close"] > snap["sma60"]).mean()
    new_high20 = int((snap["close"] > snap["high_20_prev"]).sum())
    new_low20 = int((snap["close"] < snap["low_20_prev"]).sum())
    strong_up = int((snap["pct_chg"] >= 5).sum())
    strong_down = int((snap["pct_chg"] <= -5).sum())
    limit_up = int((snap["pct_chg"] >= 9.8).sum())
    limit_down = int((snap["pct_chg"] <= -9.8).sum())
    previous_dates = history[history["date"] < snap["date"].max()]["date"]
    prev_amount = history[history["date"] == previous_dates.max()]["amount"].sum() if not previous_dates.empty else np.nan
    amount_chg = snap["amount"].sum() / prev_amount - 1 if prev_amount and not pd.isna(prev_amount) else np.nan

    score = 0
    score += 25 if up >= 0.55 else 15 if up >= 0.45 else 5
    score += 20 if pos20 >= 0.55 else 10 if pos20 >= 0.45 else 3
    score += 20 if above60 >= 0.50 else 10 if above60 >= 0.40 else 3
    score += 15 if new_high20 > new_low20 else 5
    score += 10 if amount_chg > 0.05 else 5 if amount_chg > -0.05 else 2
    score += 10 if limit_up > max(limit_down * 2, 10) else 4
    label = "适合进攻" if score >= 75 else "结构性机会，可控仓参与主线" if score >= 55 else "偏观察，少追高" if score >= 40 else "不适合进攻"

    idx_rows = []
    index_status = "当日数据"
    if pro is not None:
        for name, code in {
            "上证指数": "000001.SH",
            "沪深300": "000300.SH",
            "中证500": "000905.SH",
            "创业板指": "399006.SZ",
        }.items():
            try:
                idx = pro.index_daily(ts_code=code, start_date="20250901", end_date=trade_date).sort_values("trade_date")
            except Exception:
                continue
            if idx.empty:
                continue
            idx["ma20"] = idx["close"].rolling(20).mean()
            idx["ma60"] = idx["close"].rolling(60).mean()
            last = idx.iloc[-1]
            idx_rows.append(
                {
                    "name": name,
                    "close": float(last["close"]),
                    "pct_chg": float(last["pct_chg"]),
                    "above20": bool(last["close"] > last["ma20"]),
                    "above60": bool(last["close"] > last["ma60"]),
                    "data_status": "当日数据",
                }
            )
    missing_indices = sorted(REQUIRED_INDICES - {row["name"] for row in idx_rows})
    if not missing_indices and idx_rows:
        save_index_cache(pd.to_datetime(trade_date).strftime("%Y-%m-%d"), idx_rows)
    else:
        cached = load_previous_index_cache(pd.to_datetime(trade_date).strftime("%Y-%m-%d"))
        if cached:
            idx_rows = [{**row, "data_status": "昨日缓存"} for row in cached]
            missing_indices = sorted(REQUIRED_INDICES - {row["name"] for row in idx_rows})
            index_status = "昨日缓存"
        elif not idx_rows:
            index_status = "缺失"
    return {
        "score": score,
        "label": label,
        "up": up,
        "pos20": pos20,
        "above20": above20,
        "above60": above60,
        "new_high20": new_high20,
        "new_low20": new_low20,
        "strong_up": strong_up,
        "strong_down": strong_down,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "amount_chg": amount_chg,
        "sample_count": len(snap),
        "indexes": idx_rows,
        "missing_indices": missing_indices,
        "index_status": index_status,
    }


def industry_lifecycle(prices: pd.DataFrame, report_date: str) -> dict[str, pd.DataFrame]:
    metrics = compute_industry_lifecycle_metrics(prices)
    return industry_lifecycle_from_metrics(metrics, report_date)


def build_concept_lifecycle(report_date: str, market_score: float) -> dict[str, pd.DataFrame]:
    concepts = load_concept_data(report_date)
    if concepts.empty:
        empty = pd.DataFrame()
        lifecycle = raw_lifecycle_views(empty)
        lifecycle["status"] = "missing"
        return lifecycle
    metrics = compute_concept_lifecycle_metrics(concepts)
    if metrics.empty or "date" not in metrics.columns:
        empty = pd.DataFrame()
        lifecycle = raw_lifecycle_views(empty)
        lifecycle["status"] = "insufficient"
        return lifecycle
    lifecycle = industry_lifecycle_from_metrics(metrics, report_date)
    enriched = enrich_industry_view(lifecycle, pd.DataFrame(), market_score)
    enriched["status"] = "ok"
    return enriched


def enrich_concept_industry_resonance(
    concept_lifecycle: dict[str, pd.DataFrame],
    industry_lifecycle: dict[str, pd.DataFrame],
    report_date: str,
) -> dict[str, pd.DataFrame]:
    mapping = load_concept_industry_mapping(report_date)
    industries = industry_lifecycle.get("all", pd.DataFrame()).copy()
    industry_grade = {}
    industry_driver = {}
    if not industries.empty and "industry" in industries.columns:
        industry_grade = industries.set_index("industry")["mainline_grade"].to_dict()
        if "driver_type" in industries.columns:
            industry_driver = industries.set_index("industry")["driver_type"].to_dict()
    enriched = dict(concept_lifecycle)
    for key, frame in list(enriched.items()):
        if not isinstance(frame, pd.DataFrame) or frame.empty or "industry" not in frame.columns:
            continue
        checked = frame.copy()
        checked["matched_industry"] = checked["industry"].map(mapping)
        checked["matched_industry_grade"] = checked["matched_industry"].map(industry_grade)
        checked["matched_industry_driver"] = checked["matched_industry"].map(industry_driver)
        checked["matched_industry_display"] = checked.apply(matched_industry_display, axis=1)
        checked["resonance_status"] = checked.apply(concept_resonance_status, axis=1)
        enriched[key] = checked
    return enriched


def load_concept_industry_mapping(report_date: str) -> dict[str, str]:
    with sqlite3.connect(DB_PATH) as con:
        latest = con.execute(
            "select max(trade_date) from concept_member where trade_date <= ?",
            (report_date,),
        ).fetchone()[0]
        if not latest:
            return {}
        rows = pd.read_sql_query(
            """
            select cb.name as concept, sb.industry, count(*) as member_count
            from concept_member cm
            join concept_basic cb on cb.ts_code = cm.ts_code
            left join stock_basic sb
              on sb.ts_code = cm.con_code
              or sb.symbol = substr(cm.con_code, 1, 6)
            where cm.trade_date = ?
              and cb.idx_type = '概念板块'
              and sb.industry is not null
            group by cb.name, sb.industry
            order by cb.name, member_count desc
            """,
            con,
            params=[latest],
        )
    if rows.empty:
        return {}
    top = rows.sort_values(["concept", "member_count"], ascending=[True, False]).groupby("concept", as_index=False).head(1)
    return dict(zip(top["concept"], top["industry"]))


def matched_industry_display(row: pd.Series) -> str:
    industry = row.get("matched_industry")
    if pd.isna(industry) or not industry:
        return "—"
    grade = row.get("matched_industry_grade")
    if pd.isna(grade) or not grade:
        return f"{industry} 未入选"
    return f"{industry} {compact_grade(str(grade))}"


def concept_resonance_status(row: pd.Series) -> str:
    industry = row.get("matched_industry")
    if pd.isna(industry) or not industry:
        return "— 无对应"
    grade = row.get("matched_industry_grade")
    if grade in ["A级主线", "B级主线", "C级观察"]:
        return "✅ 共振"
    if grade in ["退潮主线", "低频监控"]:
        return "❌ 背离"
    return "— 行业未入选"


def compute_concept_lifecycle_metrics(concepts: pd.DataFrame) -> pd.DataFrame:
    valid = concepts.dropna(subset=["industry", "daily_ret", "close"]).copy()
    valid = valid[(valid["trading_days"] >= 60) & valid["sma20"].notna()].copy()
    if valid.empty:
        return pd.DataFrame()
    width_denominator = valid["up_num"] + valid["down_num"]
    valid["concept_width"] = np.where(width_denominator > 0, valid["up_num"] / width_denominator, np.nan)
    grouped = valid.groupby("symbol", group_keys=False)
    valid["concept_new_high20"] = valid["close"] > grouped["close"].transform(lambda s: s.shift(1).rolling(20).max())
    valid["concept_new_low20"] = valid["close"] < grouped["close"].transform(lambda s: s.shift(1).rolling(20).min())
    rows = []
    for _, row in valid.iterrows():
        rows.append(
            {
                "date": row["date"],
                "industry": row["industry"],
                "stocks": int(row.get("volume", 1) or 1),
                "daily_ret": row["daily_ret"],
                "up_ratio": row["concept_width"],
                "above20": row["concept_width"],
                "above60": float(row["close"] > row["sma60"]) if pd.notna(row.get("sma60")) else np.nan,
                "amount": row["amount"],
                "new_high20": int(row["concept_new_high20"]),
                "new_low20": int(row["concept_new_low20"]),
            }
        )
    ts_frame = pd.DataFrame(rows).sort_values(["industry", "date"])
    market = ts_frame.groupby("date")["daily_ret"].median().rename("market_ret").reset_index()
    ts_frame = ts_frame.merge(market, on="date", how="left")
    grouped = ts_frame.groupby("industry", group_keys=False)
    ts_frame["ind_index"] = grouped["daily_ret"].transform(lambda s: (1 + s).cumprod())
    market["mkt_cum"] = (1 + market["market_ret"]).cumprod()
    ts_frame = ts_frame.merge(market[["date", "mkt_cum"]], on="date", how="left")
    grouped = ts_frame.groupby("industry", group_keys=False)
    for window in [5, 10, 20, 30, 60]:
        ts_frame[f"ret{window}"] = grouped["ind_index"].pct_change(window)
        ts_frame[f"mkt_ret{window}"] = ts_frame["mkt_cum"] / grouped["mkt_cum"].shift(window) - 1
        ts_frame[f"excess{window}"] = ts_frame[f"ret{window}"] - ts_frame[f"mkt_ret{window}"]
    for window in [5, 10, 20, 60]:
        ts_frame[f"amount{window}"] = grouped["amount"].transform(lambda s: s.rolling(window).mean())
    ts_frame["amount5_60"] = ts_frame["amount5"] / ts_frame["amount60"]
    ts_frame["amount10_60"] = ts_frame["amount10"] / ts_frame["amount60"]
    ts_frame["amount20_60"] = ts_frame["amount20"] / ts_frame["amount60"]
    ts_frame["daily_rank"] = ts_frame.groupby("date")["daily_ret"].rank(pct=True)
    ts_frame["top20_day"] = ts_frame["daily_rank"] >= 0.80
    ts_frame["outperform_day"] = ts_frame["daily_ret"] > ts_frame["market_ret"]
    for window in [10, 20, 30]:
        ts_frame[f"top20_days{window}"] = grouped["top20_day"].transform(lambda s: s.rolling(window).sum())
        ts_frame[f"outperform_days{window}"] = grouped["outperform_day"].transform(lambda s: s.rolling(window).sum())
    for window in [20, 30, 60]:
        rollmax = grouped["ind_index"].transform(lambda s: s.rolling(window).max())
        ts_frame[f"drawdown{window}"] = ts_frame["ind_index"] / rollmax - 1
    ts_frame["above20_chg5"] = grouped["above20"].transform(lambda s: s - s.shift(5))
    ts_frame["above60_chg10"] = grouped["above60"].transform(lambda s: s - s.shift(10))
    ts_frame["low_breadth10"] = grouped["above20"].transform(lambda s: (s < 0.20).rolling(10).sum())
    ts_frame["excess20_chg5"] = grouped["excess20"].transform(lambda s: s - s.shift(5))
    return ts_frame


def compute_industry_lifecycle_metrics(prices: pd.DataFrame) -> pd.DataFrame:
    valid = prices.dropna(subset=["industry", "pct_chg_calc"]).copy()
    valid = valid[
        (valid["trading_days"] >= 120)
        & (valid["amount_ma20"].fillna(0) >= MIN_AVG_AMOUNT_20D)
        & valid["sma20"].notna()
    ].copy()
    rows = []
    for (date, industry), frame in valid.groupby(["date", "industry"]):
        if len(frame) < 8:
            continue
        rows.append(
            {
                "date": date,
                "industry": industry,
                "stocks": len(frame),
                "daily_ret": frame["pct_chg_calc"].median(),
                "up_ratio": (frame["pct_chg_calc"] > 0).mean(),
                "above20": (frame["close"] > frame["sma20"]).mean(),
                "above60": (frame["close"] > frame["sma60"]).mean(),
                "amount": frame["amount"].sum(),
                "new_high20": (frame["close"] > frame["high_20_prev"]).sum(),
                "new_low20": (frame["close"] < frame["low_20_prev"]).sum(),
            }
        )
    ts_frame = pd.DataFrame(rows).sort_values(["industry", "date"])
    market = valid.groupby("date")["pct_chg_calc"].median().rename("market_ret").reset_index()
    ts_frame = ts_frame.merge(market, on="date", how="left")
    grouped = ts_frame.groupby("industry", group_keys=False)
    ts_frame["ind_index"] = grouped["daily_ret"].transform(lambda s: (1 + s).cumprod())
    market["mkt_cum"] = (1 + market["market_ret"]).cumprod()
    ts_frame = ts_frame.merge(market[["date", "mkt_cum"]], on="date", how="left")
    grouped = ts_frame.groupby("industry", group_keys=False)

    for window in [5, 10, 20, 30, 60]:
        ts_frame[f"ret{window}"] = grouped["ind_index"].pct_change(window)
        ts_frame[f"mkt_ret{window}"] = ts_frame["mkt_cum"] / grouped["mkt_cum"].shift(window) - 1
        ts_frame[f"excess{window}"] = ts_frame[f"ret{window}"] - ts_frame[f"mkt_ret{window}"]
    for window in [5, 10, 20, 60]:
        ts_frame[f"amount{window}"] = grouped["amount"].transform(lambda s: s.rolling(window).mean())
    ts_frame["amount5_60"] = ts_frame["amount5"] / ts_frame["amount60"]
    ts_frame["amount10_60"] = ts_frame["amount10"] / ts_frame["amount60"]
    ts_frame["amount20_60"] = ts_frame["amount20"] / ts_frame["amount60"]
    ts_frame["daily_rank"] = ts_frame.groupby("date")["daily_ret"].rank(pct=True)
    ts_frame["top20_day"] = ts_frame["daily_rank"] >= 0.80
    ts_frame["outperform_day"] = ts_frame["daily_ret"] > ts_frame["market_ret"]
    for window in [10, 20, 30]:
        ts_frame[f"top20_days{window}"] = grouped["top20_day"].transform(lambda s: s.rolling(window).sum())
        ts_frame[f"outperform_days{window}"] = grouped["outperform_day"].transform(lambda s: s.rolling(window).sum())
    for window in [20, 30, 60]:
        rollmax = grouped["ind_index"].transform(lambda s: s.rolling(window).max())
        ts_frame[f"drawdown{window}"] = ts_frame["ind_index"] / rollmax - 1
    ts_frame["above20_chg5"] = grouped["above20"].transform(lambda s: s - s.shift(5))
    ts_frame["above60_chg10"] = grouped["above60"].transform(lambda s: s - s.shift(10))
    ts_frame["low_breadth10"] = grouped["above20"].transform(lambda s: (s < 0.20).rolling(10).sum())
    ts_frame["excess20_chg5"] = grouped["excess20"].transform(lambda s: s - s.shift(5))
    return ts_frame


def industry_lifecycle_from_metrics(metrics: pd.DataFrame, report_date: str) -> dict[str, pd.DataFrame]:
    last = metrics[metrics["date"] == pd.Timestamp(report_date)].copy()
    rank_cols = [
        "ret5",
        "ret10",
        "ret20",
        "ret30",
        "ret60",
        "excess5",
        "excess10",
        "excess20",
        "excess30",
        "excess60",
        "above20",
        "amount5_60",
        "amount10_60",
        "amount20_60",
    ]
    if last.empty:
        return raw_lifecycle_views(last)
    for col in rank_cols:
        last[col + "_rank"] = last[col].rank(pct=True)

    last["warming_score_raw"] = (
        last["ret5_rank"] * 8
        + last["ret10_rank"] * 12
        + last["ret20_rank"] * 20
        + last["excess20_rank"] * 15
        + last["ret60_rank"] * 10
        + last["excess60_rank"] * 8
        + (last["outperform_days20"] / 20).clip(0, 1) * 10
        + last["amount5_60_rank"] * 10
        + last["above20_rank"] * 7
    )
    last["candidate_score_raw"] = (
        last["ret5_rank"] * 5
        + last["ret20_rank"] * 25
        + last["excess20_rank"] * 15
        + last["ret60_rank"] * 15
        + last["excess60_rank"] * 10
        + (last["outperform_days20"] / 20).clip(0, 1) * 15
        + (last["top20_days20"] / 20).clip(0, 1) * 10
        + last["above20_rank"] * 15
        + last["amount10_60_rank"] * 5
        + (last["drawdown20"] > -0.10).astype(int) * 5
    )
    last["confirmed_score_raw"] = (
        last["ret5_rank"] * 5
        + last["ret20_rank"] * 25
        + last["excess20_rank"] * 10
        + last["ret60_rank"] * 20
        + last["excess60_rank"] * 10
        + (last["outperform_days30"] / 30).clip(0, 1) * 15
        + last["above20_rank"] * 15
    )
    retreat_condition = (last["ret5"] < -0.08) | (last["drawdown20"] < -0.10) | (last["above20"] < 0.40)
    retreat_penalty = retreat_condition.astype(int) * 25
    mild_penalty = ((last["ret20"] < -0.03).astype(int) * 10) + ((last["drawdown20"] < -0.07).astype(int) * 8)
    last["confirmed_score"] = (last["confirmed_score_raw"] - retreat_penalty - mild_penalty).clip(lower=0)
    last["candidate_score"] = (last["candidate_score_raw"] - retreat_condition.astype(int) * 12).clip(lower=0)
    last["warming_score"] = last["warming_score_raw"]
    last["retreat_score"] = (
        (1 - last["ret5_rank"]) * 20
        + (last["drawdown20"].abs().clip(0, 0.25) / 0.25) * 25
        + (1 - last["above20_rank"]) * 20
        + (1 - last["amount5_60_rank"]) * 10
        + ((last["confirmed_score_raw"] > 65) | (last["candidate_score_raw"] > 65)).astype(int) * 25
    )
    last["in_retreat_risk"] = last["retreat_score"] >= 55
    last["driver_type_pre"] = last.apply(preliminary_driver_type, axis=1)
    last["speed_type"] = last.apply(mainline_speed_type, axis=1)
    last["catalyst_rhythm"] = last.apply(catalyst_rhythm, axis=1)
    last["stage"] = last.apply(classify_industry_stage, axis=1)
    last["stage_score_raw"] = last.apply(stage_score_raw_by_stage, axis=1)
    last["stage_score"] = last.apply(capped_stage_score, axis=1)
    last["risk_note"] = last.apply(industry_risk_note, axis=1)

    return raw_lifecycle_views(last)


def raw_lifecycle_views(last: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if last.empty:
        return {name: last.copy() for name in ["all", "overview", "warming", "candidate", "confirmed", "retreat"]}
    return {
        "all": last,
        "overview": last.sort_values(["stage_score", "warming_score"], ascending=False).head(18),
        "warming": last.sort_values("warming_score", ascending=False).head(10),
        "candidate": last[last["stage"].isin(["候选主线", "强确认延续", "弱确认延续", "防御修复延续"])].sort_values("candidate_score", ascending=False).head(10),
        "confirmed": last[last["stage"].isin(["强确认延续", "弱确认延续", "防御修复延续"])].sort_values("confirmed_score", ascending=False).head(10),
        "retreat": last[last["in_retreat_risk"] | last["stage"].isin(["确认后退潮", "退潮风险", "企稳重估", "低频监控"])].sort_values("retreat_score", ascending=False).head(12),
    }


def get_industry_lifecycle(
    all_prices: pd.DataFrame,
    report_date: str,
    lifecycle_metrics: pd.DataFrame | None = None,
    use_cache: bool = True,
) -> dict[str, pd.DataFrame]:
    if use_cache:
        cached = load_cached_industry_lifecycle(report_date)
        if cached is not None:
            return cached
    lifecycle = (
        industry_lifecycle_from_metrics(lifecycle_metrics, report_date)
        if lifecycle_metrics is not None
        else industry_lifecycle(all_prices, report_date)
    )
    if use_cache:
        save_cached_industry_lifecycle(report_date, lifecycle)
    return lifecycle


def lifecycle_cache_path(report_date: str) -> Path:
    return LIFECYCLE_CACHE_DIR / f"industry_lifecycle_{report_date}.json"


def save_cached_industry_lifecycle(report_date: str, lifecycle: dict[str, pd.DataFrame]) -> None:
    frame = lifecycle.get("all", pd.DataFrame()).copy()
    if frame.empty:
        return
    LIFECYCLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LIFECYCLE_CACHE_VERSION,
        "report_date": report_date,
        "all": json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False)),
    }
    lifecycle_cache_path(report_date).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cached_industry_lifecycle(report_date: str) -> dict[str, pd.DataFrame] | None:
    path = lifecycle_cache_path(report_date)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if payload.get("version") != LIFECYCLE_CACHE_VERSION or payload.get("report_date") != report_date:
        return None
    frame = pd.DataFrame(payload.get("all", []))
    if frame.empty:
        return None
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"])
    return raw_lifecycle_views(frame)


def has_valid_lifecycle_cache(report_date: str) -> bool:
    path = lifecycle_cache_path(report_date)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return payload.get("version") == LIFECYCLE_CACHE_VERSION and payload.get("report_date") == report_date and bool(payload.get("all"))


def classify_industry_stage(row: pd.Series) -> str:
    fast_theme = row.get("speed_type") == "快主题"
    fast_retreat = fast_theme and (
        row["ret5"] < -0.06 or row["drawdown20"] < -0.08 or row["drawdown60"] < -0.10 or row.get("above20_chg5", 0) < -0.20
    )
    retreat = row["ret5"] < -0.08 or row["drawdown20"] < -0.10 or row["above20"] < 0.40 or row["in_retreat_risk"] or fast_retreat
    hard_weak_60 = row["ret60"] < 0 and row.get("above60", 0) < 0.30
    weak_20 = row["ret20"] < 0 and row["above20"] < 0.40
    if row.get("low_breadth10", 0) >= 8 and row.get("excess20_chg5", 0) < 0 and row["amount5_60"] < 0.90:
        return "低频监控"
    if row["retreat_score"] >= 55 and row["ret5"] > 0 and row["ret10"] > 0 and row["above20"] >= 0.35 and row["drawdown20"] > -0.08 and row["amount5_60"] >= 0.90:
        return "企稳重估"
    if row["confirmed_score_raw"] >= 70 and retreat:
        return "确认后退潮"
    if row["retreat_score"] >= 60 and retreat:
        return "退潮风险"
    if hard_weak_60 or weak_20:
        if row["warming_score"] >= 65:
            return "弱势反弹"
        return "暂不观察"
    if row["confirmed_score"] >= 65 and row["ret20"] >= -0.03 and row["above20"] >= 0.50 and row["drawdown20"] >= -0.10:
        if row["ret20"] > 0 and row["ret60"] > 0 and row["above20"] >= 0.70 and row["drawdown20"] >= -0.06:
            return "强确认延续"
        if any(k in row["industry"] for k in DEFENSIVE_INDUSTRY_KEYWORDS):
            return "防御修复延续"
        if row["ret20"] <= 0 or row["ret60"] <= 0 or row["above20"] < 0.60:
            return "确认边缘"
        return "弱确认延续"
    if row.get("driver_type_pre") == "接力式催化型" and row["warming_score"] >= 68 and row["above20"] >= 0.45:
        return "接力催化初现"
    if row["candidate_score"] >= 65 and row["outperform_days20"] >= 10:
        return "候选主线"
    if row["warming_score"] >= 70 and (row["ret20"] < -0.03 or row["above20"] < 0.45):
        return "弱势反弹"
    if row["warming_score"] >= 70:
        return "升温观察"
    return "暂不观察"


def stage_score_raw_by_stage(row: pd.Series) -> float:
    if row["stage"] in ["强确认延续", "弱确认延续", "防御修复延续", "确认边缘", "确认后退潮"]:
        return float(row["confirmed_score"])
    if row["stage"] == "候选主线":
        return float(row["candidate_score"])
    if row["stage"] in ["升温观察", "弱势反弹", "企稳重估"]:
        return float(row["warming_score"])
    if row["stage"] in ["退潮风险", "低频监控"]:
        return float(row["retreat_score"])
    return float(max(row["warming_score"], row["candidate_score"], row["confirmed_score"]))


def capped_stage_score(row: pd.Series) -> float:
    score = float(row["stage_score_raw"])
    caps = {
        "候选主线": 85,
        "升温观察": 80,
        "接力催化初现": 80,
        "确认边缘": 75,
        "弱势反弹": 70,
        "企稳重估": 70,
        "确认后退潮": 70,
        "低频监控": 55,
    }
    if row["stage"] in ["退潮风险", "暂不观察"]:
        return min(score, 50)
    return min(score, caps.get(row["stage"], 100))


def industry_risk_note(row: pd.Series) -> str:
    notes = []
    if row["ret5"] < -0.08:
        notes.append("5日急跌")
    if row["drawdown20"] < -0.10:
        notes.append("近期大幅回撤")
    if row["above20"] < 0.40:
        notes.append("宽度不足")
    if row.get("above60", 1) < 0.30 and row["ret60"] < 0:
        notes.append("60日结构弱")
    if row["stage"] == "弱势反弹":
        notes.append("中期仍弱")
    if row["stage"] == "企稳重估":
        notes.append("退潮后企稳观察")
    if row["stage"] == "低频监控":
        notes.append("长期宽度塌缩")
    if row["stage"] == "确认边缘":
        notes.append("收益结构一般")
    if row["stage"] == "防御修复延续":
        notes.append("偏防御修复")
    if row["stage"] == "弱确认延续":
        notes.append("延续强度一般")
    if row["amount5_60"] < 0.85:
        notes.append("量能降温")
    return "、".join(notes) if notes else "无明显退潮"


def enrich_industry_view(lifecycle: dict[str, pd.DataFrame], snap: pd.DataFrame, market_score: float) -> dict[str, pd.DataFrame]:
    all_rows = lifecycle["all"].copy()
    valuation = industry_valuation_temperature(snap)
    all_rows["valuation_temp"] = all_rows["industry"].map(valuation).fillna("财务数据待补")
    all_rows["driver_type"] = all_rows.apply(mainline_driver_type, axis=1)
    all_rows["speed_type"] = all_rows.apply(mainline_speed_type, axis=1)
    all_rows["catalyst_rhythm"] = all_rows.apply(catalyst_rhythm, axis=1)
    all_rows = apply_driver_speed_guardrails(all_rows)
    all_rows["status_explanation"] = all_rows.apply(mainline_status_explanation, axis=1)
    all_rows["c_level_type"] = all_rows.apply(c_level_type, axis=1)
    all_rows["style_tags"] = all_rows.apply(industry_style_tags, axis=1)
    all_rows["mainline_grade"] = all_rows.apply(lambda row: mainline_grade(row, market_score), axis=1)
    all_rows["trend_state"] = all_rows.apply(industry_trend_state, axis=1)
    all_rows["leader_state"] = all_rows.apply(industry_leader_state, axis=1)
    all_rows["fundamental_support"] = all_rows.apply(industry_fundamental_support, axis=1)
    all_rows["short_term_state"] = all_rows.apply(lambda row: short_term_state(row, market_score), axis=1)
    all_rows["mainline_conclusion"] = all_rows.apply(mainline_conclusion, axis=1)
    all_rows["grade_order"] = all_rows["mainline_grade"].map({"A级主线": 0, "B级主线": 1, "C级观察": 2, "企稳重估": 3, "退潮主线": 4, "低频监控": 5, "暂不观察": 6}).fillna(7)

    visible = all_rows[all_rows["mainline_grade"] != "暂不观察"].sort_values(
        ["grade_order", "stage_score", "candidate_score"], ascending=[True, False, False]
    )
    enriched = dict(lifecycle)
    enriched["all"] = all_rows
    enriched["mainline_overview"] = visible.head(18)
    enriched["grade_a"] = visible[visible["mainline_grade"] == "A级主线"].head(6)
    enriched["grade_b"] = visible[visible["mainline_grade"] == "B级主线"].head(10)
    enriched["grade_c"] = visible[visible["mainline_grade"] == "C级观察"].head(10)
    enriched["stable_revaluation"] = visible[visible["mainline_grade"] == "企稳重估"].head(8)
    enriched["retreat_mainline"] = visible[visible["mainline_grade"].isin(["退潮主线", "低频监控"])].head(12)
    enriched["suspect_miss"] = suspect_miss_review(all_rows)
    return enriched


def industry_valuation_temperature(snap: pd.DataFrame) -> dict[str, str]:
    if snap.empty or "industry" not in snap.columns:
        return {}
    rows = {}
    for industry, frame in snap.dropna(subset=["industry"]).groupby("industry"):
        pe = pd.to_numeric(frame["pe"], errors="coerce").replace([np.inf, -np.inf], np.nan)
        pb = pd.to_numeric(frame["pb"], errors="coerce").replace([np.inf, -np.inf], np.nan)
        valid_pe = pe[pe > 0]
        med_pe = valid_pe.median()
        med_pb = pb[pb > 0].median()
        if pd.isna(med_pe) and pd.isna(med_pb):
            temp = "不可比"
        elif (pd.notna(med_pe) and med_pe > 80) or (pd.notna(med_pb) and med_pb > 8):
            temp = "极高"
        elif (pd.notna(med_pe) and med_pe > 45) or (pd.notna(med_pb) and med_pb > 5):
            temp = "偏高"
        elif (pd.notna(med_pe) and med_pe < 18) and (pd.isna(med_pb) or med_pb < 2):
            temp = "低"
        else:
            temp = "正常"
        rows[industry] = temp
    return rows


def suspect_miss_review(frame: pd.DataFrame) -> pd.DataFrame:
    excluded_grades = {"A级主线", "B级主线", "C级观察", "退潮主线", "低频监控"}
    candidates = frame[
        (frame["ret60"] > SUSPECT_MISS_RET_60D_THRESHOLD)
        & ~frame["mainline_grade"].isin(excluded_grades)
        & ~frame["stage"].isin(["退潮风险", "确认后退潮", "低频监控"])
    ].copy()
    if candidates.empty:
        return candidates
    candidates["miss_reason"] = candidates.apply(suspect_miss_reason, axis=1)
    candidates["review_conclusion"] = candidates.apply(suspect_miss_conclusion, axis=1)
    return candidates.sort_values(["ret60", "stage_score"], ascending=False).head(12)


def suspect_miss_reason(row: pd.Series) -> str:
    reasons = []
    if row["stocks"] < 8:
        reasons.append("行业样本不足")
    if row["above20"] < 0.40 or row["above60"] < 0.30:
        reasons.append("宽度不足")
    if row["above20"] < 0.40:
        reasons.append("站上MA20比例低")
    if row["above60"] < 0.30:
        reasons.append("站上MA60比例低")
    if row["ret5"] < 0 and row["ret20"] < row["ret60"] / 3:
        reasons.append("短期已转弱")
    if row["drawdown60"] < -0.10:
        reasons.append("60日峰值回撤较深")
    if row["ret60"] > SUSPECT_MISS_RET_60D_THRESHOLD and row["above20"] < 0.40:
        reasons.append("疑似少数权重股拉动")
    if row["ret60"] > SUSPECT_MISS_RET_60D_THRESHOLD and row["ret20"] > 0 and 0.40 <= row["above20"] <= 0.55:
        reasons.append("疑似细分主线被行业稀释")
    if row["ret5"] > 0.08 and row["ret20"] < 0.05:
        reasons.append("短期脉冲待核验")
    if row["amount5_60"] < 0.90:
        reasons.append("成交额未配合")
    if row["drawdown60"] < -0.08 and row["above20"] < 0.45:
        reasons.append("已接近退潮但未触发退潮榜")
    return "、".join(dict.fromkeys(reasons)) if reasons else "数据口径或阈值导致漏报"


def suspect_miss_conclusion(row: pd.Series) -> str:
    reasons = row.get("miss_reason", "")
    if "疑似细分主线被行业稀释" in reasons:
        return "疑似细分主线"
    if "短期已转弱" in reasons or "60日峰值回撤较深" in reasons:
        return "短期转弱，观察退潮"
    if "宽度不足" in reasons or "疑似少数权重股拉动" in reasons:
        return "宽度不足，暂不纳入"
    if "短期脉冲" in reasons:
        return "可能为局部脉冲"
    if "行业样本不足" in reasons or "数据口径" in reasons:
        return "数据口径待核验"
    return "需要人工复核"


def preliminary_driver_type(row: pd.Series) -> str:
    industry = str(row["industry"])
    stage = row.get("stage", "")
    if stage in ["弱势反弹", "确认边缘"] and (row["ret20"] < 0 or row["ret60"] < 0):
        return "超跌反弹型"
    if any(k in industry for k in ["创新药", "生物", "医疗保健"]) and row["ret20"] >= -0.03:
        return "接力式催化型"
    if any(k in industry for k in ["银行", "路桥", "水力发电", "火力发电", "新型电力", "供气供热", "水务"]):
        return "防御红利型"
    if "煤炭" in industry:
        return "防御红利型" if row["ret60"] >= 0 and row["above20"] >= 0.60 else "周期修复型"
    if any(k in industry for k in ["有色", "铜", "铝", "钢铁", "化工", "玻璃", "建材", "航运"]):
        return "周期修复型"
    if any(k in industry for k in ["半导体", "元器件", "软件", "通信", "机器人", "电气设备", "医疗", "生物", "创新药"]):
        return "景气驱动型"
    if any(k in industry for k in ["航空", "航天", "军工", "低空"]):
        return "政策催化型"
    if row["amount5_60"] > 1.35 and row["ret5"] > 0.05:
        return "资金抱团型"
    return "暂不明确"


def mainline_driver_type(row: pd.Series) -> str:
    return preliminary_driver_type(row)


def mainline_speed_type(row: pd.Series) -> str:
    industry = str(row["industry"])
    driver = row.get("driver_type_pre") or preliminary_driver_type(row)
    if driver in ["政策催化型"] or any(k in industry for k in ["半导体", "元器件", "机器人", "低空", "航天", "AI"]):
        return "快主题"
    if driver == "接力式催化型" or any(k in industry for k in ["创新药", "生物", "医疗保健"]):
        return "中速趋势"
    if driver in ["防御红利型", "周期修复型"] or any(k in industry for k in ["煤炭", "银行", "路桥", "水力发电", "火力发电"]):
        return "慢趋势"
    return "中速趋势"


def apply_driver_speed_guardrails(frame: pd.DataFrame) -> pd.DataFrame:
    checked = frame.copy()
    invalid_oversold_slow = (checked["driver_type"] == "超跌反弹型") & (checked["speed_type"] == "慢趋势")
    if invalid_oversold_slow.any():
        checked.loc[invalid_oversold_slow, "stage"] = "暂不观察"
        checked.loc[invalid_oversold_slow, "stage_score"] = checked.loc[invalid_oversold_slow, "stage_score"].clip(upper=45)
        checked.loc[invalid_oversold_slow, "risk_note"] = checked.loc[invalid_oversold_slow].apply(
            lambda row: append_note(row.get("risk_note", ""), "反弹弹性不足"), axis=1
        )
    invalid_policy_slow = (checked["driver_type"] == "政策催化型") & (checked["speed_type"] == "慢趋势")
    if invalid_policy_slow.any():
        checked.loc[invalid_policy_slow, "stage"] = "暂不观察"
        checked.loc[invalid_policy_slow, "stage_score"] = checked.loc[invalid_policy_slow, "stage_score"].clip(upper=45)
        checked.loc[invalid_policy_slow, "risk_note"] = checked.loc[invalid_policy_slow].apply(
            lambda row: append_note(row.get("risk_note", ""), "政策催化速度不匹配"), axis=1
        )
    slow_growth = (checked["driver_type"] == "景气驱动型") & (checked["speed_type"] == "慢趋势")
    if slow_growth.any():
        checked.loc[slow_growth, "risk_note"] = checked.loc[slow_growth].apply(
            lambda row: append_note(row.get("risk_note", ""), "景气不足"), axis=1
        )
    return checked


def append_note(existing: str, note: str) -> str:
    parts = [part for part in str(existing).split("、") if part and part != "无明显退潮"]
    parts.append(note)
    return "、".join(dict.fromkeys(parts)) if parts else note


def catalyst_rhythm(row: pd.Series) -> str:
    driver = row.get("driver_type_pre") or preliminary_driver_type(row)
    stage = row.get("stage", "")
    if driver == "接力式催化型":
        return "接力延续" if row["ret20"] >= -0.03 and row["above20"] >= 0.45 else "催化衰减"
    if driver == "政策催化型":
        if row["ret5"] > 0.08 and row["amount5_60"] > 1.20:
            return "密集爆发"
        if row["drawdown20"] < -0.08:
            return "催化衰减"
        return "单点脉冲"
    if row["ret5"] > 0.12 and row["drawdown20"] > -0.03:
        return "情绪顶点"
    if stage in ["确认后退潮", "退潮风险"]:
        return "催化衰减"
    return "暂不明确"


def industry_style_tags(row: pd.Series) -> str:
    industry = str(row["industry"])
    tags = []
    if row["driver_type"] == "防御红利型":
        tags.extend(["高股息红利", "低波动防御"])
    if "银行" in industry:
        tags.append("低PB防御")
    if any(k in industry for k in ["路桥", "水力发电", "火力发电", "新型电力"]):
        tags.append("稳定现金流")
    if row["driver_type"] == "景气驱动型":
        tags.append("科技成长")
    if any(k in industry for k in ["半导体", "元器件"]):
        tags.append("国产替代")
    if row["driver_type"] == "周期修复型":
        tags.append("资源/周期")
    if row["driver_type"] == "超跌反弹型":
        tags.append("弱势修复")
    return " / ".join(dict.fromkeys(tags)) if tags else "待映射"


def mainline_status_explanation(row: pd.Series) -> str:
    if row["stage"] in ["确认后退潮", "退潮风险"]:
        return "确认后退潮"
    if row["stage"] == "企稳重估":
        return "峰值回撤后修复"
    if row["stage"] == "接力催化初现":
        return "接力催化初现"
    if row.get("driver_type") == "接力式催化型" and row["stage"] in ["强确认延续", "弱确认延续", "候选主线"]:
        return "接力催化延续"
    if row["stage"] in ["弱势反弹", "确认边缘"]:
        return "弱势反弹"
    if row["drawdown60"] < -0.10 and row["ret5"] > 0:
        return "峰值回撤后修复"
    if row["drawdown20"] < -0.06 and row["above20"] >= 0.50:
        return "高位震荡"
    if row["stage"] in ["强确认延续", "弱确认延续", "防御修复延续"]:
        return "真延续"
    if row["stage"] == "升温观察":
        return "重新升温" if row["drawdown60"] < -0.08 else "早期升温"
    return "暂不明确"


def c_level_type(row: pd.Series) -> str:
    if row["stage"] == "接力催化初现":
        return "接力催化初现 C级"
    if row["stage"] == "升温观察":
        return "早期升温 C级"
    if row["stage"] == "确认边缘" and row["ret20"] >= -0.03 and row["above20"] >= 0.45:
        return "低位修复 C级"
    if row["ret5"] > 0.05 and row["amount5_60"] > 1.30 and row["ret20"] < 0:
        return "单点脉冲 C级"
    if row["stage"] in ["弱势反弹", "确认边缘"]:
        return "弱势反弹 C级"
    return "观察 C级"


def mainline_grade(row: pd.Series, market_score: float) -> str:
    if row["stage"] == "低频监控":
        return "低频监控"
    if row["stage"] == "企稳重估":
        return "企稳重估"
    if row["stage"] in ["确认后退潮", "退潮风险"] or row["in_retreat_risk"]:
        return "退潮主线"
    if (
        market_score >= 55
        and row["stage"] == "强确认延续"
        and row["ret20"] > 0
        and row["ret60"] > 0
        and row["above20"] >= 0.70
        and row["drawdown20"] >= -0.08
        and row["drawdown60"] >= -0.10
    ):
        return "A级主线"
    if row["stage"] in ["强确认延续", "弱确认延续", "防御修复延续", "候选主线"]:
        if row["ret60"] < 0 and row.get("above60", 0) < 0.30:
            return "C级观察"
        if row["ret20"] < 0 and row["above20"] < 0.40:
            return "C级观察"
        if row["driver_type"] == "防御红利型" and row["ret20"] < 0 and row["ret60"] < 0:
            return "C级观察"
        if row["drawdown60"] < -0.12 and row["ret20"] <= 0:
            return "C级观察"
        return "B级主线"
    if row["stage"] in ["升温观察", "接力催化初现", "确认边缘", "弱势反弹"]:
        return "C级观察"
    return "暂不观察"


def industry_trend_state(row: pd.Series) -> str:
    if row["stage"] in ["确认后退潮", "退潮风险"] or row["in_retreat_risk"]:
        return "退潮破位"
    if row["ret5"] > 0.08 and row["ret20"] > 0.15:
        return "偏离过大"
    if row["ret5"] < 0 and row["drawdown20"] < -0.05:
        return "回踩观察"
    if row["above20"] >= 0.55 and row["ret20"] >= -0.03:
        return "趋势良好"
    return "回踩观察"


def industry_leader_state(row: pd.Series) -> str:
    if row["stage"] in ["确认后退潮", "退潮风险"] or row["in_retreat_risk"]:
        return "龙头转弱/需防退潮"
    if row["top20_days20"] >= 6 and row["outperform_days20"] >= 12:
        return "龙头与扩散较好"
    if row["outperform_days20"] >= 10:
        return "有持续跑赢"
    return "待龙头确认"


def industry_fundamental_support(row: pd.Series) -> str:
    industry = str(row["industry"])
    valuation = row.get("valuation_temp", "财务数据待补")
    if "银行" in industry:
        return "盈利稳定但成长性弱"
    if any(k in industry for k in ["证券", "保险"]):
        return "金融周期属性，盈利弹性待观察"
    if any(k in industry for k in ["煤炭", "钢铁", "有色", "铜", "铝"]):
        return "周期属性强，盈利持续性需观察"
    if any(k in industry for k in ["半导体", "元器件", "软件", "通信", "机器人", "航空", "航天"]):
        return "景气改善待验证"
    if any(k in industry for k in ["火力发电", "水力发电", "新型电力", "路桥", "港口", "供气供热", "水务"]):
        return "偏公用/防御，现金流待核验"
    if valuation in ["极高", "不可比"]:
        return "估值或盈利质量需排雷"
    if row["stage"] in ["弱势反弹", "确认边缘"]:
        return "基本面支撑待验证"
    return "财务数据待补"


def short_term_state(row: pd.Series, market_score: float) -> str:
    if row["drawdown20"] >= -0.005 and row["ret20"] > 0 and row["above20"] >= 0.60:
        if market_score < 55 and row.get("driver_type") == "防御红利型":
            return "短期强势近高位，弱市抱团特征"
        return "短期强势近高位"
    return ""


def mainline_conclusion(row: pd.Series) -> str:
    driver = row.get("driver_type", "暂不明确")
    short_state = row.get("short_term_state", "")
    if short_state:
        return f"{short_state}；若市场转强，需观察是否被成长方向分流"
    if driver == "超跌反弹型" and row.get("speed_type") == "慢趋势":
        return "超跌反弹型与慢趋势不匹配，暂不纳入C级观察"
    if driver == "防御红利型" and row["mainline_grade"] in ["A级主线", "B级主线"]:
        return "弱市适配，可作为防御主线研究；若市场转强，观察是否被成长方向分流"
    if driver == "景气驱动型" and row["mainline_grade"] == "退潮主线":
        return "产业方向可长期跟踪，但当前短期退潮，等待重新企稳"
    if driver == "政策催化型" and row["mainline_grade"] == "C级观察":
        return "短期升温，需观察是否从主题催化扩散为中级别主线"
    if row["mainline_grade"] == "企稳重估":
        return "退潮后出现修复迹象，先重估稳定性，不直接恢复候选"
    if row["mainline_grade"] == "低频监控":
        return "宽度长期不足，移出核心日报，仅低频监控"
    if row["mainline_grade"] == "A级主线":
        return "主线确认，可重点跟踪但不追高"
    if row["mainline_grade"] == "B级主线":
        return "有主线雏形，优先等回踩或扩散确认"
    if row["mainline_grade"] == "C级观察":
        return "观察是否由反弹升级为持续主线"
    if row["mainline_grade"] == "退潮主线":
        return "风险监控，等待企稳后再研究"
    return "暂不观察"


def warming_type(row: pd.Series) -> str:
    if row["ret20"] < -0.05 and row["ret60"] < -0.08:
        return "弱势反弹"
    if row["ret20"] < 0 and row["above20"] < 0.50:
        return "低位修复"
    if any(k in row["industry"] for k in DEFENSIVE_INDUSTRY_KEYWORDS):
        return "防御修复"
    if row["candidate_score"] >= 65:
        return "延续升温"
    return "强势升温"


def stock_observation_pools(snap: pd.DataFrame, lifecycle: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    industries = lifecycle["all"].set_index("industry")
    allowed_carriers = allowed_carrier_industries(lifecycle)
    active_industries = industries[
        industries["stage"].isin(
            [
                "升温观察",
                "候选主线",
                "强确认延续",
                "弱确认延续",
                "防御修复延续",
                "确认边缘",
                "企稳重估",
                "确认后退潮",
                "退潮风险",
                "弱势反弹",
            ]
        )
    ]
    frame = snap[snap["industry"].isin(active_industries.index)].copy()
    if frame.empty:
        empty = pd.DataFrame()
        return {
            "steady": empty,
            "rebound": empty,
            "growth": empty,
            "risk": empty,
            "etf": etf_proxy_table(lifecycle),
            "core": empty,
            "elastic": empty,
        }
    frame["industry_stage"] = frame["industry"].map(active_industries["stage"])
    frame["mainline_grade"] = frame["industry"].map(active_industries["mainline_grade"]).fillna("暂不观察")
    frame["driver_type"] = frame["industry"].map(active_industries["driver_type"]).fillna("暂不明确")
    frame["industry_score"] = frame["industry"].map(active_industries["stage_score"]).fillna(0)
    frame["industry_in_retreat"] = frame["industry"].map(active_industries["in_retreat_risk"]).fillna(False)
    frame["industry_width"] = frame["industry"].map(active_industries["above20"]).fillna(np.nan)
    frame["carrier_allowed"] = frame["industry"].isin(allowed_carriers)
    frame["rs20_in_ind"] = frame.groupby("industry")["ret_20d"].rank(pct=True)
    frame["rs60_in_ind"] = frame.groupby("industry")["ret_60d"].rank(pct=True)
    frame["pivot_distance_pct"] = frame["close"] / frame["pivot_20d"] - 1
    frame["breakout"] = frame["close"] > frame["pivot_20d"]
    frame["low_vol_contract"] = frame["range20_prev"] < frame["range60_prev"] * 0.75
    frame["breakout_amount_multiple"] = frame["amount"] / frame["amount_ma50"]
    frame["overheat"] = (frame["ret_20d"] > 0.30) | (frame["pivot_distance_pct"] > 0.05)
    frame["severe_overheat"] = frame["ret_20d"] > 0.80
    frame["wait_pullback"] = frame["breakout"] & (frame["pivot_distance_pct"] > 0.05)
    frame["valuation_flag"] = frame.apply(valuation_flag, axis=1)
    frame["valuation_temp"] = frame.apply(stock_valuation_temperature, axis=1)
    frame["fundamental_tag"] = frame.apply(fundamental_tag, axis=1)
    frame["risk_tags"] = frame.apply(stock_risk_tags, axis=1)
    frame["risk_penalty"] = frame.apply(stock_risk_penalty, axis=1)
    frame["impact_on_mainline"] = frame.apply(stock_impact_on_mainline, axis=1)
    frame["technical_score"] = (
        (frame["close"] > frame["sma20"]).fillna(False).astype(int) * 5
        + frame["low_vol_contract"].fillna(False).astype(int) * 4
        + frame["breakout"].fillna(False).astype(int) * 3
        + (frame["pivot_distance_pct"].between(-0.03, 0.05)).fillna(False).astype(int) * 3
    )
    frame["stock_score_raw"] = (
        frame["industry_score"] * 0.25
        + frame["rs20_in_ind"].fillna(0) * 20
        + frame["rs60_in_ind"].fillna(0) * 8
        + frame["technical_score"]
    )
    frame["stock_score"] = (frame["stock_score_raw"] - frame["risk_penalty"]).clip(lower=0)
    frame["action"] = frame.apply(stock_action, axis=1)
    frame["pool"] = frame.apply(stock_pool, axis=1)
    frame["trend_state"] = frame.apply(stock_trend_state, axis=1)
    frame["market_cap_tier"] = frame["total_mv"].apply(market_cap_tier)
    frame["role_tag"] = frame.apply(stock_role_tag, axis=1)
    eligible = frame[
        (frame["amount"] >= 20_000_000)
        & (frame["close"] > frame["sma20"])
        & ~frame["name"].fillna("").str.contains("ST", case=False, regex=False)
    ].copy()
    carrier_source = eligible[
        eligible["carrier_allowed"]
        & eligible["mainline_grade"].isin(["A级主线", "B级主线", "C级观察", "企稳重估"])
    ].copy()
    core = (
        carrier_source[
            ~carrier_source["pool"].eq("过热/风险复核池")
            & carrier_source["market_cap_tier"].isin(["超大市值", "大型"])
        ]
        .sort_values(["mainline_grade", "total_mv", "stock_score"], ascending=[True, False, False])
        .groupby("industry", group_keys=False)
        .head(2)
        .sort_values(["mainline_grade", "stock_score"], ascending=[True, False])
        .head(16)
    )
    elastic = (
        carrier_source[
            carrier_source["market_cap_tier"].isin(["中型", "小型"])
            & (
                carrier_source["ret_20d"].gt(0.12)
                | carrier_source["pool"].eq("弹性成长观察池")
                | carrier_source["driver_type"].isin(["景气驱动型", "政策催化型", "周期修复型"])
            )
        ]
        .sort_values(["mainline_grade", "rs20_in_ind", "ret_20d"], ascending=[True, False, False])
        .groupby("industry", group_keys=False)
        .head(2)
        .head(16)
    )
    risk = frame[
        (frame["pool"] == "过热/风险复核池")
        | frame["fundamental_tag"].isin(["亏损/不可比", "估值极高"])
        | frame["valuation_temp"].isin(["极高", "不可比"])
    ].sort_values(["risk_penalty", "stock_score_raw"], ascending=False).head(20)
    return {
        "steady": eligible[eligible["pool"] == "稳健中军观察池"].sort_values("stock_score", ascending=False).head(15),
        "rebound": eligible[eligible["pool"] == "低位修复/弱势反弹观察池"].sort_values("stock_score", ascending=False).head(15),
        "growth": eligible[eligible["pool"] == "弹性成长观察池"].sort_values("stock_score", ascending=False).head(15),
        "risk": risk,
        "etf": etf_proxy_table(lifecycle),
        "core": core,
        "elastic": elastic,
    }


def allowed_carrier_industries(lifecycle: dict[str, pd.DataFrame]) -> set[str]:
    allowed = set()
    for key in ["grade_a", "grade_b", "grade_c", "stable_revaluation"]:
        frame = lifecycle.get(key, pd.DataFrame())
        if not frame.empty and "industry" in frame.columns:
            allowed.update(frame["industry"].dropna().astype(str).tolist())
    return allowed


def valuation_flag(row: pd.Series) -> str:
    flags = []
    pe = row.get("pe")
    pb = row.get("pb")
    if pd.isna(pe):
        flags.append("盈利不可比/缺失")
    elif pe > 200:
        flags.append("PE极端")
    elif pe > 100:
        flags.append("PE>100")
    elif pe > 80:
        flags.append("PE偏高")
    elif pe > 45:
        flags.append("PE偏高")
    if pd.notna(pb):
        if pb > 10:
            flags.append("PB极高")
        elif pb > 8:
            flags.append("PB偏高")
        elif pb > 5:
            flags.append("PB偏高")
    return "、".join(flags) if flags else "估值初筛正常"


def stock_valuation_temperature(row: pd.Series) -> str:
    pe = row.get("pe")
    pb = row.get("pb")
    if pd.isna(pe) or pe <= 0:
        return "不可比"
    if pe > 200 or (pd.notna(pb) and pb > 10):
        return "极高"
    if pe > 45 or (pd.notna(pb) and pb > 5):
        return "偏高"
    if pe < 18 and (pd.isna(pb) or pb < 2):
        return "低"
    return "正常"


def fundamental_tag(row: pd.Series) -> str:
    temp = row.get("valuation_temp", "财务数据待补")
    if temp == "不可比":
        return "亏损/不可比"
    if temp == "极高":
        return "估值极高"
    if temp == "偏高":
        return "估值偏高"
    if is_growth_industry(row.get("industry", "")):
        return "成长兑现待核验"
    if any(k in str(row.get("industry", "")) for k in ["煤炭", "钢铁", "有色", "化工"]):
        return "周期位置待核验"
    return "质量较稳/财务待补"


def stock_risk_tags(row: pd.Series) -> str:
    tags = []
    if row["industry_in_retreat"] or row["industry_stage"] in ["确认后退潮", "退潮风险"]:
        tags.append("行业退潮")
    if row["pct_chg"] <= -8:
        tags.append("当日大跌")
    elif row["pct_chg"] <= -5:
        tags.append("单日风险")
    if row["pct_chg"] >= 9.8:
        tags.append("涨停不可追")
    if row["ret_20d"] > 0.80:
        tags.append("严重过热")
    elif row["ret_20d"] > 0.50:
        tags.append("过热")
    elif row["ret_20d"] > 0.30:
        tags.append("涨幅偏大")
    if row["pivot_distance_pct"] > 0.05:
        tags.append("偏离pivot")
    elif row["pivot_distance_pct"] > 0.04:
        tags.append("接近追高阈值")
    elif -0.03 <= row["pivot_distance_pct"] <= 0.03:
        tags.append("接近pivot")
    if row.get("breakout_amount_multiple", np.nan) > 2.5:
        tags.append("放量偏高")
    elif row.get("breakout_amount_multiple", np.nan) > 2.0:
        tags.append("放量较高")
    if row["valuation_flag"] != "估值初筛正常":
        tags.append(row["valuation_flag"])
    if not tags:
        tags.append("估值合理")
    return "、".join(tags)


def stock_impact_on_mainline(row: pd.Series) -> str:
    retreat_grade = row.get("mainline_grade") in ["退潮主线", "低频监控"]
    retreat_stage = row.get("industry_stage") in ["确认后退潮", "退潮风险", "低频监控"]
    if row.get("industry_in_retreat") or retreat_grade or retreat_stage:
        return "行业退潮确认"
    valuation_extreme = row.get("valuation_temp") in ["极高", "不可比"] or "PE极端" in str(row.get("risk_tags", "")) or "PB极高" in str(row.get("risk_tags", ""))
    c_observation = row.get("mainline_grade") == "C级观察"
    width = row.get("industry_width", np.nan)
    if c_observation and row.get("overheat", False) and valuation_extreme:
        return "个股透支主线修复"
    if c_observation and row.get("ret_20d", 0) > 0.50 and (pd.isna(width) or width < 0.55):
        return "妖股扰动，削弱行业宽度可信度"
    if row.get("mainline_grade") in ["A级主线", "B级主线", "C级观察", "企稳重估"] and pd.notna(width) and width < 0.40:
        return "行业内部分化严重"
    if row.get("ret_20d", 0) > 0.50 and row.get("mainline_grade") in ["A级主线", "B级主线", "C级观察"]:
        return "细分方向过热"
    if row.get("carrier_allowed") and pd.notna(width) and width >= 0.55:
        return "不影响主线，仅个股风险"
    return "主线质量待复核"


def stock_risk_penalty(row: pd.Series) -> float:
    penalty = 0
    if row["industry_in_retreat"] or row["industry_stage"] in ["确认后退潮", "退潮风险"]:
        penalty += 25
    if row["pct_chg"] <= -8:
        penalty += 35
    elif row["pct_chg"] <= -5:
        penalty += 18
    if row["pct_chg"] >= 9.8:
        penalty += 18
    if row["ret_20d"] > 0.80:
        penalty += 40
    elif row["ret_20d"] > 0.50:
        penalty += 25
    elif row["ret_20d"] > 0.30:
        penalty += 12
    if row["pivot_distance_pct"] > 0.05:
        penalty += 12
    if pd.isna(row.get("pe")):
        penalty += 8
    elif row["pe"] > 200:
        penalty += 35
    elif row["pe"] > 100:
        penalty += 25
    elif row["pe"] > 80:
        penalty += 12
    if pd.notna(row.get("pb")):
        if row["pb"] > 10:
            penalty += 20
        elif row["pb"] > 8:
            penalty += 12
        elif row["pb"] > 5:
            penalty += 5
    return penalty


def is_growth_industry(industry: str) -> bool:
    return any(keyword in str(industry) for keyword in GROWTH_INDUSTRY_KEYWORDS)


def stock_trend_state(row: pd.Series) -> str:
    if row["industry_stage"] in ["确认后退潮", "退潮风险"] or row["industry_in_retreat"] or row["close"] < row["sma20"]:
        return "退潮破位"
    if row["ret_20d"] > 0.30 or row["pivot_distance_pct"] > 0.05:
        return "偏离过大"
    if row["pivot_distance_pct"] < -0.03 or row["pct_chg"] < 0:
        return "回踩观察"
    return "趋势良好"


def market_cap_tier(total_mv: float) -> str:
    if pd.isna(total_mv):
        return "未知"
    if total_mv >= 30_000_000:
        return "超大市值"
    if total_mv >= 5_000_000:
        return "大型"
    if total_mv >= 1_000_000:
        return "中型"
    return "小型"


def stock_role_tag(row: pd.Series) -> str:
    if row.get("market_cap_tier") in ["超大市值", "大型"]:
        return "中军/权重载体"
    if is_growth_industry(row.get("industry", "")):
        return "成长弹性"
    if row.get("ret_20d", 0) > 0.20:
        return "价格弹性"
    return "修复弹性"


def elastic_source(row: pd.Series) -> str:
    industry = str(row.get("industry", ""))
    driver = row.get("driver_type", "暂不明确")
    if driver == "政策催化型":
        return "政策弹性"
    if driver == "景气驱动型":
        if any(k in industry for k in ["半导体", "元器件", "软件", "通信"]):
            return "国产替代弹性"
        return "业绩拐点"
    if driver == "周期修复型":
        return "周期修复弹性"
    if row.get("market_cap_tier") == "小型":
        return "小市值高弹性"
    if row.get("ret_20d", 0) > 0.20:
        return "价格弹性"
    return "题材弹性"


def etf_proxy_table(lifecycle: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    allowed = allowed_carrier_industries(lifecycle)
    frame = lifecycle.get("mainline_overview", pd.DataFrame())
    if not frame.empty:
        frame = frame[frame["industry"].isin(allowed)].head(16)
    for _, row in frame.iterrows():
        industry = row["industry"]
        etf_code = lookup_etf_code(industry) or ""
        proxy = format_etf_proxy(industry)
        # format_etf_proxy 已包含代码和名称，无需再拼接
        proxy_display = proxy if etf_code else proxy
        if row["mainline_grade"] == "A级主线":
            scene = "主线跟踪/优先研究行业载体"
        elif row["mainline_grade"] == "B级主线":
            scene = "候选主线跟踪/等扩散确认"
        elif row["mainline_grade"] == "企稳重估":
            scene = "企稳重估/仅观察修复质量"
        else:
            scene = "低位修复观察/不追短线"
        rows.append(
            {
                "mainline_grade": row["mainline_grade"],
                "industry": industry,
                "etf_code": etf_code,
                "proxy": proxy_display,
                "scene": scene,
                "trend_state": row["trend_state"],
                "risk_note": row["risk_note"],
            }
        )
    return pd.DataFrame(rows)


def stock_action(row: pd.Series) -> str:
    if row["industry_stage"] in ["确认后退潮", "退潮风险"]:
        return "行业退潮，暂缓"
    if row["pct_chg"] <= -8:
        return "风险复核"
    if row["ret_20d"] > 0.80:
        return "过热不追"
    if (pd.notna(row.get("pe")) and row["pe"] > 200) or (pd.notna(row.get("pb")) and row["pb"] > 10):
        return "估值异常"
    if row["industry_stage"] in ["弱势反弹", "确认边缘"]:
        return "等回踩"
    if (
        row["wait_pullback"]
        or row["ret_20d"] > 0.20
        or row["pct_chg"] >= 9.8
        or row.get("breakout_amount_multiple", 0) > 2.5
        or row["pivot_distance_pct"] > 0.04
    ):
        return "等回踩"
    if row["valuation_flag"] != "估值初筛正常":
        return "风险复核" if not is_growth_industry(row["industry"]) else "等回踩"
    return "重点研究"


def stock_pool(row: pd.Series) -> str:
    hard_risk = (
        row["industry_stage"] in ["确认后退潮", "退潮风险"]
        or row["pct_chg"] <= -8
        or row["ret_20d"] > 0.50
        or (pd.notna(row.get("pe")) and row["pe"] > 100)
        or (pd.notna(row.get("pb")) and row["pb"] > 8)
        or row["wait_pullback"]
    )
    if hard_risk:
        return "过热/风险复核池"
    if row["industry_stage"] in ["弱势反弹", "确认边缘"]:
        return "低位修复/弱势反弹观察池"
    if is_growth_industry(row["industry"]) or row["ret_20d"] > 0.25 or row["valuation_flag"] != "估值初筛正常":
        return "弹性成长观察池"
    if row["industry_stage"] in ["候选主线", "强确认延续", "弱确认延续", "防御修复延续"] and row["valuation_flag"] == "估值初筛正常":
        return "稳健中军观察池"
    return "低位修复/弱势反弹观察池"


def _yesterday_score_text(market: dict) -> str:
    yday = market.get("yesterday_score")
    if yday is None:
        return "无昨日数据"
    diff = market["score"] - yday
    arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"
    return f"昨日 {yday} → 今日 {market['score']} ({arrow}{abs(diff)})"


def _env_condition_note(market: dict) -> str:
    if market["score"] >= 55:
        return "**环境已转强**（≥55），可关注主线行业 ETF 和中军载体"
    return "**环境偏弱**（<55），优先观察早期信号，不追高"


def _concept_divergence_tag(lifecycle: dict[str, pd.DataFrame]) -> str:
    """Return a tag if concept themes show notable divergences, else empty."""
    frame = lifecycle.get("mainline_overview", pd.DataFrame())
    if frame.empty or "resonance_status" not in frame.columns:
        return ""
    diverged = frame[frame["resonance_status"] == "❌ 背离"].head(3)
    if diverged.empty:
        return ""
    names = "、".join(diverged["industry"].astype(str).tolist())
    return f" ⚠️{names}与行业背离"


def _concept_divergence_note(lifecycle: dict[str, pd.DataFrame]) -> str:
    frame = lifecycle.get("mainline_overview", pd.DataFrame())
    if frame.empty or "resonance_status" not in frame.columns:
        return ""
    diverged = frame[frame["resonance_status"] == "❌ 背离"].head(3)
    if diverged.empty:
        return ""
    names = "、".join(diverged["industry"].astype(str).tolist())
    return f"（{names}与对应行业背离，注意分歧）"


def _compact_change_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 |"
    rows = []
    for _, r in frame.head(10).iterrows():
        rows.append(
            f"| {r.get('industry','')} | {r.get('today_level','')} | {r.get('t1_level','')} "
            f"| {r.get('t3_level','')} | {r.get('t5_level','')} | {r.get('today_stage','')} "
            f"| {r.get('early_signal_type','')} | {r.get('change_desc','')} | {r.get('judgment','')} |"
        )
    return "\n".join(rows)


def _compact_carrier_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 暂无 | 暂无 | 暂无 | 暂无 |"
    rows = []
    for _, r in frame.head(5).iterrows():
        rows.append(
            f"| {r.get('industry','')} | {r.get('symbol','')} {r.get('name','')} "
            f"| {r.get('trend_state','')} | {r.get('action','')} |"
        )
    return "\n".join(rows)


def _full_score_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 |"
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['mainline_grade']} | {r['industry']} | {r['stage']} | {r.get('driver_type','')} "
            f"| {r.get('speed_type','')} | {r.get('catalyst_rhythm','')} | {r.get('stage','')} "
            f"| {r.get('confirmed_score',0):.0f} | {pct(r.get('ret5'))} | {pct(r.get('ret20'))} "
            f"| {pct(r.get('ret60'))} | {pct(r.get('drawdown20'))} | {pct(r.get('drawdown60'))} "
            f"| {pct(r.get('above20'))} | {r.get('risk_note','')} |"
        )
    return "\n".join(rows)


def _mainline_action_table(
    lifecycle: dict[str, pd.DataFrame],
    stocks: dict[str, pd.DataFrame],
    concept_lifecycle: dict[str, pd.DataFrame],
) -> str:
    """Combine industry overview with ETF and action hints in one scannable table."""
    frame = lifecycle.get("mainline_overview", pd.DataFrame()).head(10)
    if frame.empty:
        return "| 暂无 | 暂无 | 暂无 | 暂无 |"

    # Build ETF lookup
    etf_map = {}
    etf_df = stocks.get("etf", pd.DataFrame())
    if not etf_df.empty and "industry" in etf_df.columns:
        for _, r in etf_df.iterrows():
            code = str(r.get("proxy", ""))
            if "暂无" not in code:
                etf_map[str(r["industry"])] = code

    rows = []
    for _, r in frame.iterrows():
        ind = str(r["industry"])
        grade = str(r.get("mainline_grade", ""))
        ret20_str = pct(r.get("ret20"))
        above20_str = pct(r.get("above20"))
        conclusion = str(r.get("risk_note", "") or r.get("status_explanation", ""))[:40]

        # Action hint
        action = ""
        if grade == "A级主线":
            action = "✅ 可跟"
        elif grade == "B级主线":
            action = "⏳ 观察"
        elif grade == "C级观察":
            action = "👀 等信号"
        elif "退潮" in grade:
            action = "❌ 躲开"
        else:
            action = "—"

        # ETF hint
        etf = etf_map.get(ind, "")

        rows.append(
            f"| {grade.replace('级主线','')} | {ind} | {ret20_str} | {above20_str} "
            f"| {action} | {etf} | {conclusion} |"
        )

    # Concept divergence note
    concept_note = _concept_divergence_note(concept_lifecycle)
    concept_rows = ""
    if concept_note:
        concept_rows = f"\n\n⚠️ 概念与行业背离：{concept_note}"

    header = "| 级别 | 行业 | 20日收益 | 宽度(MA20) | 操作 | 可交易ETF | 备注 |"
    sep = "| --- | --- | ---: | ---: | --- | --- | --- |"
    return header + "\n" + sep + "\n" + "\n".join(rows) + concept_rows


def render_report(
    report_date: str,
    market: dict,
    lifecycle: dict[str, pd.DataFrame],
    concept_lifecycle: dict[str, pd.DataFrame],
    stocks: dict[str, pd.DataFrame],
    yesterday_review: pd.DataFrame,
    recent_review: pd.DataFrame,
) -> str:
    report = f"""# A 股主线研究日报 V0.3（{report_date}）

## 0. 今日结论卡片

| 项目 | 结论 |
| --- | --- |
| 市场状态 | {market['label']}（{market['score']}/100） |
| 行动约束 | {environment_action_summary(market['score'])} |
| A级主线 | {compact_industries(lifecycle['grade_a'].head(5))} |
| B级主线 | {compact_industries(lifecycle['grade_b'].head(5))} |
| C级观察 | {compact_industries(lifecycle['grade_c'].head(5))} |
| 概念主题 | {compact_industries(concept_lifecycle.get('mainline_overview', pd.DataFrame()).head(5))} |
| 行业+概念共振 | {concept_resonance_pairs(concept_lifecycle, '✅ 共振', market['score'])} |
| 行业+概念背离 | {concept_resonance_pairs(concept_lifecycle, '❌ 背离', market['score'])} |
| 今日重点复核行业 | {compact_priority_review_industries(recent_review)} |
| 早期信号 | {early_signal_summary_line(recent_review, lifecycle)} |
| 退潮警报 | {compact_industries(lifecycle['retreat_mainline'].head(5))} |
| 四灯信号 | {four_lights_signal(market, recent_review, lifecycle, concept_lifecycle)} |

本日报是 **主线研究日报**，不是个股推荐、短线行动提示或交易指令。优先级是：市场环境 → 主线级别 → 行业生命周期 → 主线载体。正文只放核心判断，完整评分和载体池放在附录。

## 1. 市场环境

| 广度/趋势 | 数值 | 情绪/量能 | 数值 |
| --- | ---: | --- | ---: |
| A股样本数 | {market['sample_count']} | 成交额较上一交易日 | {pct(market['amount_chg'])} |
| 上涨股票比例 | {pct(market['up'])} | 涨幅>=5%数量 | {market['strong_up']} |
| 20日收益为正比例 | {pct(market['pos20'])} | 跌幅<=-5%数量 | {market['strong_down']} |
| 站上MA20比例 | {pct(market['above20'])} | 近似涨停/跌停 | {market['limit_up']} / {market['limit_down']} |
| 站上MA60比例 | {pct(market['above60'])} | 20日新高/新低 | {market['new_high20']} / {market['new_low20']} |

**数据缓存**：{cache_summary(market.get('cache_stats', {}))}

**概念缓存**：{concept_cache_summary(market.get('concept_cache_stats', {}))}

**概念成分股缓存**：{concept_member_cache_summary(market.get('concept_member_stats', {}))}

**环境分档**

| 环境分 | 市场状态 | 含义 | 行动约束 |
| ---: | --- | --- | --- |
| 0-29 | 防守/弱势 | 市场宽度差，退潮风险高 | 不做新增行动建议，只做风险监控 |
| 30-44 | 弱观察 | 局部修复，但整体弱 | 只观察，不追 |
| 45-54 | 偏观察 | 有结构机会，但不支持进攻 | 仅研究观察，等待确认 |
| 55-69 | 中性偏强 | 主线可跟踪，但执行需克制 | 优先观察 ETF/中军，不追高 |
| ≥70 | 进攻 | 主线扩散，环境配合 | 可积极跟踪，但仍需结构验证 |

**昨日对比**

{market_score_compare_table(market)}

**主要指数**

| 指数 | 收盘 | 涨跌幅 | 站上MA20 | 站上MA60 | 数据状态 |
| --- | ---: | ---: | --- | --- | --- |
{index_table(market['indexes'], market.get('missing_indices', []))}

## 2. 主线总览

| 级别 | 行业 | 状态 | 驱动 | 速度 | 20日 | 60日 | 60峰值回撤 | 宽度 | 结论 |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |
{compact_mainline_overview_table(lifecycle['mainline_overview'].head(10))}

## 3. 概念主题主线

| 级别 | 概念 | 状态 | 驱动 | 对应行业 | 共振判断 | 20日 | 60日 | 60峰值回撤 | 宽度 | 可交易ETF | 结论 |
| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |
{concept_theme_table(concept_lifecycle)}

概念板块用于捕捉商业航天、低空经济、AI、创新药等主题驱动行情；它和行业主线是平行维度，不互相替代。若行业与概念同时入选，可视为"行业 + 概念共振"的研究线索。

共振/背离字段仅用于辅助判断主线可信度和市场共识方向，不改变主线评级，也不构成交易信号。

## 4. 主线变化复核

| 行业 | 今日级别 | T-1级别 | T-3级别 | T-5级别 | 今日阶段 | 早期信号类型 | 近5日状态变化 | 生命周期判断 | 原处理 | 复核优先级 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
{recent_lifecycle_review_table(recent_review.head(10))}

## 5. 退潮与风险

| 行业 | 状态 | 驱动 | 速度 | 阶段 | 5日 | 60峰值回撤 | 宽度 | 风险备注 |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- |
{compact_retreat_table(lifecycle['retreat_mainline'].head(6))}

**昨日判断复核重点**

| 昨日行业 | 昨日级别 | 昨日阶段 | 今日表现 | 峰值回撤变化 | 今日阶段 | 判断结果 | 当前处理 |
| --- | --- | --- | ---: | ---: | --- | --- | --- |
{compact_yesterday_review_table(yesterday_review.head(8))}

## 6. 主线载体摘要

{carrier_summary_table(stocks, market['score'])}

## 7. 明日复核清单

{next_day_checklist(recent_review, lifecycle)}

## 8. 疑似漏报复核摘要

{suspect_miss_summary_table(lifecycle['suspect_miss'])}

本模块用于发现 60 日涨幅较高但未进入主线榜的行业，辅助判断是否存在行业分类过粗、细分主线被稀释、宽度不足或峰值回撤过深等问题。疑似漏报不等于研究结论，本模块仅用于框架审计和后续迭代。

---

# 附录

## 附录 A：完整主线评分表

### A1 主线级别总览

| 主线级别 | 行业 | 状态解释 | 驱动类型 | 速度 | 催化节奏 | 当前阶段 | 综合分 | 5日 | 20日 | 60日 | 20峰值回撤 | 60峰值回撤 | 宽度 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
{mainline_overview_table(lifecycle['mainline_overview'].head(18))}

### A2 A级主线

| 行业 | 状态解释 | 速度 | 催化节奏 | 综合分 | 20日 | 60日 | 20峰值回撤 | 60峰值回撤 | 宽度 | 风险 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
{grade_detail_table(lifecycle['grade_a'])}

### A3 B级主线

| 行业 | 状态解释 | 驱动类型 | 速度 | 阶段 | 综合分 | 5日 | 20日 | 60日 | 60峰值回撤 | 宽度 | 结论 |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
{bc_table(lifecycle['grade_b'].head(10))}

### A4 C级观察

| 行业 | C级类型 | 驱动类型 | 速度 | 阶段 | 分数 | 5日 | 20日 | 60日 | 60峰值回撤 | 结论 |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
{c_table(lifecycle['grade_c'].head(10))}

### A5 企稳重估

| 行业 | 驱动类型 | 阶段 | 5日 | 20日回撤 | 宽度 | 量能 | 处理 |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
{stable_revaluation_table(lifecycle['stable_revaluation'])}

### A6 概念主题完整表

| 级别 | 概念 | 状态解释 | 驱动类型 | 速度 | 催化节奏 | 当前阶段 | 综合分 | 5日 | 20日 | 60日 | 20峰值回撤 | 60峰值回撤 | 宽度 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
{mainline_overview_table(concept_lifecycle.get('mainline_overview', pd.DataFrame()).head(18))}

## 附录 B：完整主线载体池

载体池不是行动清单，而是帮助跟踪主线是否真实扩散。ETF/中军/弹性载体只允许来自当日 A/B/C/企稳重估行业；退潮行业个股只能进入风险复核。

### B1 ETF / 行业指数

| 主线级别 | 主线 | 可交易ETF | 适合场景 | 当前状态 | 风险提示 |
| --- | --- | --- | --- | --- | --- |
{etf_table(etf_proxy_table(lifecycle))}

### B2 中军龙头

| 主线 | 代码 | 名称 | 市值层级 | 行业地位 | 趋势状态 | 估值温度 | 基本面标签 | 研究优先级 | 行动约束 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
{carrier_table(stocks['core'], market['score'], carrier_type='core')}

### B3 弹性龙头

| 主线 | 代码 | 名称 | 弹性来源 | 趋势状态 | 过热状态 | 风险标签 | 研究优先级 | 行动约束 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{carrier_table(stocks['elastic'], market['score'], carrier_type='elastic')}

### B4 风险复核标的

| 主线 | 代码 | 名称 | 趋势状态 | 估值温度 | 风险标签 | 对主线影响 | 研究优先级 | 行动约束 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
{risk_carrier_table(stocks['risk'], market['score'])}

## 附录 C：主线生命周期迁移规则

| 迁移 | 需要看到什么 | 不能忽略什么 |
| --- | --- | --- |
| 升温观察 | 5/10日相对强度改善，成交开始放大，宽度改善 | 20/60日可能仍未确认 |
| 升温 → 候选 | 5/10日不弱，过去20日跑赢天数提升，站上MA20比例改善，回撤可控 | 不能只是单日脉冲 |
| 候选 → 确认 | 20日表现转强，行业扩散继续，成交额未明显萎缩 | 若宽度不足，只能确认边缘 |
| 确认 → 退潮 | 5日明显转弱，20日回撤扩大，站上MA20比例快速下降 | 中期分高也要降级 |
| 退潮 → 企稳重估 | 5/10日修复，宽度回升，量能温和改善，回撤不再扩大 | 不直接恢复为候选主线 |
| 退潮 → 低频监控 | 连续宽度低于20%，20日相对强度继续下降，量能无改善 | 移出核心退潮榜，只低频观察 |
| 退潮 → 重新升温 | 重新满足升温观察，宽度和量能改善，龙头重新走强 | 从升温观察重新开始 |
| 弱势反弹 → 候选 | 中期趋势修复、宽度提升、回撤收窄 | 不能因5日反弹直接升级 |
| 确认后退潮 | 中期曾强，但短期急跌、回撤大或宽度塌缩 | 只做风险监控和等待修复 |

### C1 早期主线信号说明

| 信号 | 标签 | 含义 | 识别条件 |
| --- | --- | --- | --- |
| 企稳重估 | 🛡 不跌了 | 之前跌透了，现在止跌反弹，宽度恢复，量能企稳 | 退潮评分≥55 但短线转正，宽度≥35%，回撤收窄 |
| 重新升温 | 🔥 冷变热 | 之前无人关注的行业，突然开始涨，从冷板凳升级到 C 级 | 前 5 天还在退潮/低频，现在升为 C 级且 5 日收益>0 |
| C级结构修复 | 🔧 在修复 | C 级行业，跌得不深、没崩，正在修复技术结构 | C 级但非弱势反弹，20 日收益>-3%，60 日回撤>-15% |

### C2 早期主线回测数据

基于 2021~2026 年 5 年回测，早期核心信号 + 环境分 ≥45 的组合（2653 条样本）。

| 信号类型 | 样本 | 20日胜率 | 40日胜率 | 40日超额 | 40日峰值 | 见顶天数 | 最佳环境 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 🛡 企稳修复 | 1300 | 66.0% | **69.5%** | +3.08% | +3.40% | 13 天 | 偏观察(45-54) 胜率 77.1% |
| 🔥 冷变热 | 745 | 58.0% | **58.9%** | +2.61% | **+4.37%** | 13 天 | 偏观察(45-54) 胜率 63.6% |
| 🔧 修复 | 608 | 57.6% | **62.0%** | +2.05% | +3.68% | 12 天 | 中性偏强(55-69) 胜率 65.1% |

**三条操作铁律：**

1. **企稳修复优先**。40 日胜率 69.5%、回撤最小（-9.1%），是赔率最高的信号类型。
2. **冷变热在进攻环境反而要警惕**。当环境分 ≥70 时胜率仅 55.3%，热门市场里的回暖常是假性升温。
3. **最佳持有窗口是 40 天**。三个信号从 20 天→40 天胜率都有 +3%~+5% 的跳升，40→60 天边际提升有限。

### C3 交易策略范式（回测验证版）

基于 2021-2026 年 2653 条早期信号逐条验证。β 策略适用。

| 阶段 | 时机 | 企稳 🛡 | 回暖 🔥 | 修复 🔧 | 关键规则 |
| --- | --- | --- | --- | --- | --- |
| ① 建仓 | 信号后 1~3 天 | 8~10% | 5~8%（≥70 减半） | 5% | 有 ETF/中军才建；等回踩 |
| ② 加仓 | 第 5~10 天 | +5% | +5%（≥70 不加） | +5% | 级别升级或站上 MA20；**不**绑宽度 |
| ③ 减仓 | 达峰或触发 | 达 +3.4% 减 1/3 | 达 +4.4% 减 1/3 | 达 +3.7% 减 1/3 | 中位峰值在 8~9 天 |
| ④ 清仓 | 40 天或触发 | 全部 | 全部 | 全部 | 到期/退潮/回撤达 -7% |

**统一止损：持仓回撤达 -7%。** 「日低 -3%」无效（82% 触发）。回撤 <5% 时胜率 88-93%，>7% 时仅 50-59%。

**环境规则：** ≤29 不建仓（已有仓位不因环境清仓）；45-54 企稳最优（77.1% 胜率）；≥70 回暖减半（胜率仅 55.3%）。

> 详细范式及验证数据：见 `dp-xiangmu.md` 第 10-11 节。

## 附录 D：疑似漏报复核明细

> 疑似漏报不等于机会提示。部分行业可能已处于退潮初期、宽度不足，或仅为少数个股/细分方向拉动。本模块仅用于框架审计和后续迭代，不构成交易建议。

| 行业 | 60日收益 | 20日收益 | 5日收益 | 站上MA20比例 | 站上MA60比例 | 60日峰值回撤 | 未入选原因 | 复核结论 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
{suspect_miss_table(lifecycle['suspect_miss'])}

## 附录 E：数据口径、局限与 TODO

- 本框架不负责猜底；它只负责识别升温、确认主线、跟踪延续、发现退潮、判断是否企稳重估，不负责预测底部、捕捉最低点、给出交易执行点、执行管理或精细个股排序。
- 今日数据来自 Tushare 日线快照，不含真实9:26集合竞价和分钟级成交。
- 行业分类使用本地 stock_basic 行业字段，后续应升级为申万/中信行业 + 概念主题双维度。
- 概念主题接入东方财富概念板块，和行业主线平行展示；概念板块宽度使用涨跌家数比例 `up_num/(up_num+down_num)` 近似，非真实个股 MA 穿透计算。
- 概念板块价格用每日涨跌幅反推等权价格指数，和市值加权指数、实际 ETF 净值存在偏差。
- 概念成分股可能和行业分类重叠，两者不是互斥关系；东方财富概念板块每日更新，可能存在 1-2 日数据延迟。
- 当前已增加风格/主题标签，后续应建立行业-主题-风格三层映射。
- 基本面字段后续只用于主线解释、载体分层、风险标签和排雷，不进入主线综合分，也不作为固定个股分权重。预留字段：ROE, EPS, 营收同比增速, 归母净利润同比增速, 扣非净利润同比增速, 毛利率, 净利率, 经营现金流/净利润, 资产负债率, PE历史分位, PB历史分位, 行业估值分位。
- 当前内部个股分只用于筛选载体，不作为外部推荐排序；基本面不做正向加权，只通过估值温度、基本面标签和硬性风险惩罚影响展示。
- 目前基本面只做 PE/PB 与标签级判断：质量较稳、盈利改善、盈利承压、亏损/不可比、现金流待核验、估值正常、估值偏高、估值极高、周期高位风险、财务数据待补。
- 风险惩罚是硬扣分，不应被强度分完全抵消；用户不应理解为"个股分越高 = 越值得行动"。
- 后续补充退潮持续天数：用于区分初始退潮、退潮确认、退潮延续和低频监控。
- 本日报不构成投资建议。

### 宽度分级参考

| 站上MA20比例 | 判断 |
| ---: | --- |
| ≥80% | 扩散良好 |
| 60%-80% | 扩散尚可 |
| 40%-60% | 分化明显 |
| 20%-40% | 宽度不足 |
| <20% | 高风险 / 弱扩散 |
"""
    return _apply_stage_labels(report)


def _apply_stage_labels(text: str) -> str:
    """Replace internal stage/grade names with display labels in the final report."""
    for internal, display in _STAGE_LABEL.items():
        text = text.replace(internal, display)
    for internal, display in _GRADE_LABEL.items():
        text = text.replace(internal, display)
    for internal, display in _SIGNAL_LABEL.items():
        text = text.replace(internal, display)
    return text


def pct(value: float) -> str:
    return "NA" if pd.isna(value) else f"{value:.2%}"


# ---- 阶段/级别显示映射（内部逻辑不变，仅影响日报展示用词） ----
_STAGE_LABEL = {
    "强确认延续": "强势确认", "弱确认延续": "偏弱确认", "防御修复延续": "防御修复",
    "确认边缘": "待确认", "确认后退潮": "逐步退潮",
    "企稳重估": "企稳修复", "候选主线": "待观察", "升温观察": "预热中",
    "接力催化初现": "接力初现", "弱势反弹": "弱反弹", "低频监控": "低关注",
    "暂不观察": "无信号", "高位震荡": "高震", "峰值回撤后修复": "回撤修复",
    "早期升温": "初升温", "真延续": "趋势延续",
    "C级加速升温": "加速升温", "C级结构修复": "C修复",
}
_GRADE_LABEL = {
    "退潮主线": "退潮",
}
_SIGNAL_LABEL = {
    "企稳重估": "🛡 企稳", "重新升温": "🔥 回暖", "C级结构修复": "🔧 修复",
}


def _label(text: str) -> str:
    """Translate internal stage/grade to display label."""
    return _STAGE_LABEL.get(text, _GRADE_LABEL.get(text, _SIGNAL_LABEL.get(text, text)))


def num(value: float, digits: int = 1) -> str:
    return "NA" if pd.isna(value) else f"{value:.{digits}f}"


def index_table(rows: list[dict], missing_indices: list[str] | None = None) -> str:
    missing_indices = missing_indices or []
    if not rows or missing_indices:
        missing_text = "、".join(missing_indices) if missing_indices else "全部主要指数"
        warning = (
            f"| 数据缺失 | NA | NA | NA | NA | 缺失 |\n\n"
            f"> 主要指数数据缺失：{missing_text}。今日无法渲染完整指数趋势表，请检查 Tushare 指数代码、字段名、MA20/MA60历史长度或本地指数缓存。"
        )
        if not rows:
            return warning
        rendered = "\n".join(
            f"| {r['name']} | {r['close']:.2f} | {r['pct_chg']:.2f}% | {'是' if r['above20'] else '否'} | {'是' if r['above60'] else '否'} | {r.get('data_status', '当日数据')} |"
            for r in rows
        )
        return f"{rendered}\n{warning}"
    return "\n".join(
        f"| {r['name']} | {r['close']:.2f} | {r['pct_chg']:.2f}% | {'是' if r['above20'] else '否'} | {'是' if r['above60'] else '否'} | {r.get('data_status', '当日数据')} |"
        for r in rows
    )


def market_score_compare_table(market: dict) -> str:
    prev = market.get("previous")
    if not prev:
        return (
            "| 项目 | 数值 |\n"
            "| --- | ---: |\n"
            f"| 今日环境分 | {market.get('score', 'NA')} |\n"
            "| 昨日环境分 | 暂无历史记录 |\n"
            "| 环境分变化 | NA |\n"
            f"| 状态变化 | 暂无历史记录 → {market.get('label', 'NA')} |"
        )
    prev_score = prev.get("score")
    score = market.get("score")
    delta = score - prev_score if pd.notna(score) and pd.notna(prev_score) else np.nan
    delta_text = "NA" if pd.isna(delta) else f"{delta:+.0f}"
    return (
        "| 项目 | 数值 |\n"
        "| --- | ---: |\n"
        f"| 今日环境分 | {score} |\n"
        f"| 昨日环境分 | {prev_score} |\n"
        f"| 环境分变化 | {delta_text} |\n"
        f"| 状态变化 | {prev.get('label', 'NA')} → {market.get('label', 'NA')} |"
    )


def compact_mainline_overview_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {compact_grade(r['mainline_grade'])} | {r['industry']} | {r['status_explanation']} | {r['driver_type']} | {r['speed_type']} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['drawdown60'])} | {pct(r['above20'])} | {compact_mainline_conclusion(r)} |"
        )
    return "\n".join(rows) if rows else "| 暂无 | NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def concept_theme_table(lifecycle: dict[str, pd.DataFrame]) -> str:
    if lifecycle.get("status") == "missing":
        return "| 暂无 | 概念数据缺失 | NA | NA | — | — 无对应 | NA | NA | NA | NA | 请先同步 concept_daily 概念板块数据 |"
    if lifecycle.get("status") == "insufficient":
        return "| 暂无 | 概念历史不足 | NA | NA | — | — 无对应 | NA | NA | NA | NA | 概念数据少于60个交易日，暂无法计算生命周期 |"
    frame = lifecycle.get("mainline_overview", pd.DataFrame())
    if frame.empty:
        return "| 暂无 | 无入选概念 | NA | NA | — | — 无对应 | NA | NA | NA | NA | 当日暂无 A/B/C/企稳级别概念主题 |"
    visible = frame[frame["mainline_grade"].isin(["A级主线", "B级主线", "C级观察", "企稳重估"])].head(12)
    rows = []
    for _, r in visible.iterrows():
        concept_name = r["industry"]
        etf_info = format_etf_proxy(concept_name)
        rows.append(
            f"| {compact_grade(r['mainline_grade'])} | {concept_name} | {r['status_explanation']} | {r['driver_type']} | {r.get('matched_industry_display', '—')} | {r.get('resonance_status', '— 无对应')} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['drawdown60'])} | {pct(r['above20'])} | {etf_info} | {concept_conclusion(r)} |"
        )
    return "\n".join(rows) if rows else "| 暂无 | 无入选概念 | NA | NA | — | — 无对应 | NA | NA | NA | NA | 暂无合适ETF | 当日暂无 A/B/C/企稳级别概念主题 |"


def concept_conclusion(row: pd.Series) -> str:
    conclusion = compact_mainline_conclusion(row)
    if row.get("mainline_grade") in ["C级观察", "企稳重估"]:
        return f"{conclusion}；优先复核主题延续性"
    return conclusion


def compact_grade(grade: str) -> str:
    mapping = {
        "A级主线": "A",
        "B级主线": "B",
        "C级观察": "C",
        "退潮主线": "退潮",
        "低频监控": "低频监控",
        "企稳重估": "企稳",
    }
    return mapping.get(str(grade), str(grade))


def compact_mainline_conclusion(row: pd.Series) -> str:
    conclusion = str(row.get("mainline_conclusion", ""))
    rhythm = row.get("catalyst_rhythm", "暂不明确")
    if rhythm and rhythm != "暂不明确":
        return f"{rhythm}；{conclusion}"
    return conclusion


def overview_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['stage']} | {r['stage_score']:.1f} | {pct(r['ret5'])} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['above20'])} | {pct(r['drawdown20'])} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def mainline_overview_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['mainline_grade']} | {r['industry']} | {r['status_explanation']} | {r['driver_type']} | {r['speed_type']} | {r['catalyst_rhythm']} | {r['stage']} | {r['stage_score']:.1f} | {pct(r['ret5'])} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['drawdown20'])} | {pct(r['drawdown60'])} | {pct(r['above20'])} | {r['mainline_conclusion']} |"
        )
    return "\n".join(rows) if rows else "| NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def grade_detail_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['status_explanation']} | {r['speed_type']} | {r['catalyst_rhythm']} | {r['stage_score']:.1f} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['drawdown20'])} | {pct(r['drawdown60'])} | {pct(r['above20'])} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| 暂无 | NA | NA | NA | NA | NA | NA | NA | NA | NA | 当前市场环境或行业结构不足以授予A级 |"


def bc_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['status_explanation']} | {r['driver_type']} | {r['speed_type']} | {r['stage']} | {r['stage_score']:.1f} | {pct(r['ret5'])} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['drawdown60'])} | {pct(r['above20'])} | {r['mainline_conclusion']} |"
        )
    return "\n".join(rows) if rows else "| 暂无 | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def c_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['c_level_type']} | {r['driver_type']} | {r['speed_type']} | {r['stage']} | {r['stage_score']:.1f} | {pct(r['ret5'])} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['drawdown60'])} | {r['mainline_conclusion']} |"
        )
    return "\n".join(rows) if rows else "| 暂无 | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def retreat_mainline_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['status_explanation']} | {r['driver_type']} | {r['speed_type']} | {r['stage']} | {r['retreat_score']:.1f} | {pct(r['ret5'])} | {pct(r['drawdown20'])} | {pct(r['drawdown60'])} | {pct(r['above20'])} | {r['catalyst_rhythm']} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| 暂无 | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def compact_retreat_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['status_explanation']} | {r['driver_type']} | {r['speed_type']} | {r['stage']} | {pct(r['ret5'])} | {pct(r['drawdown60'])} | {pct(r['above20'])} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| 暂无 | NA | NA | NA | NA | NA | NA | NA | NA |"


def stable_revaluation_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['driver_type']} | {r['stage']} | {pct(r['ret5'])} | {pct(r['drawdown20'])} | {pct(r['above20'])} | {num(r['amount5_60'], 2)} | 企稳重估，等待是否重新升温 |"
        )
    return "\n".join(rows) if rows else "| 暂无 | NA | NA | NA | NA | NA | NA | NA |"


def suspect_miss_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 暂无 | NA | NA | NA | NA | NA | NA | 暂无满足60日高涨幅且未入选的行业 | 无需复核 |"
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {pct(r['ret60'])} | {pct(r['ret20'])} | {pct(r['ret5'])} | {pct(r['above20'])} | {pct(r['above60'])} | {pct(r['drawdown60'])} | {r['miss_reason']} | {r['review_conclusion']} |"
        )
    return "\n".join(rows)


def compact_industries(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "无"
    return "、".join(frame["industry"].head(5).astype(str).tolist())


def compact_priority_review_industries(recent_review: pd.DataFrame) -> str:
    if recent_review.empty or "priority_label" not in recent_review.columns:
        return "无"
    frame = recent_review[recent_review["priority_label"] == "优先复核"]
    if frame.empty:
        return "无"
    names = "、".join(frame["industry"].head(5).astype(str).tolist())
    return f"{names}（企稳重估/重新升温/C级结构修复 + 环境分≥45）"


def early_signal_summary_line(
    recent_review: pd.DataFrame,
    lifecycle: dict[str, pd.DataFrame],
) -> str:
    """Extract and display early signal industries with backtest stats for trial positioning."""
    early_from_review = pd.DataFrame()
    if not recent_review.empty and "early_signal_type" in recent_review.columns:
        early_from_review = recent_review[recent_review["early_signal_type"].isin(["企稳重估", "重新升温", "C级结构修复"])].copy()

    stable = lifecycle.get("stable_revaluation", pd.DataFrame())

    seen = set()

    # ---- Collect all three types ----
    # 企稳重估: from stable_revaluation + early_from_review with matching type
    stable_list = []
    if not stable.empty and "industry" in stable.columns:
        for _, r in stable.iterrows():
            name = str(r["industry"])
            if name not in seen:
                seen.add(name)
                stable_list.append(name)
    if not early_from_review.empty:
        for _, r in early_from_review.iterrows():
            name = str(r["industry"])
            if name not in seen and r.get("early_signal_type") == "企稳重估":
                seen.add(name)
                stable_list.append(name)

    # 重新升温
    rewarm_list = []
    if not early_from_review.empty:
        rewarm = early_from_review[early_from_review["early_signal_type"] == "重新升温"]
        for _, r in rewarm.iterrows():
            name = str(r["industry"])
            if name not in seen:
                seen.add(name)
                rewarm_list.append(name)

    # C级结构修复
    repair_list = []
    if not early_from_review.empty:
        repair = early_from_review[early_from_review["early_signal_type"] == "C级结构修复"]
        for _, r in repair.iterrows():
            name = str(r["industry"])
            if name not in seen:
                seen.add(name)
                repair_list.append(name)

    # ---- Format output ----
    LABELS = {
        "企稳重估": "🛡 不跌了（企稳）",
        "重新升温": "🔥 冷变热（升温）",
        "C级结构修复": "🔧 在修复（修结构）",
    }
    STATS_MAP = {
        "企稳重估": "胜率69.5% 超额+3.1% 峰值+3.4% 约13天见顶",
        "重新升温": "胜率58.9% 超额+2.6% 峰值+4.4% 约13天见顶(进攻>70时慎用)",
        "C级结构修复": "胜率62.0% 超额+2.1% 峰值+3.7% 约12天见顶",
    }

    def format_group(signal_type, industry_list):
        label = LABELS.get(signal_type, signal_type)
        names = "、".join(industry_list)
        stat = STATS_MAP.get(signal_type, "")
        return f"{label} → {names}（{stat}）"

    parts = []
    if stable_list:
        parts.append(format_group("企稳重估", stable_list[:4]))
    if rewarm_list:
        parts.append(format_group("重新升温", rewarm_list[:4]))
    if repair_list:
        parts.append(format_group("C级结构修复", repair_list[:4]))

    if not parts:
        return "无"

    return " ".join(parts)


def concept_resonance_pairs(lifecycle: dict[str, pd.DataFrame], status: str, market_score: float) -> str:
    frame = lifecycle.get("mainline_overview", pd.DataFrame())
    if frame.empty or "resonance_status" not in frame.columns:
        return "暂无明显共振 / 背离" if status == "✅ 共振" else "暂无明显背离"
    matched = frame[frame["resonance_status"] == status].head(8)
    if matched.empty:
        return "暂无明显共振" if status == "✅ 共振" else "暂无明显背离"
    pairs = []
    for _, row in matched.iterrows():
        industry = row.get("matched_industry")
        grade = row.get("matched_industry_grade")
        suffix = "退潮" if status == "❌ 背离" and grade in ["退潮主线", "低频监控"] else ""
        pairs.append(f"{row['industry']} ↔ {industry}{suffix}")
    note = ""
    if market_score >= 55 and status == "✅ 共振":
        note = "（环境分≥55，优先解释为资金共识增强）"
    elif market_score >= 55 and status == "❌ 背离":
        note = "（环境分≥55，提示资金分歧）"
    elif market_score < 45:
        note = "（环境偏弱，仅观察）"
    return "、".join(pairs) + note


def four_lights_signal(
    market: dict,
    recent_review: pd.DataFrame,
    lifecycle: dict[str, pd.DataFrame],
    concept_lifecycle: dict[str, pd.DataFrame],
) -> str:
    env_score = float(market.get("score", 0) or 0)
    lamp_env = "🟢" if env_score >= 55 else "🔴"
    env_note = f"环境{env_score:.0f}<55" if env_score < 55 else f"环境{env_score:.0f}>=55"

    priority = priority_review_frame(recent_review)
    lamp_direction = "🟢" if not priority.empty else "🔴"
    direction_note = f"优先复核{len(priority)}个" if not priority.empty else "无优先复核"

    resonance_lamp, resonance_note = concept_direction_resonance_lamp(concept_lifecycle)
    timing_lamp, timing_note = timing_lamp_for_priority(priority, lifecycle)

    lights = f"{lamp_env}{lamp_direction}{resonance_lamp}{timing_lamp}"
    posture = four_lights_posture([lamp_env, lamp_direction, resonance_lamp, timing_lamp])
    return f"{lights} → {posture}（{env_note}；{direction_note}；{resonance_note}；{timing_note}）"


def priority_review_frame(recent_review: pd.DataFrame) -> pd.DataFrame:
    if recent_review.empty or "priority_label" not in recent_review.columns:
        return pd.DataFrame()
    return recent_review[recent_review["priority_label"] == "优先复核"].copy()


def concept_direction_resonance_lamp(concept_lifecycle: dict[str, pd.DataFrame]) -> tuple[str, str]:
    frame = concept_lifecycle.get("mainline_overview", pd.DataFrame())
    needed = {"resonance_status", "matched_industry"}
    if frame.empty or not needed.issubset(frame.columns):
        return "⚪", "无共振数据"
    resonant = frame[frame["resonance_status"] == "✅ 共振"].copy()
    if resonant.empty:
        return "🔴", "无行业概念同向"
    pairs = [
        f"{row.get('industry')}↔{row.get('matched_industry')}"
        for _, row in resonant.head(2).iterrows()
    ]
    suffix = "等" if len(resonant) > 2 else ""
    return "🟢", f"共振{len(resonant)}对：{'、'.join(pairs)}{suffix}"


def timing_lamp_for_priority(priority: pd.DataFrame, lifecycle: dict[str, pd.DataFrame]) -> tuple[str, str]:
    if priority.empty:
        return "🔴", "无早期时机"
    all_rows = lifecycle.get("all", pd.DataFrame())
    if all_rows.empty or "industry" not in all_rows.columns:
        return "⚪", "无时机数据"
    watch = set(priority["industry"].astype(str).tolist())
    frame = all_rows[all_rows["industry"].astype(str).isin(watch)].copy()
    if frame.empty:
        return "⚪", "无时机数据"
    hot_terms = ["情绪顶点", "偏离过大"]
    hot_names = []
    for _, row in frame.iterrows():
        text = " ".join(
            str(row.get(col, ""))
            for col in ["status_explanation", "trend_state", "catalyst_rhythm", "mainline_conclusion"]
        )
        if any(term in text for term in hot_terms):
            hot_names.append(str(row.get("industry")))
    if hot_names:
        return "🔴", f"{'、'.join(hot_names[:3])}偏热"
    return "🟢", "早期方向未见情绪顶点"


def four_lights_posture(lamps: list[str]) -> str:
    green = lamps.count("🟢")
    if green >= 3:
        return "机会强"
    if green == 2:
        return "重点复核"
    if green == 1:
        return "仅观察"
    return "不动"


def environment_action_summary(score: float) -> str:
    if score >= 70:
        return "允许重点跟踪，但仍需等待结构验证"
    if score >= 55:
        return "研究可继续，行动约束降级，优先回踩确认"
    if score >= 40:
        return "仅研究观察，不主动行动，最高行动等级为等回踩"
    return "防守优先，不做新增行动建议"


def cache_summary(stats: dict) -> str:
    if not stats:
        return "未记录"
    if stats.get("mode") == "cache_hit":
        return f"本地缓存命中，日线 {stats.get('daily_rows', 0)} 行，估值快照 {stats.get('daily_basic_rows', 0)} 行。"
    if stats.get("mode") == "cache_hit_missing_daily_basic":
        return (
            f"本地日线缓存命中，日线 {stats.get('daily_rows', 0)} 行；"
            "当日估值快照缺失，日报继续生成，但估值/基本面标签仅作缺失提示。"
        )
    return (
        "本次仅增量补齐当日缓存，"
        f"新增日线 {stats.get('synced_daily_rows', 0)} 行，"
        f"新增估值快照 {stats.get('synced_daily_basic_rows', 0)} 行；"
        f"当前当日日线 {stats.get('daily_rows', 0)} 行，估值快照 {stats.get('daily_basic_rows', 0)} 行。"
    )


def concept_cache_summary(stats: dict) -> str:
    if not stats:
        return "未记录"
    if stats.get("mode") == "cache_hit":
        return f"本地概念缓存命中，概念日线 {stats.get('concept_daily_rows', 0)} 行。"
    if stats.get("mode") == "missing_no_token":
        return (
            f"概念日线缓存不足，当前 {stats.get('concept_daily_rows', 0)} 行；"
            "未检测到 TUSHARE_TOKEN，概念主题主线将显示缺失提示。"
        )
    return (
        "本次增量补齐概念缓存，"
        f"新增概念日线 {stats.get('synced_concept_daily_rows', 0)} 行，"
        f"当前概念日线 {stats.get('concept_daily_rows', 0)} 行。"
    )


def concept_member_cache_summary(stats: dict) -> str:
    if not stats:
        return "未记录"
    mode = stats.get("mode")
    if mode == "cache_hit":
        return f"本地概念成分股缓存命中，成分股 {stats.get('concept_member_rows', 0)} 行。"
    if mode == "incremental_sync":
        return (
            "本次增量补齐概念成分股，"
            f"新增/更新 {stats.get('synced_concept_member_rows', 0)} 行，"
            f"当前成分股 {stats.get('concept_member_rows', 0)} 行。"
        )
    if mode == "sync_failed":
        return f"概念成分股同步失败：{stats.get('error', '未知错误')}；共振/背离字段将显示无对应。"
    if mode == "missing_no_token":
        return "概念成分股缓存缺失且无 TUSHARE_TOKEN；共振/背离字段将显示无对应。"
    return "未记录"


def market_snapshot_path(report_date: str) -> Path:
    return MARKET_SNAPSHOT_DIR / f"market_snapshot_{report_date}.json"


def save_market_snapshot(report_date: str, market: dict) -> None:
    MARKET_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": report_date,
        "score": market.get("score"),
        "label": market.get("label"),
        "up": market.get("up"),
        "pos20": market.get("pos20"),
        "above20": market.get("above20"),
        "above60": market.get("above60"),
        "amount_chg": market.get("amount_chg"),
    }
    market_snapshot_path(report_date).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_previous_market_snapshot(report_date: str) -> dict | None:
    for path in sorted(MARKET_SNAPSHOT_DIR.glob("market_snapshot_*.json"), reverse=True):
        date_part = path.stem.replace("market_snapshot_", "")
        if date_part < report_date:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def index_cache_path(report_date: str) -> Path:
    return INDEX_CACHE_DIR / f"index_cache_{report_date}.json"


def save_index_cache(report_date: str, rows: list[dict]) -> None:
    INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"date": report_date, "rows": rows}
    index_cache_path(report_date).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_previous_index_cache(report_date: str) -> list[dict]:
    for path in sorted(INDEX_CACHE_DIR.glob("index_cache_*.json"), reverse=True):
        date_part = path.stem.replace("index_cache_", "")
        if date_part < report_date:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rows = payload.get("rows", [])
            if rows:
                return rows
    return []


def carrier_summary_table(stocks: dict[str, pd.DataFrame], market_score: float) -> str:
    rows = ["| 类型 | 摘要 | 代表方向/标的 | 行动约束 |", "| --- | --- | --- | --- |"]
    for key, label in [("etf", "ETF/指数"), ("core", "中军龙头"), ("elastic", "弹性龙头"), ("risk", "风险复核")]:
        frame = stocks.get(key, pd.DataFrame())
        count = 0 if frame.empty else len(frame)
        if frame.empty:
            names = "暂无"
            action = "继续观察"
        elif key == "etf":
            names = "、".join(frame["industry"].head(4).astype(str).tolist())
            action = "仅作主线载体跟踪"
        elif key == "risk":
            names = "、".join((frame["industry"].astype(str) + "/" + frame["name"].fillna("").astype(str)).head(4).tolist())
            action = "风险复核"
        else:
            names = "、".join((frame["industry"].astype(str) + "/" + frame["name"].fillna("").astype(str)).head(4).tolist())
            action = environment_action_summary(market_score)
        rows.append(f"| {label} | {count} 个 | {names} | {action} |")
    return "\n".join(rows)


def suspect_miss_summary_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 摘要 | 结论 |\n| --- | --- |\n| 暂无明显疑似漏报 | 无需复核 |"
    conclusions = frame["review_conclusion"].value_counts().head(3)
    summary = "；".join(f"{idx} {count} 个" for idx, count in conclusions.items())
    names = "、".join(frame["industry"].head(5).astype(str).tolist())
    return f"| 摘要 | 结论 |\n| --- | --- |\n| {names} | {summary}；完整明细见附录 D |"


def snapshot_path(report_date: str) -> Path:
    return SNAPSHOT_DIR / f"mainline_snapshot_{report_date}.json"


def save_lifecycle_snapshot(report_date: str, lifecycle: dict[str, pd.DataFrame]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    cols = [
        "industry",
        "mainline_grade",
        "stage",
        "status_explanation",
        "driver_type",
        "speed_type",
        "catalyst_rhythm",
        "leader_state",
        "valuation_temp",
        "short_term_state",
        "mainline_conclusion",
        "ret5",
        "ret20",
        "ret60",
        "drawdown20",
        "drawdown60",
        "above20",
        "above60",
        "amount5_60",
        "stage_score",
    ]
    frame = lifecycle["all"][cols].copy()
    frame.insert(0, "date", report_date)
    frame.to_json(snapshot_path(report_date), orient="records", force_ascii=False, indent=2)


def load_previous_snapshot(report_date: str) -> pd.DataFrame:
    for path in sorted(SNAPSHOT_DIR.glob("mainline_snapshot_*.json"), reverse=True):
        date_part = path.stem.replace("mainline_snapshot_", "")
        if date_part < report_date:
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            return pd.DataFrame(rows)
    return pd.DataFrame()


def load_snapshot_offsets(report_date: str, offsets: tuple[int, ...] = (1, 3, 5)) -> dict[int, pd.DataFrame]:
    # Display labels are review-window anchors:
    # T-1 = previous trading snapshot, T-3 = middle anchor of the recent 3-session window,
    # T-5 = middle anchor of the recent 5-session window. They are resolved from snapshot
    # order, not calendar-day arithmetic.
    window_anchor_index = {1: 1, 3: 2, 5: 4}
    paths = []
    for path in sorted(SNAPSHOT_DIR.glob("mainline_snapshot_*.json")):
        date_part = path.stem.replace("mainline_snapshot_", "")
        if date_part < report_date:
            paths.append(path)
    out = {}
    for offset in offsets:
        index = window_anchor_index.get(offset, offset)
        if len(paths) >= index:
            path = paths[-index]
            try:
                out[offset] = pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                out[offset] = pd.DataFrame()
        else:
            out[offset] = pd.DataFrame()
    return out


def build_recent_lifecycle_review(report_date: str, lifecycle: dict[str, pd.DataFrame], market_score: float) -> pd.DataFrame:
    snapshots = load_snapshot_offsets(report_date)
    today = lifecycle["mainline_overview"].copy()
    if today.empty:
        today = lifecycle["all"].sort_values("stage_score", ascending=False).head(14).copy()
    industries = today["industry"].head(14).tolist()
    rows = []
    for industry in industries:
        now = lifecycle["all"].set_index("industry").loc[industry]
        refs = {offset: snapshot_row(snapshots[offset], industry) for offset in [1, 3, 5]}
        levels = {offset: display_level(refs[offset].get("mainline_grade")) for offset in [1, 3, 5]}
        change_desc = lifecycle_change_description(now, refs)
        judgment, action = lifecycle_judgment(now, refs)
        early_type = early_mainline_signal_type(now, refs)
        priority_label = priority_review_label(now, early_type, market_score)
        rows.append(
            {
                "industry": industry,
                "today_level": display_level(now["mainline_grade"]),
                "t1_level": levels[1],
                "t3_level": levels[3],
                "t5_level": levels[5],
                "today_stage": now["stage"],
                "early_signal_type": early_type or "无",
                "change_desc": change_desc,
                "judgment": judgment,
                "action": action,
                "priority_label": priority_label,
            }
        )
    return pd.DataFrame(rows)


def early_mainline_signal_type(now: pd.Series, refs: dict[int, dict]) -> str:
    grade = now.get("mainline_grade")
    stage = now.get("stage")
    if stage == "企稳重估":
        return "企稳重估"
    if grade in ["A级主线", "B级主线"]:
        return ""
    previous_grades = [refs[offset].get("mainline_grade") for offset in [1, 3, 5] if refs[offset].get("mainline_grade")]
    from_cold = any(level in ["退潮主线", "低频监控", "暂不观察"] for level in previous_grades)
    if from_cold and grade == "C级观察" and now.get("ret5", 0) > 0:
        return "重新升温"
    if (
        grade == "C级观察"
        and stage not in ["弱势反弹", "确认后退潮", "退潮风险", "低频监控", "暂不观察"]
        and now.get("ret20", 0) > -0.03
        and now.get("drawdown60", -1) > -0.15
    ):
        return "C级结构修复"
    return ""


def priority_review_label(now: pd.Series, early_type: str, market_score: float) -> str:
    if (
        early_type in ["企稳重估", "重新升温", "C级结构修复"]
        and market_score >= 45
        and now.get("mainline_grade") not in ["A级主线", "B级主线"]
    ):
        return "优先复核"
    return "常规复核"


def snapshot_row(frame: pd.DataFrame, industry: str) -> dict:
    if frame.empty or "industry" not in frame.columns:
        return {}
    matched = frame[frame["industry"] == industry]
    if matched.empty:
        return {}
    return matched.iloc[0].to_dict()


def display_level(level) -> str:
    if pd.isna(level) or not level:
        return "无历史记录"
    return str(level)


def lifecycle_change_description(now: pd.Series, refs: dict[int, dict]) -> str:
    if not any(refs[offset].get("mainline_grade") for offset in [1, 3, 5]):
        return "首次记录，等待T-3/T-5复核"
    ordered = []
    for offset in [5, 3, 1]:
        level = refs[offset].get("mainline_grade")
        if level:
            ordered.append(display_level(level))
    ordered.append(display_level(now["mainline_grade"]))
    compact = []
    for level in ordered:
        if not compact or compact[-1] != level:
            compact.append(level)
    if len(compact) == 1:
        if now["mainline_grade"] == "C级观察" and now["stage"] == "弱势反弹":
            return "弱势反弹未晋级"
        return f"{compact[0]}连续保持"
    return " → ".join(compact)


def lifecycle_judgment(now: pd.Series, refs: dict[int, dict]) -> tuple[str, str]:
    t1, t3, t5 = refs[1], refs[3], refs[5]
    today_rank = grade_rank(now["mainline_grade"])
    previous_ranks = [grade_rank(ref.get("mainline_grade")) for ref in [t5, t3, t1] if ref.get("mainline_grade")]
    if not previous_ranks:
        return "首次记录", "继续观察"
    best_prev = min(previous_ranks) if previous_ranks else 9
    worst_prev = max(previous_ranks) if previous_ranks else 9
    drawdown_widened = any(
        ref.get("drawdown60") is not None and pd.notna(ref.get("drawdown60")) and now["drawdown60"] < float(ref.get("drawdown60")) - 0.03
        for ref in [t1, t3, t5]
    )
    width_fell = any(
        ref.get("above20") is not None and pd.notna(ref.get("above20")) and now["above20"] < float(ref.get("above20")) - 0.15
        for ref in [t1, t3]
    )
    fast_retreat = now["speed_type"] == "快主题" and (now["drawdown20"] < -0.08 or width_fell or now["ret5"] < -0.06)

    if now["mainline_grade"] in ["退潮主线", "低频监控"] or now["stage"] in ["确认后退潮", "退潮风险"]:
        if fast_retreat:
            return "快主题退潮预警", "风险复核"
        if all((ref.get("mainline_grade") in ["退潮主线", "低频监控"]) for ref in [t5, t3, t1] if ref):
            return "退潮延续", "低频监控"
        return "确认后退潮", "风险复核"
    if now["stage"] == "企稳重估":
        return "企稳重估", "继续观察，等待重新升温"
    if previous_ranks and today_rank < best_prev and now["ret20"] >= -0.03 and now["above20"] >= 0.45:
        return "晋级确认", "提高关注"
    if worst_prev >= 3 and now["mainline_grade"] == "C级观察" and now["ret5"] > 0.05 and now["amount5_60"] > 1.10:
        return "加速升温", "提高关注，但不追高"
    if previous_ranks and today_rank > best_prev and (drawdown_widened or width_fell or now["ret5"] < -0.03):
        return "降级预警", "降低关注"
    if now["mainline_grade"] == "C级观察" and now["stage"] == "弱势反弹" and now["ret20"] < 0 and now["ret60"] < 0 and now["ret5"] <= 0:
        return "弱势反弹失败", "降低关注"
    if now["mainline_grade"] in ["A级主线", "B级主线"] and now["ret20"] > 0 and now["drawdown60"] > -0.10 and not width_fell:
        return "延续确认", "保持研究"
    if now["ret5"] < 0 and now["drawdown60"] > -0.10 and not width_fell:
        return "分歧观察", "继续观察"
    if now["driver_type"] == "接力式催化型" and now["catalyst_rhythm"] == "接力延续" and now["drawdown60"] > -0.12:
        return "接力催化延续", "保持研究"
    return "分歧观察", "继续观察"


def recent_lifecycle_review_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 暂无 | 无历史记录 | 无历史记录 | 无历史记录 | 无历史记录 | NA | 无 | 暂无可比记录 | 分歧观察 | 继续观察 | 常规复核 |"
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['today_level']} | {r['t1_level']} | {r['t3_level']} | {r['t5_level']} | {r['today_stage']} | {r['early_signal_type']} | {r['change_desc']} | {r['judgment']} | {r['action']} | {r['priority_label']} |"
        )
    return "\n".join(rows)


def next_day_checklist(recent_review: pd.DataFrame, lifecycle: dict[str, pd.DataFrame]) -> str:
    if recent_review.empty:
        return "1. 暂无历史复核记录，优先观察今日 B/C/退潮主线是否延续。"
    rows = []
    ordered = recent_review.copy()
    if "priority_label" in ordered.columns:
        ordered["priority_order"] = (ordered["priority_label"] != "优先复核").astype(int)
        ordered = ordered.sort_values(["priority_order"]).drop(columns=["priority_order"])
    for i, (_, r) in enumerate(ordered.head(7).iterrows(), start=1):
        industry = r["industry"]
        judgment = r["judgment"]
        if r.get("priority_label") == "优先复核":
            text = f"{industry}：{r.get('early_signal_type', '早期主线')}，优先复核是否继续扩散、宽度是否改善、是否从早期信号升级为候选/确认。"
        elif judgment in ["延续确认", "接力催化延续"]:
            text = f"{industry}：{judgment}，复核60日峰值回撤是否继续可控、宽度是否稳定。"
        elif judgment in ["晋级确认", "加速升温"]:
            text = f"{industry}：{judgment}，复核是否继续扩散，避免单点脉冲。"
        elif judgment in ["确认后退潮", "快主题退潮预警", "退潮延续"]:
            text = f"{industry}：{judgment}，复核是否进入企稳重估，不能因单日反弹直接恢复候选。"
        elif judgment == "弱势反弹失败":
            text = f"{industry}：弱势反弹失败，复核是否继续降级为低频监控。"
        elif judgment == "企稳重估":
            text = f"{industry}：企稳重估，复核宽度和量能是否支持重新升温。"
        else:
            text = f"{industry}：{judgment}，复核5日收益、宽度和峰值回撤是否改善。"
        rows.append(f"{i}. {text}")
    return "\n".join(rows)



def build_yesterday_review(report_date: str, lifecycle: dict[str, pd.DataFrame]) -> pd.DataFrame:
    prev = load_previous_snapshot(report_date)
    if prev.empty:
        return pd.DataFrame()
    today = lifecycle["all"].set_index("industry")
    rows = []
    for _, old in prev.head(18).iterrows():
        industry = old["industry"]
        if industry not in today.index:
            rows.append(
                {
                    "industry": industry,
                    "prev_grade": old.get("mainline_grade", "NA"),
                    "prev_stage": old.get("stage", "NA"),
                    "today_perf": np.nan,
                    "drawdown_change": np.nan,
                    "catalyst": "暂不明确",
                    "today_stage": "样本不足",
                    "result": "样本不足",
                    "action": "降为低频监控",
                }
            )
            continue
        now = today.loc[industry]
        drawdown_change = float(now["drawdown60"]) - float(old.get("drawdown60", now["drawdown60"]))
        prev_retreat = old.get("mainline_grade") in ["退潮主线", "低频监控"] or old.get("stage") in ["退潮风险", "确认后退潮", "低频监控"]
        today_perf = float(now["ret5"]) if pd.notna(now["ret5"]) else np.nan
        if prev_retreat and pd.notna(today_perf) and today_perf <= -0.05:
            result = "加速退潮破位（深度破位）" if today_perf <= -0.08 else "加速退潮破位"
            rows.append(
                {
                    "industry": industry,
                    "prev_grade": old.get("mainline_grade", "NA"),
                    "prev_stage": old.get("stage", "NA"),
                    "today_perf": today_perf,
                    "drawdown_change": drawdown_change,
                    "catalyst": now.get("catalyst_rhythm", "暂不明确"),
                    "today_stage": "退潮风险",
                    "result": result,
                    "action": "风险复核",
                }
            )
            continue
        promoted = grade_rank(now["mainline_grade"]) < grade_rank(old.get("mainline_grade", "暂不观察"))
        retreated = now["mainline_grade"] in ["退潮主线", "低频监控"] or now["stage"] in ["确认后退潮", "退潮风险"]
        accelerated = now["speed_type"] == "快主题" and now["ret5"] > 0.08 and now["amount5_60"] > 1.20
        repaired = now["stage"] == "企稳重估" or (drawdown_change > 0 and now["ret5"] > 0)
        continued = old.get("mainline_grade") == now["mainline_grade"] or old.get("stage") == now["stage"]
        if now["mainline_grade"] in ["退潮主线", "低频监控"]:
            result = "延续退潮" if continued else "触发退潮"
            action = "低频监控" if now["mainline_grade"] == "低频监控" else "风险复核" if continued else "降级"
        elif now["mainline_grade"] == "企稳重估":
            result = "重新修复"
            action = "企稳重估"
        elif promoted:
            result = "晋级"
            action = "保持研究"
        elif accelerated:
            result = "加速"
            action = "保持研究"
        elif repaired:
            result = "重新修复"
            action = "保持观察"
        elif continued:
            result = "判断延续"
            action = "保持研究"
        else:
            result = "重新分层"
            action = "重新分层"
        if now["mainline_grade"] == "暂不观察" or now["stage"] == "暂不观察":
            action = "暂不研究"
            if result == "判断延续":
                result = "弱势延续"
        rows.append(
            {
                "industry": industry,
                "prev_grade": old.get("mainline_grade", "NA"),
                "prev_stage": old.get("stage", "NA"),
                "today_perf": today_perf,
                "drawdown_change": drawdown_change,
                "catalyst": now.get("catalyst_rhythm", "暂不明确"),
                "today_stage": now["stage"],
                "result": result,
                "action": action,
            }
        )
    return pd.DataFrame(rows)


def yesterday_review_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 首次运行 | NA | NA | NA | NA | NA | NA | 暂无昨日结构化快照，今日起开始记录 |"
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['prev_grade']} | {r['prev_stage']} | {pct(r['today_perf'])} | {pct(r['drawdown_change'])} | {r['catalyst']} | {r['today_stage']} | {r['result']} | {r['action']} |"
        )
    return "\n".join(rows)


def compact_yesterday_review_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 首次运行 | NA | NA | NA | NA | NA | 暂无昨日结构化快照，今日起开始记录 |"
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['prev_grade']} | {r['prev_stage']} | {pct(r['today_perf'])} | {pct(r['drawdown_change'])} | {r['today_stage']} | {r['result']} | {r['action']} |"
        )
    return "\n".join(rows)


def grade_rank(grade: str) -> int:
    return {"A级主线": 0, "B级主线": 1, "C级观察": 2, "企稳重估": 3, "退潮主线": 4, "低频监控": 5}.get(str(grade), 9)


def warming_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {warming_type(r)} | {r['warming_score']:.1f} | {pct(r['ret5'])} | {pct(r['ret20'])} | {int(r['outperform_days20'])}/20 | {pct(r['above20'])} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| NA | NA | NA | NA | NA | NA | NA | NA |"


def candidate_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['stage']} | {r['candidate_score']:.1f} | {pct(r['ret5'])} | {pct(r['ret20'])} | {pct(r['ret60'])} | {int(r['outperform_days20'])}/20 | {pct(r['drawdown20'])} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def confirmed_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['stage']} | {r['confirmed_score']:.1f} | {pct(r['ret5'])} | {pct(r['ret20'])} | {pct(r['ret60'])} | {pct(r['above20'])} | {pct(r['drawdown20'])} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def retreat_table(frame: pd.DataFrame) -> str:
    rows = []
    for _, r in frame.iterrows():
        rows.append(
            f"| {r['industry']} | {r['stage']} | {r['retreat_score']:.1f} | {r['confirmed_score_raw']:.1f} | {pct(r['ret5'])} | {pct(r['drawdown20'])} | {pct(r['above20'])} | {num(r['amount5_60'], 2)} | {r['risk_note']} |"
        )
    return "\n".join(rows) if rows else "| NA | NA | NA | NA | NA | NA | NA | NA | NA |"


def research_priority(row: pd.Series) -> str:
    pool = row.get("pool", "")
    if pool == "稳健中军观察池" and row.get("stock_score", 0) >= 65:
        return "重点研究"
    if pool == "低位修复/弱势反弹观察池":
        return "低位修复观察"
    if pool == "弹性成长观察池":
        return "普通观察"
    if pool == "过热/风险复核池":
        return "风险复核"
    return "普通观察"


def environment_adjusted_action(row: pd.Series, market_score: float) -> str:
    raw = row.get("action", "仅观察")
    if raw in ["行业退潮，暂缓", "风险复核", "估值异常", "过热不追"]:
        return raw
    if market_score >= 70:
        return raw if raw != "重点研究" else "可跟踪，等买点"
    if market_score >= 55:
        return "等回踩确认" if raw in ["重点研究", "等回踩"] else raw
    if market_score >= 40:
        return "仅研究观察/等回踩"
    return "仅保留观察，不新增行动"


def stock_table(frame: pd.DataFrame, market_score: float) -> str:
    if frame.empty:
        return "| NA | NA | NA | NA | NA | NA | NA | NA | NA | NA | NA |"
    rows = []
    for _, r in frame.iterrows():
        valuation = f"PE {num(r.get('pe'), 1)} / PB {num(r.get('pb'), 2)}"
        rows.append(
            f"| {r['symbol']} | {r.get('name', '')} | {r['industry']} | {r['industry_stage']} | {r['stock_score']:.1f} | {pct(r['ret_20d'])} | {pct(r['pivot_distance_pct'])} | {valuation} | {r['risk_tags']} | {research_priority(r)} | {environment_adjusted_action(r, market_score)} |"
        )
    return "\n".join(rows)


def etf_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "| 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 |"
    rows = []
    for _, r in frame.iterrows():
        etf_code = r.get("etf_code", "")
        proxy_text = r["proxy"]
        if etf_code:
            proxy_text = r["proxy"]
        rows.append(
            f"| {r['mainline_grade']} | {r['industry']} | {proxy_text} | {r['scene']} | {r['trend_state']} | {r['risk_note']} |"
        )
    return "\n".join(rows)


def carrier_table(frame: pd.DataFrame, market_score: float, carrier_type: str) -> str:
    if frame.empty:
        if carrier_type == "core":
            return "| 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 |"
        return "| 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 |"
    rows = []
    for _, r in frame.iterrows():
        if carrier_type == "core":
            rows.append(
                f"| {r['industry']} | {r['symbol']} | {r.get('name', '')} | {r['market_cap_tier']} | {r['role_tag']} | {r['trend_state']} | {r['valuation_temp']} | {r['fundamental_tag']} | {research_priority(r)} | {environment_adjusted_action(r, market_score)} |"
            )
        else:
            overheat = "严重过热" if r.get("ret_20d", 0) > 0.80 else "过热" if r.get("ret_20d", 0) > 0.50 else "偏离较大" if r.get("pivot_distance_pct", 0) > 0.05 else "未明显过热"
            rows.append(
                f"| {r['industry']} | {r['symbol']} | {r.get('name', '')} | {elastic_source(r)} | {r['trend_state']} | {overheat} | {r['risk_tags']} | {research_priority(r)} | {environment_adjusted_action(r, market_score)} |"
            )
    return "\n".join(rows)


def risk_carrier_table(frame: pd.DataFrame, market_score: float) -> str:
    if frame.empty:
        return "| 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 | 暂无 |"
    rows = []
    for _, r in frame.head(12).iterrows():
        rows.append(
            f"| {r['industry']} | {r['symbol']} | {r.get('name', '')} | {r['trend_state']} | {r['valuation_temp']} | {r['risk_tags']} | {r.get('impact_on_mainline', '主线质量待复核')} | 风险复核 | {environment_adjusted_action(r, market_score)} |"
        )
    return "\n".join(rows)


if __name__ == "__main__":
    main()
