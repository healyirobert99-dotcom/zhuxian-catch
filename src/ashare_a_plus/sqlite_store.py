from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd


SCHEMA = """
create table if not exists stock_basic (
    symbol text primary key,
    ts_code text not null unique,
    name text not null,
    industry text,
    list_date text,
    is_st integer not null default 0,
    is_delist_risk integer not null default 0,
    is_suspended integer not null default 0
);

create table if not exists stock_daily (
    symbol text not null,
    date text not null,
    open real not null,
    high real not null,
    low real not null,
    close real not null,
    volume real not null,
    amount real not null,
    raw_open real,
    raw_high real,
    raw_low real,
    raw_close real,
    adj_factor real,
    pct_chg real,
    source text not null,
    updated_at text not null default current_timestamp,
    primary key (symbol, date)
);

create index if not exists idx_stock_daily_date on stock_daily(date);

create table if not exists stock_daily_basic (
    symbol text not null,
    date text not null,
    turnover_rate real,
    volume_ratio real,
    pe real,
    pb real,
    ps real,
    total_mv real,
    circ_mv real,
    updated_at text not null default current_timestamp,
    primary key (symbol, date)
);

create index if not exists idx_stock_daily_basic_date on stock_daily_basic(date);

create table if not exists concept_basic (
    ts_code text primary key,
    name text not null,
    idx_type text not null
);

create table if not exists concept_daily (
    ts_code text not null,
    trade_date text not null,
    pct_change real,
    turnover_rate real,
    up_num integer,
    down_num integer,
    total_mv real,
    leading text,
    leading_pct real,
    primary key (ts_code, trade_date)
);

create index if not exists idx_concept_daily_date on concept_daily(trade_date);

create table if not exists concept_member (
    trade_date text not null,
    ts_code text not null,
    con_code text not null,
    name text,
    primary key (trade_date, ts_code, con_code)
);

create index if not exists idx_concept_member_date on concept_member(trade_date);
"""


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.execute("pragma journal_mode=wal")
        con.execute("pragma synchronous=normal")
        return con

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.executescript(SCHEMA)

    def upsert_stock_basic(self, stock_basic: pd.DataFrame) -> None:
        if stock_basic.empty:
            return
        cols = ["symbol", "ts_code", "name", "industry", "list_date", "is_st", "is_delist_risk", "is_suspended"]
        frame = stock_basic[cols].copy()
        sql = """
        insert into stock_basic (symbol, ts_code, name, industry, list_date, is_st, is_delist_risk, is_suspended)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(symbol) do update set
            ts_code=excluded.ts_code,
            name=excluded.name,
            industry=excluded.industry,
            list_date=excluded.list_date,
            is_st=excluded.is_st,
            is_delist_risk=excluded.is_delist_risk,
            is_suspended=excluded.is_suspended
        """
        with self.connect() as con:
            con.executemany(sql, frame.itertuples(index=False, name=None))

    def upsert_daily(self, daily: pd.DataFrame) -> None:
        if daily.empty:
            return
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
        frame = daily[cols].copy()
        sql = """
        insert into stock_daily
        (symbol, date, open, high, low, close, volume, amount, raw_open, raw_high, raw_low, raw_close, adj_factor, pct_chg, source)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(symbol, date) do update set
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume,
            amount=excluded.amount,
            raw_open=excluded.raw_open,
            raw_high=excluded.raw_high,
            raw_low=excluded.raw_low,
            raw_close=excluded.raw_close,
            adj_factor=excluded.adj_factor,
            pct_chg=excluded.pct_chg,
            source=excluded.source,
            updated_at=current_timestamp
        """
        with self.connect() as con:
            con.executemany(sql, frame.itertuples(index=False, name=None))

    def upsert_daily_basic(self, daily_basic: pd.DataFrame) -> None:
        if daily_basic.empty:
            return
        cols = ["symbol", "date", "turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"]
        frame = daily_basic[cols].copy()
        sql = """
        insert into stock_daily_basic
        (symbol, date, turnover_rate, volume_ratio, pe, pb, ps, total_mv, circ_mv)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(symbol, date) do update set
            turnover_rate=excluded.turnover_rate,
            volume_ratio=excluded.volume_ratio,
            pe=excluded.pe,
            pb=excluded.pb,
            ps=excluded.ps,
            total_mv=excluded.total_mv,
            circ_mv=excluded.circ_mv,
            updated_at=current_timestamp
        """
        with self.connect() as con:
            con.executemany(sql, frame.itertuples(index=False, name=None))

    def upsert_concept_basic(self, concept_basic: pd.DataFrame) -> None:
        if concept_basic.empty:
            return
        cols = ["ts_code", "name", "idx_type"]
        frame = concept_basic[cols].copy()
        sql = """
        insert into concept_basic (ts_code, name, idx_type)
        values (?, ?, ?)
        on conflict(ts_code) do update set
            name=excluded.name,
            idx_type=excluded.idx_type
        """
        with self.connect() as con:
            con.executemany(sql, frame.itertuples(index=False, name=None))

    def upsert_concept_daily(self, concept_daily: pd.DataFrame) -> None:
        if concept_daily.empty:
            return
        cols = [
            "ts_code",
            "trade_date",
            "pct_change",
            "turnover_rate",
            "up_num",
            "down_num",
            "total_mv",
            "leading",
            "leading_pct",
        ]
        frame = concept_daily[cols].copy()
        sql = """
        insert into concept_daily
        (ts_code, trade_date, pct_change, turnover_rate, up_num, down_num, total_mv, leading, leading_pct)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(ts_code, trade_date) do update set
            pct_change=excluded.pct_change,
            turnover_rate=excluded.turnover_rate,
            up_num=excluded.up_num,
            down_num=excluded.down_num,
            total_mv=excluded.total_mv,
            leading=excluded.leading,
            leading_pct=excluded.leading_pct
        """
        with self.connect() as con:
            con.executemany(sql, frame.itertuples(index=False, name=None))

    def upsert_concept_member(self, concept_member: pd.DataFrame) -> None:
        if concept_member.empty:
            return
        cols = ["trade_date", "ts_code", "con_code", "name"]
        frame = concept_member[cols].copy()
        sql = """
        insert into concept_member (trade_date, ts_code, con_code, name)
        values (?, ?, ?, ?)
        on conflict(trade_date, ts_code, con_code) do update set
            name=excluded.name
        """
        with self.connect() as con:
            con.executemany(sql, frame.itertuples(index=False, name=None))

    def load_stock_list(self) -> pd.DataFrame:
        with self.connect() as con:
            return pd.read_sql_query(
                """
                select symbol, name, industry, list_date
                from stock_basic
                where is_st=0 and is_delist_risk=0 and is_suspended=0
                order by symbol
                """,
                con,
                dtype={"symbol": str},
            )

    def load_history(self, start: str, end: str, symbols: list[str] | None = None) -> pd.DataFrame:
        params: list[object] = [start, end]
        symbol_clause = ""
        if symbols:
            placeholders = ",".join(["?"] * len(symbols))
            symbol_clause = f" and d.symbol in ({placeholders})"
            params.extend(symbols)
        sql = f"""
        select d.symbol, d.date, d.open, d.high, d.low, d.close, d.volume, d.amount,
               b.name, b.industry
        from stock_daily d
        join stock_basic b on b.symbol = d.symbol
        where d.date >= ? and d.date <= ?
          and b.is_st=0 and b.is_delist_risk=0 and b.is_suspended=0
          {symbol_clause}
        order by d.symbol, d.date
        """
        with self.connect() as con:
            df = pd.read_sql_query(sql, con, params=params, dtype={"symbol": str})
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def stats(self) -> dict:
        with self.connect() as con:
            daily = con.execute(
                "select min(date), max(date), count(*), count(distinct symbol) from stock_daily"
            ).fetchone()
            daily_basic = con.execute(
                "select min(date), max(date), count(*), count(distinct symbol) from stock_daily_basic"
            ).fetchone()
            stocks = con.execute("select count(*) from stock_basic").fetchone()
        return {
            "min_date": daily[0],
            "max_date": daily[1],
            "daily_rows": daily[2],
            "daily_symbols": daily[3],
            "daily_basic_min_date": daily_basic[0],
            "daily_basic_max_date": daily_basic[1],
            "daily_basic_rows": daily_basic[2],
            "daily_basic_symbols": daily_basic[3],
            "stock_count": stocks[0],
        }

    def daily_row_count(self, date: str) -> int:
        with self.connect() as con:
            row = con.execute("select count(*) from stock_daily where date = ?", (date,)).fetchone()
        return int(row[0])

    def daily_basic_row_count(self, date: str) -> int:
        with self.connect() as con:
            row = con.execute("select count(*) from stock_daily_basic where date = ?", (date,)).fetchone()
        return int(row[0])

    def concept_daily_row_count(self, date: str) -> int:
        with self.connect() as con:
            row = con.execute("select count(*) from concept_daily where trade_date = ?", (date,)).fetchone()
        return int(row[0])
