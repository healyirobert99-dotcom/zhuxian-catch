from __future__ import annotations

import argparse
import multiprocessing as mp
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


BASE = Path(__file__).resolve().parents[1]
DB_PATH = BASE / "data" / "a_stock_selector.sqlite3"
CATALYST_DIR = BASE / "data" / "catalysts"
CATALYST_TITLES_PATH = CATALYST_DIR / "catalyst_titles.csv"
CATALYST_KEYWORDS_PATH = BASE / "config" / "catalyst_keywords.csv"

OUTPUT_COLUMNS = [
    "date",
    "source_type",
    "source_name",
    "title",
    "summary",
    "related_industry",
    "related_concept",
]


@dataclass
class SyncResult:
    fetched_rows: int
    appended_rows: int
    output_path: Path
    source_notes: list[str]


def main() -> None:
    args = parse_args()
    result = sync_catalysts(
        days=args.days,
        end_date=args.end_date,
        source_timeout=args.source_timeout,
        include_notices=args.include_notices,
    )
    print(f"Catalyst titles synced: fetched={result.fetched_rows}, appended={result.appended_rows}")
    print(result.output_path)
    for note in result.source_notes:
        print(note)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync lightweight catalyst titles for daily mainline review.")
    parser.add_argument("--days", type=int, default=5, help="Lookback calendar days. Default: 5.")
    parser.add_argument("--end-date", help="End date such as 20260605 or 2026-06-05. Defaults to today.")
    parser.add_argument("--source-timeout", type=int, default=12, help="Seconds before a single source is skipped. Default: 12.")
    parser.add_argument("--include-notices", action="store_true", help="Also fetch Eastmoney announcements. Slower; disabled by default.")
    return parser.parse_args()


def sync_catalysts(
    days: int = 5,
    end_date: str | None = None,
    source_timeout: int = 12,
    include_notices: bool = False,
) -> SyncResult:
    CATALYST_DIR.mkdir(parents=True, exist_ok=True)
    end_ts = pd.Timestamp.today().normalize() if not end_date else pd.to_datetime(end_date).normalize()
    start_ts = end_ts - pd.Timedelta(days=max(days - 1, 0))
    universe = load_match_universe()

    frames: list[pd.DataFrame] = []
    notes: list[str] = []
    sources = [
        ("财联社电报", fetch_cls_telegraph),
        ("东方财富快讯", fetch_eastmoney_briefs),
        ("新闻联播", fetch_cctv_news),
    ]
    if include_notices:
        sources.append(("东方财富公告", fetch_notices))

    for name, fetcher in sources:
        frame, error = fetch_with_timeout(fetcher, start_ts, end_ts, source_timeout)
        if error:
            notes.append(f"{name}: {error}")
            continue
        if frame.empty:
            notes.append(f"{name}: no rows")
            continue
        frames.append(frame)
        notes.append(f"{name}: {len(frame)} rows")

    fetched = normalize_source_frames(frames)
    if not fetched.empty:
        fetched = enrich_related_entities(fetched, universe)
        fetched = fetched[fetched["title"].astype(str).str.len() > 0]
        fetched = fetched.drop_duplicates(subset=["date", "source_type", "source_name", "title"])

    existing = read_existing_titles()
    combined = merge_titles(existing, fetched)
    combined.to_csv(CATALYST_TITLES_PATH, index=False)
    appended = len(combined) - len(existing)
    return SyncResult(
        fetched_rows=len(fetched),
        appended_rows=max(appended, 0),
        output_path=CATALYST_TITLES_PATH,
        source_notes=notes,
    )


def fetch_with_timeout(fetcher, start_ts: pd.Timestamp, end_ts: pd.Timestamp, timeout: int) -> tuple[pd.DataFrame, str | None]:
    queue: mp.Queue = mp.Queue()
    proc = mp.Process(target=_fetch_worker, args=(fetcher.__name__, start_ts.isoformat(), end_ts.isoformat(), queue))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(2)
        return pd.DataFrame(columns=OUTPUT_COLUMNS), f"timeout after {timeout}s"
    if queue.empty():
        return pd.DataFrame(columns=OUTPUT_COLUMNS), "no response"
    payload = queue.get()
    if payload.get("error"):
        return pd.DataFrame(columns=OUTPUT_COLUMNS), f"failed ({payload['error']})"
    return payload.get("frame", pd.DataFrame(columns=OUTPUT_COLUMNS)), None


def _fetch_worker(fetcher_name: str, start_iso: str, end_iso: str, queue: mp.Queue) -> None:
    fetcher = globals()[fetcher_name]
    try:
        frame = fetcher(pd.Timestamp(start_iso), pd.Timestamp(end_iso))
    except Exception as exc:  # noqa: BLE001
        queue.put({"error": str(exc), "frame": pd.DataFrame(columns=OUTPUT_COLUMNS)})
        return
    queue.put({"error": None, "frame": frame})


