import pandas as pd

from ashare_a_plus.sqlite_store import SQLiteStore


def test_sqlite_store_round_trip(tmp_path):
    store = SQLiteStore(tmp_path / "cache.sqlite3")
    store.upsert_stock_basic(
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "ts_code": ["000001.SZ"],
                "name": ["平安银行"],
                "industry": ["银行"],
                "list_date": ["19910403"],
                "is_st": [0],
                "is_delist_risk": [0],
                "is_suspended": [0],
            }
        )
    )
    store.upsert_daily(
        pd.DataFrame(
            {
                "symbol": ["000001"],
                "date": ["2024-01-02"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.0],
                "close": [10.5],
                "volume": [1000.0],
                "amount": [10_000_000.0],
                "raw_open": [10.0],
                "raw_high": [11.0],
                "raw_low": [9.0],
                "raw_close": [10.5],
                "adj_factor": [1.0],
                "pct_chg": [1.0],
                "source": ["tushare"],
            }
        )
    )

    stock_list = store.load_stock_list()
    prices = store.load_history("2024-01-01", "2024-01-31")

    assert stock_list["symbol"].tolist() == ["000001"]
    assert prices.iloc[0]["name"] == "平安银行"
    assert prices.iloc[0]["close"] == 10.5
    assert store.stats()["daily_rows"] == 1

    store.upsert_concept_basic(
        pd.DataFrame(
            {
                "ts_code": ["BK0963.DC"],
                "name": ["商业航天"],
                "idx_type": ["概念板块"],
            }
        )
    )
    store.upsert_concept_daily(
        pd.DataFrame(
            {
                "ts_code": ["BK0963.DC"],
                "trade_date": ["2024-01-02"],
                "pct_change": [2.5],
                "turnover_rate": [3.0],
                "up_num": [20],
                "down_num": [5],
                "total_mv": [100_000.0],
                "leading": ["示例股份"],
                "leading_pct": [10.0],
            }
        )
    )
    store.upsert_concept_member(
        pd.DataFrame(
            {
                "trade_date": ["2024-01-02"],
                "ts_code": ["BK0963.DC"],
                "con_code": ["000001.SZ"],
                "name": ["平安银行"],
            }
        )
    )

    assert store.concept_daily_row_count("2024-01-02") == 1
