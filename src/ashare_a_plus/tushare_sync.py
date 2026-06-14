from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .sqlite_store import SQLiteStore


@dataclass(frozen=True)
class TushareSyncConfig:
    token: str
    db_path: Path
    start: str
    end: str
    sleep_seconds: float = 0.15
    retries: int = 3
    adjusted: bool = False
    skip_existing: bool = True


def default_config(token: str | None = None, db_path: Path | None = None) -> TushareSyncConfig:
    base = Path(__file__).resolve().parents[2]
    return TushareSyncConfig(
        token=token_from_env_or_arg(token),
        db_path=db_path or base / "data" / "a_stock_selector.sqlite3",
        start="2021-01-01",
        end=pd.Timestamp.today().date().isoformat(),
    )


def token_from_env_or_arg(token: str | None) -> str:
    value = token or os.environ.get("TUSHARE_TOKEN")
    if not value:
        raise RuntimeError("Missing Tushare token. Pass --token or set TUSHARE_TOKEN.")
    return value


class TushareSync:
    def __init__(self, config: TushareSyncConfig | None = None):
        self.config = config or default_config()
        self.store = SQLiteStore(self.config.db_path)
        import tushare as ts  # type: ignore

        ts.set_token(self.config.token)
        self.pro = ts.pro_api(self.config.token)

    def sync(self) -> dict:
        stock_basic = self._fetch_stock_basic()
        self.store.upsert_stock_basic(stock_basic)
        trade_days = self._fetch_trade_days()
        print(f"stock_basic: {len(stock_basic)} active rows", flush=True)
        print(f"trade_days: {len(trade_days)} from {self.config.start} to {self.config.end}", flush=True)

        synced_days = 0
        synced_rows = 0
        synced_basic_rows = 0
        for i, trade_date in enumerate(trade_days, start=1):
            iso_date = pd.to_datetime(trade_date).date().isoformat()
            has_daily = self.store.daily_row_count(iso_date) >= 3000
            has_daily_basic = self.store.daily_basic_row_count(iso_date) >= 3000
            if self.config.skip_existing and has_daily and has_daily_basic:
                if i == 1 or i % 20 == 0 or i == len(trade_days):
                    print(f"skip {i}/{len(trade_days)} existing {iso_date}", flush=True)
                continue
            daily = _empty_daily() if self.config.skip_existing and has_daily else self._fetch_daily_adjusted(trade_date)
            daily_basic = (
                _empty_daily_basic()
                if self.config.skip_existing and has_daily_basic
                else self._fetch_daily_basic(trade_date)
            )
            self.store.upsert_daily(daily)
            self.store.upsert_daily_basic(daily_basic)
            synced_days += 1
            synced_rows += len(daily)
            synced_basic_rows += len(daily_basic)
            if i == 1 or i % 20 == 0 or i == len(trade_days):
                print(f"synced {i}/{len(trade_days)} days, rows={synced_rows}, daily_basic_rows={synced_basic_rows}", flush=True)
            time.sleep(self.config.sleep_seconds)
        stats = self.store.stats()
        stats.update({"synced_days": synced_days, "synced_rows": synced_rows, "synced_basic_rows": synced_basic_rows})
        return stats

    def _call(self, func_name: str, **kwargs) -> pd.DataFrame:
        func = getattr(self.pro, func_name)
        last_exc: Exception | None = None
        for attempt in range(1, self.config.retries + 1):
            try:
                return func(**kwargs)
            except Exception as exc:  # pragma: no cover - network tolerance
                last_exc = exc
                wait = self.config.sleep_seconds * attempt * 4
                print(f"{func_name} failed attempt {attempt}: {exc}; sleep {wait:.1f}s", flush=True)
                time.sleep(wait)
        raise RuntimeError(f"Tushare {func_name} failed after {self.config.retries} retries") from last_exc

    def _fetch_stock_basic(self) -> pd.DataFrame:
        df = self._call(
            "stock_basic",
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,industry,list_date",
        )
        df = df.copy()
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        df["name"] = df["name"].astype(str)
        upper_name = df["name"].str.upper()
        df["is_st"] = upper_name.str.contains("ST", regex=False).astype(int)
        df["is_delist_risk"] = df["name"].str.contains("退", regex=False).astype(int)
        df["is_suspended"] = 0
        ordinary = df["symbol"].str.startswith(("00", "30", "60", "68"))
        return df.loc[ordinary, ["symbol", "ts_code", "name", "industry", "list_date", "is_st", "is_delist_risk", "is_suspended"]]

    def _fetch_trade_days(self) -> list[str]:
        df = self._call(
            "trade_cal",
            exchange="SSE",
            start_date=self.config.start.replace("-", ""),
            end_date=self.config.end.replace("-", ""),
            is_open="1",
            fields="cal_date,is_open",
        )
        return df["cal_date"].astype(str).sort_values().tolist()

    def _fetch_daily_adjusted(self, trade_date: str) -> pd.DataFrame:
        daily = self._call("daily", trade_date=trade_date)
        if daily.empty:
            return _empty_daily()
        if self.config.adjusted:
            adj = self._call("adj_factor", trade_date=trade_date)
            df = daily.merge(adj[["ts_code", "adj_factor"]], on="ts_code", how="left")
            df = df.dropna(subset=["adj_factor"]).copy()
        else:
            df = daily.copy()
            df["adj_factor"] = 1.0
        if df.empty:
            return _empty_daily()

        # Tushare amount is in thousand yuan and volume is in hands.
        # raw * adj_factor is a continuous back-adjusted style series.
        df["symbol"] = df["ts_code"].str.slice(0, 6)
        df["date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        df["raw_open"] = pd.to_numeric(df["open"], errors="coerce")
        df["raw_high"] = pd.to_numeric(df["high"], errors="coerce")
        df["raw_low"] = pd.to_numeric(df["low"], errors="coerce")
        df["raw_close"] = pd.to_numeric(df["close"], errors="coerce")
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
        df["open"] = df["raw_open"] * df["adj_factor"]
        df["high"] = df["raw_high"] * df["adj_factor"]
        df["low"] = df["raw_low"] * df["adj_factor"]
        df["close"] = df["raw_close"] * df["adj_factor"]
        df["volume"] = pd.to_numeric(df["vol"], errors="coerce")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce") * 1000
        df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
        df["source"] = "tushare_hfq" if self.config.adjusted else "tushare_raw"
        cols = [
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
        return df[cols].dropna(subset=["open", "high", "low", "close", "volume", "amount"])

    def _fetch_daily_basic(self, trade_date: str) -> pd.DataFrame:
        df = self._call(
            "daily_basic",
            trade_date=trade_date,
            fields="ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,ps,total_mv,circ_mv",
        )
        if df.empty:
            return _empty_daily_basic()
        df = df.copy()
        df["symbol"] = df["ts_code"].str.slice(0, 6)
        df["date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        for col in ["turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df[["symbol", "date", "turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"]]

    def sync_concept_basic(self) -> int:
        """
        Fetch Eastmoney concept board metadata from Tushare dc_index.
        """
        fields = "ts_code,name,idx_type"
        df = self._call("dc_index", idx_type="概念板块", fields=fields)
        if df.empty:
            return 0
        df = _ensure_columns(df.copy(), ["ts_code", "name", "idx_type"])
        df["idx_type"] = df["idx_type"].fillna("概念板块")
        df = df[df["idx_type"] == "概念板块"].copy()
        out = df[["ts_code", "name", "idx_type"]].dropna(subset=["ts_code", "name"])
        self.store.upsert_concept_basic(out)
        return len(out)

    def sync_concept_daily(self, start_date: str, end_date: str) -> int:
        """
        Fetch Eastmoney concept board daily snapshots from Tushare dc_index.

        The endpoint is date-oriented. We loop over open trade days, keep only
        idx_type='概念板块', and write concept_daily.
        """
        trade_days = self._fetch_trade_days_for_range(start_date, end_date)
        total = 0
        for i, trade_date in enumerate(trade_days, start=1):
            iso_date = pd.to_datetime(trade_date).date().isoformat()
            if self.config.skip_existing and self.store.concept_daily_row_count(iso_date) >= 100:
                if i == 1 or i % 20 == 0 or i == len(trade_days):
                    print(f"skip concept_daily {i}/{len(trade_days)} existing {iso_date}", flush=True)
                continue
            df = self._call("dc_index", trade_date=trade_date, idx_type="概念板块")
            out = normalize_concept_daily(df, trade_date)
            self.store.upsert_concept_daily(out)
            total += len(out)
            if i == 1 or i % 20 == 0 or i == len(trade_days):
                print(f"synced concept_daily {i}/{len(trade_days)}, rows={total}", flush=True)
            time.sleep(self.config.sleep_seconds)
        return total

    def sync_concept_members(self, trade_date: str) -> int:
        """
        Fetch Eastmoney concept constituents from Tushare dc_member for a date.
        """
        with self.store.connect() as con:
            basic = pd.read_sql_query(
                """
                select ts_code, name, idx_type
                from concept_basic
                where idx_type in ('概念板块', 'THS')
                order by ts_code
                """,
                con,
            )
        if basic.empty:
            self.sync_concept_basic()
            with self.store.connect() as con:
                basic = pd.read_sql_query(
                    """
                    select ts_code, name, idx_type
                    from concept_basic
                    where idx_type in ('概念板块', 'THS')
                    order by ts_code
                    """,
                    con,
                )
        if basic.empty:
            return 0
        rows = []
        for i, ts_code in enumerate(basic["ts_code"].dropna().astype(str).tolist(), start=1):
            df = self._call("dc_member", ts_code=ts_code, trade_date=trade_date)
            out = normalize_concept_member(df, trade_date, ts_code)
            if not out.empty:
                rows.append(out)
            if i == 1 or i % 50 == 0 or i == len(basic):
                print(f"synced concept_member {i}/{len(basic)}", flush=True)
            time.sleep(self.config.sleep_seconds)
        if not rows:
            return 0
        members = pd.concat(rows, ignore_index=True)
        self.store.upsert_concept_member(members)
        return len(members)

    def _fetch_trade_days_for_range(self, start_date: str, end_date: str) -> list[str]:
        df = self._call(
            "trade_cal",
            exchange="SSE",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            is_open="1",
            fields="cal_date,is_open",
        )
        return df["cal_date"].astype(str).sort_values().tolist()


def _empty_daily() -> pd.DataFrame:
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


def _empty_daily_basic() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["symbol", "date", "turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"]
    )


def _ensure_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df


def normalize_concept_daily(df: pd.DataFrame, trade_date: str) -> pd.DataFrame:
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
    frame = _ensure_columns(frame, columns)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"].fillna(trade_date)).dt.strftime("%Y-%m-%d")
    for col in ["pct_change", "turnover_rate", "total_mv", "leading_pct"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    for col in ["up_num", "down_num"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).astype(int)
    return frame[columns].dropna(subset=["ts_code", "trade_date"])


def normalize_concept_member(df: pd.DataFrame, trade_date: str, ts_code: str) -> pd.DataFrame:
    columns = ["trade_date", "ts_code", "con_code", "name"]
    if df.empty:
        return pd.DataFrame(columns=columns)
    frame = df.copy()
    rename_map = {"code": "con_code", "symbol": "con_code", "stock_code": "con_code", "stock_name": "name"}
    frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
    frame = _ensure_columns(frame, columns)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"].fillna(trade_date)).dt.strftime("%Y-%m-%d")
    frame["ts_code"] = frame["ts_code"].fillna(ts_code)
    return frame[columns].dropna(subset=["trade_date", "ts_code", "con_code"])