def fetch_cls_telegraph(start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    import akshare as ak

    raw = ak.stock_info_global_cls(symbol="重点")
    return normalize_generic_news(raw, source_type="产业", source_name="财联社电报", start_ts=start_ts, end_ts=end_ts)


def fetch_eastmoney_briefs(start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    import akshare as ak

    raw = ak.stock_info_global_em()
    return normalize_generic_news(raw, source_type="产业", source_name="东方财富快讯", start_ts=start_ts, end_ts=end_ts)


def fetch_cctv_news(start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    import akshare as ak

    rows = []
    for dt in pd.date_range(start_ts, end_ts, freq="D"):
        try:
            raw = ak.news_cctv(date=dt.strftime("%Y%m%d"))
        except Exception:  # noqa: BLE001
            continue
        normalized = normalize_generic_news(raw, source_type="政策", source_name="新闻联播", start_ts=dt, end_ts=dt)
        if not normalized.empty:
            rows.append(normalized)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUTPUT_COLUMNS)


def fetch_notices(start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    import akshare as ak

    rows = []
    for dt in pd.date_range(start_ts, end_ts, freq="D"):
        try:
            raw = ak.stock_notice_report(symbol="全部", date=dt.strftime("%Y%m%d"))
        except Exception:  # noqa: BLE001
            continue
        normalized = normalize_generic_news(raw, source_type="公告", source_name="东方财富公告", start_ts=dt, end_ts=dt)
        if not normalized.empty:
            rows.append(normalized)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=OUTPUT_COLUMNS)


def normalize_generic_news(
    raw: pd.DataFrame,
    *,
    source_type: str,
    source_name: str,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    frame = raw.copy()
    title_col = first_existing_column(frame, ["标题", "title", "内容", "摘要", "新闻标题", "公告标题", "报告名称"])
    summary_col = first_existing_column(frame, ["摘要", "内容", "summary", "简介", "正文", "新闻内容", "公告内容"])
    date_col = first_existing_column(frame, ["日期", "时间", "发布时间", "公告日期", "datetime", "date"])
    if not title_col:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    dates = parse_source_dates(frame[date_col], fallback=end_ts) if date_col else pd.Series([end_ts] * len(frame))
    summary = frame[summary_col].astype(str) if summary_col and summary_col != title_col else ""
    out = pd.DataFrame(
        {
            "date": dates.dt.strftime("%Y-%m-%d"),
            "source_type": source_type,
            "source_name": source_name,
            "title": frame[title_col].astype(str).str.replace("\n", " ", regex=False).str.strip(),
            "summary": summary,
            "related_industry": "",
            "related_concept": "",
        }
    )
    if isinstance(out["summary"], pd.Series):
        out["summary"] = out["summary"].astype(str).str.replace("\n", " ", regex=False).str.strip()
    mask = (pd.to_datetime(out["date"]) >= start_ts) & (pd.to_datetime(out["date"]) <= end_ts)
    return out[mask]


def parse_source_dates(values: pd.Series, fallback: pd.Timestamp) -> pd.Series:
    text = values.astype(str)
    parsed = pd.to_datetime(text, errors="coerce")
    parsed = parsed.fillna(fallback)
    return parsed.dt.normalize()


def first_existing_column(frame: pd.DataFrame, candidates: list[str]) -> str | None:
    for col in candidates:
        if col in frame.columns:
            return col
    lowered = {str(col).lower(): col for col in frame.columns}
    for col in candidates:
        found = lowered.get(col.lower())
        if found is not None:
            return found
    return None


def normalize_source_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    normalized = pd.concat(frames, ignore_index=True)
    for col in OUTPUT_COLUMNS:
        if col not in normalized.columns:
            normalized[col] = ""
    return normalized[OUTPUT_COLUMNS].fillna("")


def load_match_universe() -> dict[str, list[str]]:
    industries: list[str] = []
    concepts: list[str] = []
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as con:
            try:
                industries = [
                    row[0]
                    for row in con.execute(
                        "select distinct industry from stock_basic where industry is not null and industry != ''"
                    ).fetchall()
                ]
            except sqlite3.Error:
                industries = []
            try:
                concepts = [
                    row[0]
                    for row in con.execute(
                        "select distinct name from concept_basic where name is not null and name != ''"
                    ).fetchall()
                ]
            except sqlite3.Error:
                concepts = []
    keywords = read_keyword_list()
    return {
        "industry": sorted(set(str(x) for x in industries if len(str(x)) >= 2), key=len, reverse=True),
        "concept": sorted(set(str(x) for x in concepts if len(str(x)) >= 2), key=len, reverse=True),
        "keyword": keywords,
    }


def read_keyword_list() -> list[str]:
    if not CATALYST_KEYWORDS_PATH.exists():
        return []
    try:
        frame = pd.read_csv(CATALYST_KEYWORDS_PATH)
    except Exception:  # noqa: BLE001
        return []
    if "keyword" not in frame.columns:
        return []
    return sorted(set(frame["keyword"].dropna().astype(str)), key=len, reverse=True)


def enrich_related_entities(frame: pd.DataFrame, universe: dict[str, list[str]]) -> pd.DataFrame:
    checked = frame.copy()
    checked["related_industry"] = checked.apply(
        lambda row: row["related_industry"] or first_match(row["title"], universe["industry"]),
        axis=1,
    )
    checked["related_concept"] = checked.apply(
        lambda row: row["related_concept"] or first_match(row["title"], universe["concept"]) or first_match(row["title"], universe["keyword"]),
        axis=1,
    )
    return checked


def first_match(text: str, candidates: list[str]) -> str:
    title = str(text)
    for item in candidates:
        if item and item in title:
            return item
    return ""


def read_existing_titles() -> pd.DataFrame:
    if not CATALYST_TITLES_PATH.exists():
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    try:
        frame = pd.read_csv(CATALYST_TITLES_PATH)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    for col in OUTPUT_COLUMNS:
        if col not in frame.columns:
            frame[col] = ""
    return frame[OUTPUT_COLUMNS].fillna("")


def merge_titles(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    frames = [frame for frame in [existing, fetched] if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    combined = pd.concat(frames, ignore_index=True)
    for col in OUTPUT_COLUMNS:
        if col not in combined.columns:
            combined[col] = ""
    combined = combined[OUTPUT_COLUMNS].fillna("")
    combined = combined.drop_duplicates(subset=["date", "source_type", "source_name", "title"], keep="last")
    return combined.sort_values(["date", "source_type", "source_name", "title"], ascending=[False, True, True, True])


if __name__ == "__main__":
    main()
