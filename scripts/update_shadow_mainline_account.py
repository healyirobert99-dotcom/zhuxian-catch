from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

from research_run_cards import SHADOW_SCHEMA_VERSION  # noqa: E402


DB_PATH = BASE / "data" / "a_stock_selector.sqlite3"
RUN_CARD_DIR = BASE / "reports" / "daily_review" / "run_cards"
OUT_DIR = BASE / "reports" / "shadow_mainline_account"
ACCOUNT_PATH = OUT_DIR / "shadow_mainline_account.csv"
HORIZONS = (5, 10, 20, 40, 60)
COLUMNS = [
    "shadow_id",
    "date",
    "industry",
    "signal_type",
    "mainline_level",
    "stage",
    "market_score",
    "market_bucket",
    "reason",
    "carrier_type",
    "carrier_name",
    "entry_reference",
    "exit_status",
    "exit_date",
    "exit_reason",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "ret_40d",
    "ret_60d",
    "max_drawdown_20d",
    "max_drawdown_40d",
    "notes",
]


def main() -> None:
    args = parse_args()
    path, total, new = update_shadow_account(args.start_date, args.end_date)
    print(path)
    print(f"events={total}, new={new}")


def update_shadow_account(start_date: str | None = None, end_date: str | None = None) -> tuple[Path, int, int]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    account = load_account()
    cards = load_run_cards(start_date, end_date)
    new_events = build_events(cards)
    account = merge_events(account, new_events)
    account = update_forward_metrics(account)
    account.to_csv(ACCOUNT_PATH, index=False, encoding="utf-8")
    return ACCOUNT_PATH, len(account), len(new_events)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update non-trading shadow mainline observation account.")
    parser.add_argument("--start-date", help="Only load run cards on/after this date, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Only load run cards on/before this date, YYYY-MM-DD.")
    return parser.parse_args()


def load_account() -> pd.DataFrame:
    if not ACCOUNT_PATH.exists():
        return pd.DataFrame(columns=COLUMNS)
    frame = pd.read_csv(ACCOUNT_PATH, dtype=str)
    for col in COLUMNS:
        if col not in frame.columns:
            frame[col] = np.nan
    return frame[COLUMNS]


def load_run_cards(start_date: str | None, end_date: str | None) -> list[dict]:
    cards = []
    start = pd.Timestamp(start_date) if start_date else None
    end = pd.Timestamp(end_date) if end_date else None
    for path in sorted(RUN_CARD_DIR.glob("run_card_*.json")):
        card = json.loads(path.read_text(encoding="utf-8"))
        report_date = pd.Timestamp(card.get("report_date"))
        if start is not None and report_date < start:
            continue
        if end is not None and report_date > end:
            continue
        cards.append(card)
    return cards


def build_events(cards: list[dict]) -> pd.DataFrame:
    rows = []
    for card in cards:
        date = card.get("report_date", "")
        market = card.get("market", {})
        market_score = market.get("market_score")
        market_bucket = market.get("market_bucket")
        for item in card.get("early_focus_list", []):
            rows.append(
                event_row(
                    date=date,
                    industry=item.get("industry", ""),
                    signal_type="early_core_env45",
                    level="early_focus",
                    stage=item.get("stage", ""),
                    market_score=market_score,
                    market_bucket=market_bucket,
                    reason=item.get("reason", "核心早期信号 + 环境分>=45"),
                    notes=f"schema={SHADOW_SCHEMA_VERSION}; source=run_card",
                )
            )
        for industry in card.get("concept_industry_resonance", {}).get("resonance", []):
            rows.append(
                event_row(
                    date=date,
                    industry=industry,
                    signal_type="concept_industry_resonance",
                    level="concept_resonance",
                    stage="concept_industry_resonance",
                    market_score=market_score,
                    market_bucket=market_bucket,
                    reason="概念与行业主线共振",
                    notes=f"schema={SHADOW_SCHEMA_VERSION}; source=run_card",
                )
            )
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame(rows, columns=COLUMNS)


