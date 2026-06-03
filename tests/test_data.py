import pandas as pd

from ashare_a_plus.data import filter_stock_universe, normalize_history


def test_normalize_history_sorts_and_renames_columns():
    raw = pd.DataFrame(
        {
            "日期": ["2020-01-02", "2020-01-01"],
            "开盘": [11, 10],
            "最高": [12, 11],
            "最低": [10, 9],
            "收盘": [11.5, 10.5],
            "成交量": [200, 100],
            "成交额": [2000, 1000],
        }
    )

    out = normalize_history(raw, "1")

    assert list(out.columns) == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert out["symbol"].unique().tolist() == ["000001"]
    assert out["date"].tolist() == [pd.Timestamp("2020-01-01"), pd.Timestamp("2020-01-02")]


def test_filter_stock_universe_removes_st_and_non_common_prefixes():
    stock_list = pd.DataFrame(
        {
            "symbol": ["000001", "300001", "600001", "688001", "830001", "000002"],
            "name": ["平安银行", "创业股", "ST风险", "科创股", "北交所", "退市股"],
        }
    )

    out = filter_stock_universe(stock_list)

    assert out["symbol"].tolist() == ["000001", "300001", "688001"]
