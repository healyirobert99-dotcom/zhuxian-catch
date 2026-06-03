from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

import generate_daily_review as daily_review  # noqa: E402
import render_daily_review_html as html_review  # noqa: E402


DB_PATH = BASE / "data" / "a_stock_selector.sqlite3"
REPORT_DIR = BASE / "reports" / "daily_review"
STRICT_MIN_DAILY_ROWS = 5000
STRICT_MIN_CONCEPT_MEMBER_ROWS = 1000
REQUIRED_INDEX_CODES = {
    "上证指数": "000001.SH",
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "创业板指": "399006.SZ",
}
STRICT_MIN_CONCEPT_ROWS = 100


def main() -> None:
    args = parse_args()
    load_dotenv(BASE / ".env")
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise SystemExit("TUSHARE_TOKEN is required for automated daily review. Refusing to use stale local cache.")

    pro = daily_review.build_tushare_client(token)
    trade_date = normalize_trade_date(args.trade_date) if args.trade_date else latest_review_trade_date(pro)
    sync_stats = daily_review.ensure_daily_cache(trade_date, token, pro)
    concept_stats = daily_review.ensure_concept_cache(trade_date, token, pro)
    concept_member_stats = daily_review.ensure_concept_member_cache(trade_date, token)
    validate_complete_daily_cache(trade_date)
    validate_complete_concept_cache(trade_date)
    validate_complete_concept_member_cache(trade_date)
    validate_required_index_data(pro, trade_date)

    paths = daily_review.generate_daily_report(
        trade_date,
        token,
        pro,
        use_lifecycle_cache=not args.no_lifecycle_cache,
    )
    html_path = render_html_for_trade_date(trade_date)

    print("Daily review generated:")
    for path in paths:
        print(path)
    print(html_path)
    print(f"Concept cache rows: {concept_stats.get('concept_daily_rows', 'NA')}")
    print(f"Concept member rows: {concept_member_stats.get('concept_member_rows', 'NA')}")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the daily mainline research report and matching HTML page.")
    parser.add_argument("--trade-date", help="Optional trade date such as 20260601 or 2026-06-01.")
    parser.add_argument("--no-lifecycle-cache", action="store_true", help="Recompute lifecycle metrics instead of using cache.")
    return parser.parse_args()


def normalize_trade_date(value: str) -> str:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return text
    return pd.to_datetime(text).strftime("%Y%m%d")


def latest_review_trade_date(pro) -> str:
    today = date.today()
    tushare_date = latest_open_trade_date_from_tushare(pro, today)
    if tushare_date:
        return tushare_date
    raise SystemExit("Tushare trade calendar unavailable. Refusing to fall back to local cache for automation.")