def event_row(
    *,
    date: str,
    industry: str,
    signal_type: str,
    level: str,
    stage: str,
    market_score,
    market_bucket,
    reason: str,
    notes: str,
) -> dict:
    industry = str(industry)
    date = pd.Timestamp(date).date().isoformat()
    shadow_id = f"{date}|{industry}|{signal_type}"
    return {
        "shadow_id": shadow_id,
        "date": date,
        "industry": industry,
        "signal_type": signal_type,
        "mainline_level": level,
        "stage": stage,
        "market_score": market_score,
        "market_bucket": market_bucket,
        "reason": reason,
        "carrier_type": "industry_beta",
        "carrier_name": "行业中位收益",
        "entry_reference": "close",
        "exit_status": "active",
        "exit_date": "",
        "exit_reason": "",
        "ret_5d": "",
        "ret_10d": "",
        "ret_20d": "",
        "ret_40d": "",
        "ret_60d": "",
        "max_drawdown_20d": "",
        "max_drawdown_40d": "",
        "notes": notes,
    }


def merge_events(account: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([account, events], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=COLUMNS)
    combined = combined.drop_duplicates("shadow_id", keep="first")
    return combined[COLUMNS].sort_values(["date", "industry", "signal_type"]).reset_index(drop=True)


def update_forward_metrics(account: pd.DataFrame) -> pd.DataFrame:
    if account.empty:
        return account
    industries = sorted(account["industry"].dropna().astype(str).unique())
    start = pd.to_datetime(account["date"]).min().date().isoformat()
    beta = load_industry_beta(start, industries)
    latest_date = beta["date"].max() if not beta.empty else pd.NaT
    out = account.copy()
    for idx, row in out.iterrows():
        industry = str(row["industry"])
        date = pd.Timestamp(row["date"])
        path = beta[(beta["industry"] == industry) & (beta["date"] >= date)].sort_values("date").reset_index(drop=True)
        if path.empty:
            continue
        entry = float(path.iloc[0]["index"])
        for horizon in HORIZONS:
            col = f"ret_{horizon}d"
            if len(path) > horizon:
                out.at[idx, col] = path.iloc[horizon]["index"] / entry - 1
        for horizon in (20, 40):
            col = f"max_drawdown_{horizon}d"
            if len(path) > 1:
                window = path.iloc[: min(horizon + 1, len(path))]["index"].astype(float)
                peak = window.cummax()
                out.at[idx, col] = (window / peak - 1).min()
        observed_days = max(len(path) - 1, 0)
        if observed_days >= 60:
            out.at[idx, "exit_status"] = "completed"
            out.at[idx, "exit_date"] = path.iloc[min(60, len(path) - 1)]["date"].date().isoformat()
            out.at[idx, "exit_reason"] = "观察期结束"
        elif pd.notna(latest_date):
            out.at[idx, "exit_status"] = "active"
    return out[COLUMNS]


def load_industry_beta(start: str, industries: list[str]) -> pd.DataFrame:
    if not industries:
        return pd.DataFrame(columns=["date", "industry", "daily_ret", "index"])
    placeholders = ",".join(["?"] * len(industries))
    with sqlite3.connect(DB_PATH) as con:
        frame = pd.read_sql_query(
            f"""
            select d.date, b.industry, d.pct_chg
            from stock_daily d
            join stock_basic b on b.symbol = d.symbol
            where d.date >= ?
              and b.industry in ({placeholders})
              and d.pct_chg is not null
              and b.is_st=0 and b.is_delist_risk=0 and b.is_suspended=0
            order by b.industry, d.date
            """,
            con,
            params=[start, *industries],
        )
    if frame.empty:
        return pd.DataFrame(columns=["date", "industry", "daily_ret", "index"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["daily_ret"] = pd.to_numeric(frame["pct_chg"], errors="coerce") / 100
    beta = frame.groupby(["date", "industry"], as_index=False)["daily_ret"].median()
    beta = beta.sort_values(["industry", "date"])
    beta["index"] = beta.groupby("industry")["daily_ret"].transform(lambda s: (1 + s.fillna(0)).cumprod())
    return beta


if __name__ == "__main__":
    main()
