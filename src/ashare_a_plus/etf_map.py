"""
ETF 映射模块 —— 从 data/sector_etf_map.csv 加载行业/概念 → 可交易 ETF 的映射表。

用法:
    from ashare_a_plus.etf_map import lookup_etf, format_etf_proxy, load_etf_mappings

    info = lookup_etf("半导体")       # 自动匹配行业或概念维度
    info = lookup_etf("商业航天")     # 概念维度优先
    info = lookup_etf("银行", dimension="行业")  # 指定维度

    proxy_text = format_etf_proxy("半导体")  # "512480（国联安半导体ETF）"
"""

from __future__ import annotations

import csv
import functools
import os
from pathlib import Path
from typing import Optional

# CSV 中使用的特殊维度标记
DIMENSION_INDUSTRY = "行业"
DIMENSION_CONCEPT = "概念"
DIMENSION_STRATEGY = "策略"

_CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "sector_etf_map.csv"


@functools.lru_cache(maxsize=1)
def load_etf_mappings() -> list[dict]:
    """加载并解析 sector_etf_map.csv，返回结构化列表。"""
    if not _CSV_PATH.exists():
        return []

    rows = []

    with open(_CSV_PATH, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 板块列用于节分隔（如 "=== 行业维度 ==="），跳过
            category = (row.get("板块") or "").strip()
            if not category or category.startswith("==="):
                continue

            dim = (row.get("维度") or "").strip()
            if not dim:
                continue

            keyword = (row.get("项目主线/概念关键词") or "").strip()
            etf_code = (row.get("ETF代码") or "").strip()
            etf_name = (row.get("ETF简称") or "").strip()
            purity = (row.get("纯度") or "").strip()
            note = (row.get("备注") or "").strip()

            # 跳过无 ETF 可用的行
            if etf_code in ("—", ""):
                continue

            rows.append({
                "dimension": dim,
                "keyword": keyword,
                "etf_code": etf_code,
                "etf_name": etf_name,
                "purity": purity,
                "note": note,
            })

    return rows


def lookup_etf(
    name: str,
    dimension: str = "auto",
    maturity: str = "all",
) -> Optional[dict]:
    """根据行业/概念名称查找对应的 ETF 信息。

    Args:
        name: 行业或概念名称，如 "半导体"、"商业航天"、"白酒"
        dimension: "行业" | "概念" | "auto"
            指定匹配维度，"auto" 表示两个维度都匹配，概念优先
        maturity: "all" | "纯" | "偏纯"
            "纯" 只返回纯度标记为"纯"的 ETF，"偏纯" 返回"纯"+偏纯，"all" 全部

    Returns:
        dict 包含 etf_code, etf_name, purity, note, keyword 等字段，未找到返回 None
    """
    mappings = load_etf_mappings()
    name_lower = name.strip().lower()

    candidates = []
    for m in mappings:
        if dimension == "行业" and m["dimension"] != DIMENSION_INDUSTRY:
            continue
        if dimension == "概念" and m["dimension"] != DIMENSION_CONCEPT:
            continue
        if dimension == "auto" and m["dimension"] == DIMENSION_STRATEGY:
            continue

        # 完全匹配
        if m["keyword"].lower() == name_lower:
            candidates.append((0, m))
            continue

        # 关键词包含匹配（正反双向）
        kw_lower = m["keyword"].lower()
        if kw_lower in name_lower or name_lower in kw_lower:
            score = max(len(kw_lower), len(name_lower)) - min(len(kw_lower), len(name_lower))
            candidates.append((score + 1, m))
            continue

        # "/" 分隔的多关键词分别匹配
        for part in kw_lower.split("/"):
            part = part.strip()
            if part and part in name_lower:
                candidates.append((2, m))
                break

    if not candidates:
        return None

    # 排序：精确匹配优先，概念维度优先（dimension=auto 时）
    def sort_key(item):
        score, m = item
        dim_priority = 0 if m["dimension"] == DIMENSION_CONCEPT else 1
        pure_priority = 0 if m["purity"] == "纯" else (1 if m["purity"] == "偏纯" else 2)
        return (score, dim_priority, pure_priority)

    candidates.sort(key=sort_key)

    # 纯度过滤
    if maturity == "纯":
        candidates = [(s, m) for s, m in candidates if m["purity"] == "纯"]
        if not candidates:
            return None
    elif maturity == "偏纯":
        candidates = [(s, m) for s, m in candidates if m["purity"] in ("纯", "偏纯")]
        if not candidates:
            return None

    _, best = candidates[0]
    return best


def format_etf_proxy(name: str, dimension: str = "auto") -> str:
    """将行业/概念名称格式化为人类可读的 ETF 代理字符串。

    返回示例：
        "512480（国联安半导体ETF）"
        "159227（鹏华航空航天ETF，约20亿规模）"
        "暂无合适ETF，使用行业指数观察"
        "512170（华宝医疗ETF，CXO含量约40%）

    Args:
        name: 行业/概念名称
        dimension: "行业" | "概念" | "auto"
    """
    info = lookup_etf(name, dimension=dimension)
    if info is None:
        return "暂无合适ETF，使用行业指数观察"

    code = info["etf_code"]
    etf_name = info["etf_name"]
    purity = info["purity"]
    note = info.get("note", "")

    # 代理类加说明
    suffix = ""
    if purity == "代理" and note:
        suffix = f"，{note}"
    elif purity == "代理":
        suffix = "，非纯ETF"

    return f"{code}（{etf_name}{suffix}）"


def lookup_etf_code(name: str, dimension: str = "auto") -> Optional[str]:
    """快捷方法：只返回 ETF 代码。"""
    info = lookup_etf(name, dimension=dimension)
    return info["etf_code"] if info else None


def lookup_etf_full(name: str, dimension: str = "auto") -> tuple[Optional[str], Optional[str], Optional[str]]:
    """返回 (etf_code, etf_name, purity_note) 三元组。"""
    info = lookup_etf(name, dimension=dimension)
    if info is None:
        return (None, None, None)
    purity_note = {"纯": "纯ETF", "偏纯": "偏纯", "代理": "代理"}.get(info["purity"], "")
    if purity_note and info.get("note"):
        purity_note = f"{purity_note}（{info['note']}）"
    return (info["etf_code"], info["etf_name"], purity_note)


def all_available_concepts() -> list[str]:
    """返回所有可用 ETF 的概念关键词列表。"""
    mappings = load_etf_mappings()
    return sorted(set(m["keyword"] for m in mappings if m["dimension"] == DIMENSION_CONCEPT))


def all_available_industries() -> list[str]:
    """返回所有可用 ETF 的行业关键词列表。"""
    mappings = load_etf_mappings()
    return sorted(set(m["keyword"] for m in mappings if m["dimension"] == DIMENSION_INDUSTRY))
