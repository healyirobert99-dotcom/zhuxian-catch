"""Tests for ashare_a_plus.etf_map module."""
from ashare_a_plus.etf_map import (
    load_etf_mappings,
    lookup_etf,
    lookup_etf_code,
    format_etf_proxy,
    lookup_etf_full,
)


def test_load_mappings_returns_data():
    mappings = load_etf_mappings()
    assert len(mappings) > 50
    # 验证结构
    for m in mappings:
        assert "dimension" in m
        assert "keyword" in m
        assert "etf_code" in m
        assert "etf_name" in m
        assert "purity" in m


def test_lookup_industry_bank():
    info = lookup_etf("银行", dimension="行业")
    assert info is not None
    assert info["etf_code"] == "512800"


def test_lookup_industry_semiconductor():
    info = lookup_etf("半导体", dimension="行业")
    assert info is not None
    assert info["etf_code"] in ("512480",)


def test_lookup_industry_no_result():
    info = lookup_etf("不存在的行业", dimension="行业")
    assert info is None


def test_lookup_concept_aerospace():
    info = lookup_etf("商业航天")
    assert info is not None
    assert info["etf_code"] in ("159227", "159230")


def test_lookup_concept_ai():
    info = lookup_etf("人工智能")
    assert info is not None


def test_lookup_auto_dimension():
    # 银行只存在于行业维度
    info = lookup_etf("银行")
    assert info is not None


def test_format_etf_proxy_with_etf():
    result = format_etf_proxy("银行")
    assert "512800" in result
    assert "银行ETF" in result
    assert "暂无合适ETF" not in result


def test_format_etf_proxy_no_etf():
    result = format_etf_proxy("绝对不存在的行业")
    assert "暂无合适ETF" in result


def test_lookup_etf_code_returns_string():
    code = lookup_etf_code("白酒")
    assert code is not None
    assert isinstance(code, str)
    assert code.isdigit()


def test_lookup_etf_code_not_found():
    code = lookup_etf_code("不存在的行业")
    assert code is None


def test_lookup_etf_full():
    code, name, purity = lookup_etf_full("半导体")
    assert code is not None
    assert "半导体" in name
    assert purity in ("纯ETF", "偏纯", "代理") or purity is not None


def test_concept_proxy_matching():
    """概念模糊匹配测试：东方财富概念名映射到 ETF CSV 关键词"""
    # "人工智能" 应匹配 "人工智能(AI)"
    proxy = format_etf_proxy("人工智能")
    assert "暂无合适ETF" not in proxy

    # "低空经济" 应匹配
    proxy = format_etf_proxy("低空经济")
    assert "暂无合适ETF" not in proxy