def latest_open_trade_date_from_tushare(pro, today: date) -> str | None:
    start = (today - timedelta(days=21)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    try:
        cal = pro.trade_cal(exchange="", start_date=start, end_date=end)
    except Exception as exc:  # noqa: BLE001
        print(f"Tushare trade calendar unavailable: {exc}")
        return None
    if cal.empty or "is_open" not in cal.columns or "cal_date" not in cal.columns:
        return None
    open_days = cal[cal["is_open"].astype(int) == 1]["cal_date"].astype(str).sort_values()
    if open_days.empty:
        return None
    return open_days.iloc[-1]


def validate_complete_daily_cache(trade_date: str) -> None:
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as con:
        daily_count = con.execute("select count(*) from stock_daily where date = ?", (report_date,)).fetchone()[0]
        basic_count = con.execute("select count(*) from stock_daily_basic where date = ?", (report_date,)).fetchone()[0]
        bad_ohlcv = con.execute(
            """
            select count(*)
            from stock_daily
            where date = ?
              and (open is null or high is null or low is null or close is null or volume is null or amount is null)
            """,
            (report_date,),
        ).fetchone()[0]
    if daily_count < STRICT_MIN_DAILY_ROWS:
        raise SystemExit(
            f"Incomplete stock_daily cache for {report_date}: {daily_count} rows, "
            f"requires at least {STRICT_MIN_DAILY_ROWS}. Refusing to generate report."
        )
    if basic_count < STRICT_MIN_DAILY_ROWS:
        raise SystemExit(
            f"Incomplete stock_daily_basic cache for {report_date}: {basic_count} rows, "
            f"requires at least {STRICT_MIN_DAILY_ROWS}. Refusing to generate report."
        )
    if basic_count < int(daily_count * 0.95):
        raise SystemExit(
            f"Daily basic cache is materially smaller than daily cache for {report_date}: "
            f"stock_daily={daily_count}, stock_daily_basic={basic_count}. Refusing to generate report."
        )
    if bad_ohlcv:
        raise SystemExit(f"Found {bad_ohlcv} rows with missing OHLCV fields for {report_date}. Refusing to generate report.")


def validate_complete_concept_cache(trade_date: str) -> None:
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as con:
        concept_count = con.execute(
            """
            select count(*)
            from concept_daily d
            join concept_basic b on b.ts_code = d.ts_code
            where d.trade_date = ?
              and b.idx_type = '概念板块'
            """,
            (report_date,),
        ).fetchone()[0]
        bad_rows = con.execute(
            """
            select count(*)
            from concept_daily d
            join concept_basic b on b.ts_code = d.ts_code
            where d.trade_date = ?
              and b.idx_type = '概念板块'
              and (d.pct_change is null or d.up_num is null or d.down_num is null)
            """,
            (report_date,),
        ).fetchone()[0]
    if concept_count < STRICT_MIN_CONCEPT_ROWS:
        raise SystemExit(
            f"Incomplete concept_daily cache for {report_date}: {concept_count} rows, "
            f"requires at least {STRICT_MIN_CONCEPT_ROWS}. Refusing to generate report."
        )
    if bad_rows:
        raise SystemExit(f"Found {bad_rows} concept rows with missing pct_change/up_num/down_num for {report_date}. Refusing to generate report.")


def validate_complete_concept_member_cache(trade_date: str) -> None:
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as con:
        count = con.execute(
            """
            select count(*)
            from concept_member cm
            join concept_basic cb on cb.ts_code = cm.ts_code
            where cm.trade_date = ?
              and cb.idx_type = '概念板块'
            """,
            (report_date,),
        ).fetchone()[0]
    if count < STRICT_MIN_CONCEPT_MEMBER_ROWS:
        raise SystemExit(
            f"Incomplete concept_member cache for {report_date}: {count} rows, "
            f"requires at least {STRICT_MIN_CONCEPT_MEMBER_ROWS}. Refusing to generate report."
        )


def validate_required_index_data(pro, trade_date: str) -> None:
    missing = []
    start_date = (pd.to_datetime(trade_date) - pd.DateOffset(months=6)).strftime("%Y%m%d")
    for name, code in REQUIRED_INDEX_CODES.items():
        try:
            idx = daily_review.pro_query_with_retry(
                pro.index_daily,
                ts_code=code,
                start_date=start_date,
                end_date=trade_date,
            )
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name}({code}) 查询失败：{exc}")
            continue
        if idx.empty:
            missing.append(f"{name}({code}) 无数据")
            continue
        if "trade_date" not in idx.columns or "close" not in idx.columns or "pct_chg" not in idx.columns:
            missing.append(f"{name}({code}) 缺少必要字段")
            continue
        dates = idx["trade_date"].astype(str)
        if trade_date not in set(dates):
            latest = dates.max() if not dates.empty else "NA"
            missing.append(f"{name}({code}) 缺少当日指数数据，最新为 {latest}")
            continue
        if len(idx) < 60:
            missing.append(f"{name}({code}) 指数历史不足 60 日，无法计算 MA60")
    if missing:
        detail = "\n- ".join(missing)
        raise SystemExit(f"Required index data incomplete. Refusing to generate report.\n- {detail}")


def render_html_for_trade_date(trade_date: str) -> Path:
    report_date = pd.to_datetime(trade_date).strftime("%Y-%m-%d")
    source = REPORT_DIR / f"a_share_daily_review_{report_date}.md"
    output = source.with_suffix(".html")
    markdown = source.read_text(encoding="utf-8")
    output.write_text(html_review.render_html(markdown, source), encoding="utf-8")
    return output


if __name__ == "__main__":
    main()
