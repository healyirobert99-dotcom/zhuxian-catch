"""
ETF 历史日线数据补齐脚本

从 Tushare 下载所有 A 股 ETF 日线数据，补齐到 stock_daily 的同一历史区间。
数据存入 stock_daily 表（ETF 的 symbol 格式为 xxx.SH/.SZ，与 Tushare ts_code 一致）。

用法:
    python scripts/backfill_etf_daily.py           # 全量补齐
    python scripts/backfill_etf_daily.py --today   # 仅补齐今天
    python scripts/backfill_etf_daily.py --test    # 测试（仅10天）
"""

import argparse
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts

BASE = Path(__file__).resolve().parents[1]
DB_PATH = BASE / "data" / "a_stock_selector.sqlite3"
TUSHARE_TOKEN = "869490f6f46b978b30f96d8fd5830ef85a51e5ca7ab6a763ec71f1e3"
RATE_LIMIT = 0.2  # 每秒 5 次


def get_trading_dates(pro, start: str, end: str) -> list[str]:
    """获取交易日列表"""
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    return sorted(df["cal_date"].tolist())


def get_existing_dates(con: sqlite3.Connection) -> set[str]:
    """获取 stock_daily 中已有 ETF 数据的日期"""
    rows = con.execute(
        "SELECT DISTINCT date FROM stock_daily WHERE symbol LIKE '%.SH' OR symbol LIKE '%.SZ'"
    ).fetchall()
    # 只看 ETF（fund 类的 symbol）
    # 简单方法：检查是否有 ETF 数据的那天
    rows = con.execute(
        "SELECT DISTINCT date FROM stock_daily WHERE symbol LIKE '5%' AND length(symbol)=9"
    ).fetchall()
    return {r[0] for r in rows}


def get_completed_dates(con: sqlite3.Connection) -> set[str]:
    """通过 etf_sync_status 表获取已完成的日期"""
    try:
        cur = con.execute("SELECT trade_date FROM etf_sync_status WHERE status='done'")
        return {r[0] for r in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


def mark_completed(con: sqlite3.Connection, trade_date: str, rows: int) -> None:
    con.execute(
        "INSERT OR REPLACE INTO etf_sync_status (trade_date, status, rows, synced_at) VALUES (?, 'done', ?, ?)",
        (trade_date, rows, datetime.now().isoformat()),
    )
    con.commit()


def download_and_store(
    pro, con: sqlite3.Connection, trade_date: str
) -> int:
    """下载某一天的所有 ETF 日线数据并存入 stock_daily。"""
    try:
        df = pro.fund_daily(trade_date=trade_date, market="E")
    except Exception as e:
        print(f"  [ERR] {trade_date}: {e}")
        return 0

    if df is None or df.empty:
        return 0

    df = df.copy()
    # 过滤掉 REIT、货币 ETF 等（可选）
    # 标准化字段
    df["symbol"] = df["ts_code"]
    df["date"] = df["trade_date"]
    df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    # 过滤掉无效数据
    df = df[df["close"].notna() & (df["close"] > 0)]

    if df.empty:
        return 0

    rows = 0
    for _, r in df.iterrows():
        try:
            con.execute(
                """INSERT OR REPLACE INTO stock_daily 
                   (symbol, date, open, high, low, close, volume, amount, pct_chg, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'tushare_fund_daily')""",
                (
                    r["symbol"],
                    r["date"],
                    float(r["open"]) if pd.notna(r["open"]) else None,
                    float(r["high"]) if pd.notna(r["high"]) else None,
                    float(r["low"]) if pd.notna(r["low"]) else None,
                    float(r["close"]),
                    float(r["volume"]) if pd.notna(r["volume"]) else None,
                    float(r["amount"]) if pd.notna(r["amount"]) else None,
                    float(r["pct_chg"]) if pd.notna(r["pct_chg"]) else None,
                ),
            )
            rows += 1
        except Exception:
            continue

    con.commit()
    return rows


def main():
    parser = argparse.ArgumentParser(description="ETF 日线数据补齐")
    parser.add_argument("--today", action="store_true", help="仅补齐今天")
    parser.add_argument("--test", action="store_true", help="测试模式（仅10天）")
    parser.add_argument("--start", default="2019-07-04", help="起始日期")
    parser.add_argument("--end", help="结束日期，默认今天")
    parser.add_argument("--batch-size", type=int, default=50, help="每批次提交多少天后commit")
    args = parser.parse_args()

    pro = ts.pro_api(TUSHARE_TOKEN)
    con = sqlite3.connect(DB_PATH)

    # 创建同步状态表
    con.execute(
        """CREATE TABLE IF NOT EXISTS etf_sync_status (
            trade_date TEXT PRIMARY KEY, status TEXT, rows INTEGER, synced_at TEXT)"""
    )
    con.commit()

    end_date = args.end or datetime.now().strftime("%Y%m%d")

    if args.today:
        trade_date = datetime.now().strftime("%Y%m%d")
        rows = download_and_store(pro, con, trade_date)
        mark_completed(con, trade_date, rows)
        print(f"今日 {trade_date}: {rows} 条 ETF 日线")
        con.close()
        return

    # 获取交易日并排除已完成的
    trading_dates = get_trading_dates(pro, args.start, end_date)
    completed = get_completed_dates(con)

    if args.test:
        trading_dates = trading_dates[:10]

    to_fetch = [d for d in trading_dates if d not in completed]
    print(f"总交易日: {len(trading_dates)}, 已完成: {len(completed)}, 待补齐: {len(to_fetch)}")

    if not to_fetch:
        print("全部完成!")
        con.close()
        return

    total_rows = 0
    start_time = time.time()
    batch_count = 0

    for i, trade_date in enumerate(to_fetch):
        rows = download_and_store(pro, con, trade_date)
        if rows > 0:
            mark_completed(con, trade_date, rows)
            total_rows += rows

        batch_count += 1
        if batch_count % args.batch_size == 0:
            elapsed = time.time() - start_time
            rate = batch_count / elapsed * 60
            eta = (len(to_fetch) - batch_count) / rate
            print(
                f"  [{batch_count}/{len(to_fetch)}] {trade_date}: {rows}条 "
                f"| 累计 {total_rows} | 速率 {rate:.0f}天/分 | 预计剩余 {eta:.0f}分"
            )

        # 速率控制
        if i % 10 == 9:  # 每10次多休息一下
            time.sleep(0.5)
        else:
            time.sleep(RATE_LIMIT)

    elapsed = time.time() - start_time
    print(f"\n补齐完成: {total_rows} 条, {len(to_fetch)} 天, 耗时 {elapsed/60:.1f} 分")
    con.close()


if __name__ == "__main__":
    main()
