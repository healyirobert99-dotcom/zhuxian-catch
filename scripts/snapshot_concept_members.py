"""
概念成分股每日快照

每天调用一次，保存 concept_daily 中所有概念的成员股到 concept_member_snapshot。
首次全量拉取较慢（约30分钟/2147个概念），后续可增量。

用法:
    python scripts/snapshot_concept_members.py              # 全量快照
    python scripts/snapshot_concept_members.py --fast       # 只拉活跃概念（日报中出现过的）
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
RATE_LIMIT = 0.15  # 每秒约6-7次，安全的API调用频率


def create_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_member_snapshot (
            snapshot_date  TEXT NOT NULL,
            ts_code        TEXT NOT NULL,
            con_code       TEXT NOT NULL,
            con_name       TEXT,
            source         TEXT DEFAULT 'tushare',
            created_at     TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, ts_code, con_code)
        )
        """
    )
    con.commit()


def get_all_concept_codes(con: sqlite3.Connection) -> list[str]:
    """获取 concept_daily 中所有概念代码。"""
    rows = con.execute("SELECT DISTINCT ts_code FROM concept_daily ORDER BY ts_code").fetchall()
    return [r[0] for r in rows]


def get_active_concept_codes(con: sqlite3.Connection, lookback_days: int = 30) -> list[str]:
    """获取在过去 N 天 concept_daily 中有数据的概念（活跃概念）。"""
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT DISTINCT ts_code FROM concept_daily WHERE trade_date >= ? ORDER BY ts_code",
        (cutoff,),
    ).fetchall()
    return [r[0] for r in rows]


def get_already_snapshot_codes(con: sqlite3.Connection, date: str) -> set[str]:
    """获取当天已经快照过的概念代码。"""
    rows = con.execute(
        "SELECT DISTINCT ts_code FROM concept_member_snapshot WHERE snapshot_date = ?",
        (date,),
    ).fetchall()
    return {r[0] for r in rows}


def snapshot_concept_members(
    con: sqlite3.Connection,
    codes: list[str],
    snapshot_date: str,
    pro,
    batch_size: int = 50,
) -> dict:
    """
    批量保存概念成员快照。
    返回 {"saved": N, "skipped": N, "errors": N}
    """
    already = get_already_snapshot_codes(con, snapshot_date)
    to_fetch = [c for c in codes if c not in already]
    if not to_fetch:
        return {"saved": 0, "skipped": len(codes), "errors": 0}

    saved = 0
    errors = 0
    total = len(to_fetch)
    now_iso = datetime.now().isoformat()

    for i, code in enumerate(to_fetch):
        try:
            df = pro.ths_member(ts_code=code)
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  [WARN] {code}: {e}")
            time.sleep(RATE_LIMIT)
            continue

        if df is None or df.empty:
            time.sleep(RATE_LIMIT)
            continue

        # 标准化列名
        if "con_code" in df.columns and "con_name" in df.columns:
            rows = []
            for _, row in df.iterrows():
                rows.append(
                    (
                        snapshot_date,
                        code,
                        str(row["con_code"]),
                        str(row.get("con_name", "")),
                        "tushare",
                        now_iso,
                    )
                )
            con.executemany(
                "INSERT OR IGNORE INTO concept_member_snapshot "
                "(snapshot_date, ts_code, con_code, con_name, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            saved += 1

        # 进度和速率控制
        if (i + 1) % batch_size == 0:
            con.commit()
            print(f"  ... {i + 1}/{total} ({saved} saved, {errors} err)")

        time.sleep(RATE_LIMIT)

    con.commit()
    return {"saved": saved, "skipped": len(codes) - len(to_fetch), "errors": errors}


def generate_change_log(con: sqlite3.Connection, date: str) -> pd.DataFrame:
    """对比今天和上一交易日的快照，生成成分变化日志。"""
    # 找上一个快照日期
    prev = con.execute(
        "SELECT MAX(snapshot_date) FROM concept_member_snapshot WHERE snapshot_date < ?",
        (date,),
    ).fetchone()
    if not prev or not prev[0]:
        print("  无历史快照可对比，跳过变化日志")
        return pd.DataFrame()

    prev_date = prev[0]
    today = con.execute(
        "SELECT ts_code, COUNT(*) as cnt FROM concept_member_snapshot WHERE snapshot_date = ? GROUP BY ts_code",
        (date,),
    ).fetchall()
    yesterday = con.execute(
        "SELECT ts_code, COUNT(*) as cnt FROM concept_member_snapshot WHERE snapshot_date = ? GROUP BY ts_code",
        (prev_date,),
    ).fetchall()

    today_map = {r[0]: r[1] for r in today}
    yesterday_map = {r[0]: r[1] for r in yesterday}

    rows = []
    all_codes = set(today_map.keys()) | set(yesterday_map.keys())
    for code in sorted(all_codes):
        t_cnt = today_map.get(code, 0)
        y_cnt = yesterday_map.get(code, 0)
        if t_cnt != y_cnt:
            change_pct = ((t_cnt - y_cnt) / y_cnt * 100) if y_cnt > 0 else float("inf")
            rows.append(
                {
                    "date": date,
                    "ts_code": code,
                    "member_count": t_cnt,
                    "prev_count": y_cnt,
                    "change": t_cnt - y_cnt,
                    "change_pct": change_pct,
                }
            )

    df = pd.DataFrame(rows)
    if not df.empty:
        # 保存到数据库
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS concept_member_change_log (
                date TEXT, ts_code TEXT, member_count INTEGER,
                prev_count INTEGER, change INTEGER, change_pct REAL,
                PRIMARY KEY (date, ts_code)
            )
            """
        )
        con.executemany(
            "INSERT OR REPLACE INTO concept_member_change_log VALUES (?, ?, ?, ?, ?, ?)",
            df[["date", "ts_code", "member_count", "prev_count", "change", "change_pct"]].values.tolist(),
        )
        con.commit()
        big_changes = df[df["change_pct"].abs() > 15]
        if not big_changes.empty:
            print(f"  成分变化>15%的概念: {len(big_changes)}个")

    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="概念成分股每日快照")
    parser.add_argument("--fast", action="store_true", help="只拉活跃概念（近30天有数据）")
    parser.add_argument("--date", help="快照日期 (YYYY-MM-DD), 默认今天")
    parser.add_argument("--limit", type=int, default=0, help="限制拉取概念数（测试用）")
    args = parser.parse_args()

    snapshot_date = args.date or datetime.now().strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_PATH)

    create_table(con)

    if args.fast:
        codes = get_active_concept_codes(con)
        print(f"活跃概念: {len(codes)}个（近30日）")
    else:
        codes = get_all_concept_codes(con)
        print(f"全部概念: {len(codes)}个")

    if args.limit > 0:
        codes = codes[: args.limit]
        print(f"限制为前 {args.limit} 个")

    pro = ts.pro_api(TUSHARE_TOKEN)
    start = time.time()
    result = snapshot_concept_members(con, codes, snapshot_date, pro)
    elapsed = time.time() - start

    print(
        f"\n完成: saved={result['saved']}, skipped={result['skipped']}, "
        f"errors={result['errors']}, 耗时 {elapsed:.0f}s"
    )

    # 生成变化日志
    print("生成成分变化日志...")
    generate_change_log(con, snapshot_date)

    con.close()


if __name__ == "__main__":
    main()
