from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd


PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount"]


@dataclass(frozen=True)
class StockInfo:
    symbol: str
    name: str


class AkShareDataProvider:
    """AkShare/东方财富 data provider with CSV caching."""

    def __init__(self, raw_dir: Path):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def _akshare(self):
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "akshare is not installed. Run: python3 -m pip install -e '.[dev]'"
            ) from exc
        return ak

    def load_stock_list(self, use_cache: bool = True) -> pd.DataFrame:
        cache_path = self.raw_dir / "stock_list.csv"
        if use_cache and cache_path.exists():
            return pd.read_csv(cache_path, dtype={"symbol": str})

        ak = self._akshare()
        df = ak.stock_info_a_code_name()
        df = df.rename(columns={"code": "symbol", "代码": "symbol", "name": "name", "名称": "name"})
        df = df[["symbol", "name"]].copy()
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        df = filter_stock_universe(df)
        df.to_csv(cache_path, index=False)
        return df

    def load_history(
        self,
        symbol: str,
        start: str,
        end: str,
        adjust: str = "qfq",
        use_cache: bool = True,
    ) -> pd.DataFrame:
        symbol = str(symbol).zfill(6)
        cache_path = self.raw_dir / f"{symbol}_{start}_{end}_{adjust}.csv"
        if use_cache and cache_path.exists():
            return normalize_history(pd.read_csv(cache_path), symbol)

        ak = self._akshare()
        df = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start.replace("-", ""),
            end_date=end.replace("-", ""),
            adjust=adjust,
        )
        df = normalize_history(df, symbol)
        df.to_csv(cache_path, index=False)
        return df

    def load_many_histories(
        self,
        symbols: Iterable[str],
        start: str,
        end: str,
        adjust: str = "qfq",
        use_cache: bool = True,
    ) -> pd.DataFrame:
        frames = []
        for symbol in symbols:
            try:
                frame = self.load_history(symbol, start, end, adjust=adjust, use_cache=use_cache)
            except Exception as exc:  # pragma: no cover - network/data-source tolerance
                print(f"skip {symbol}: {exc}")
                continue
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame(columns=["symbol"] + PRICE_COLUMNS)
        return pd.concat(frames, ignore_index=True)


def filter_stock_universe(stock_list: pd.DataFrame) -> pd.DataFrame:
    df = stock_list.copy()
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df["name"] = df["name"].astype(str)
    name = df["name"].str.upper()
    excluded = (
        name.str.contains("ST", regex=False)
        | name.str.contains("*ST", regex=False)
        | name.str.contains("退", regex=False)
        | name.str.contains("N", regex=False)
    )
    ordinary_prefix = df["symbol"].str.startswith(("00", "30", "60", "68"))
    return df.loc[ordinary_prefix & ~excluded, ["symbol", "name"]].drop_duplicates("symbol")


def normalize_history(df: pd.DataFrame, symbol: Optional[str] = None) -> pd.DataFrame:
    rename_map = {
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "amount",
    }
    out = df.rename(columns=rename_map).copy()
    missing = [col for col in PRICE_COLUMNS if col not in out.columns]
    if missing:
        raise ValueError(f"history is missing required columns: {missing}")
    out = out[PRICE_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"])
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
    out = out.drop_duplicates("date")
    if symbol is not None:
        out.insert(0, "symbol", str(symbol).zfill(6))
    return out.reset_index(drop=True)
