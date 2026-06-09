from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


MAINLINE_RULE_VERSION = "mainline_rules_v0.4"
VALIDATION_RULE_VERSION = "validation_rules_v0.1"
SHADOW_SCHEMA_VERSION = "shadow_mainline_observation_v0.1"


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if pd.isna(value) if not isinstance(value, (str, bytes, list, tuple, dict)) else False:
        return None
    return value


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def frame_names(frame: pd.DataFrame, column: str, limit: int = 12) -> list[str]:
    if frame.empty or column not in frame.columns:
        return []
    return [str(x) for x in frame[column].dropna().astype(str).head(limit).tolist()]


def data_quality_flags(market: dict[str, Any], concept_status: str | None = None) -> list[str]:
    flags: list[str] = []
    if market.get("missing_indices"):
        flags.append("index_data_missing")
    if market.get("index_status") and market.get("index_status") != "当日数据":
        flags.append(f"index_status:{market.get('index_status')}")
    cache_stats = market.get("cache_stats", {})
    if cache_stats.get("daily_rows", 0) and cache_stats.get("daily_rows", 0) < 3000:
        flags.append("stock_daily_low_rows")
    concept_stats = market.get("concept_cache_stats", {})
    if concept_stats.get("concept_daily_rows", 0) and concept_stats.get("concept_daily_rows", 0) < 100:
        flags.append("concept_daily_low_rows")
    if concept_status and concept_status != "ok":
        flags.append(f"concept_lifecycle:{concept_status}")
    return flags


def build_daily_run_card(
    *,
    report_date: str,
    market: dict[str, Any],
    lifecycle: dict[str, pd.DataFrame],
    concept_lifecycle: dict[str, pd.DataFrame],
    recent_review: pd.DataFrame,
    catalyst_review: dict[str, Any],
    output_files: dict[str, str],
) -> dict[str, Any]:
    overview = lifecycle.get("mainline_overview", pd.DataFrame())
    retreat = lifecycle.get("retreat_mainline", pd.DataFrame())
    grade_a = lifecycle.get("grade_a", pd.DataFrame())
    grade_b = lifecycle.get("grade_b", pd.DataFrame())
    grade_c = lifecycle.get("grade_c", pd.DataFrame())
    early_focus = pd.DataFrame()
    if not recent_review.empty and "priority_label" in recent_review.columns:
        early_focus = recent_review[recent_review["priority_label"] == "优先复核"].copy()

    resonance = []
    divergence = []
    concepts = concept_lifecycle.get("mainline_overview", pd.DataFrame())
    if not concepts.empty and "resonance_status" in concepts.columns:
        resonance = frame_names(concepts[concepts["resonance_status"].astype(str).str.contains("共振", na=False)], "industry")
        divergence = frame_names(concepts[concepts["resonance_status"].astype(str).str.contains("背离", na=False)], "industry")

    catalyst_available = False
    catalyst_note = ""
    summary_frame = catalyst_review.get("summary") if isinstance(catalyst_review, dict) else None
    if isinstance(summary_frame, pd.DataFrame) and not summary_frame.empty:
        catalyst_available = True
        catalyst_note = f"{len(summary_frame)} matched catalyst rows"
    else:
        catalyst_note = str(catalyst_review.get("note", "暂无有效文本数据")) if isinstance(catalyst_review, dict) else "暂无有效文本数据"

    return {
        "run_id": f"daily_review_{report_date}_{MAINLINE_RULE_VERSION}",
        "run_type": "daily_review",
        "report_date": report_date,
        "data_end_date": report_date,
        "generated_at": now_iso(),
        "rule_version": MAINLINE_RULE_VERSION,
        "data_sources": {
            "price": "tushare_local_cache",
            "valuation": "tushare_local_cache",
            "concept": "local_concept_daily_and_member",
            "catalyst": "local_keyword_files_and_optional_news_sync",
        },
        "data_quality_flags": data_quality_flags(market, concept_lifecycle.get("status")),
        "market": {
            "market_score": market.get("score"),
            "market_bucket": market.get("label"),
            "prev_market_score": (market.get("previous") or {}).get("score"),
            "score_change": None
            if not market.get("previous")
            else market.get("score", 0) - (market.get("previous") or {}).get("score", 0),
        },
        "mainline_summary": {
            "a_level": frame_names(grade_a, "industry"),
            "b_level": frame_names(grade_b, "industry"),
            "c_observation": frame_names(grade_c, "industry"),
            "retreat_alerts": frame_names(retreat, "industry"),
            "overview": frame_names(overview, "industry", limit=18),
        },
        "early_focus_list": [
            {
                "industry": str(row.get("industry", "")),
                "stage": str(row.get("stage", "")),
                "reason": str(row.get("early_signal_type", row.get("lifecycle_judgment", "优先复核"))),
            }
            for _, row in early_focus.head(20).iterrows()
        ],
        "concept_industry_resonance": {
            "resonance": resonance,
            "divergence": divergence,
            "data_date": concept_lifecycle.get("data_date"),
        },
        "catalyst_summary": {
            "available": catalyst_available,
            "note": catalyst_note,
        },
        "output_files": output_files,
        "known_limitations": [
            "行业 beta 使用本地 stock_basic 行业字段近似",
            "概念板块价格由日涨跌幅反推，和真实ETF净值存在偏差",
            "催化复核只用于解释和复核优先级，不改变主线评级",
        ],
        "next_review_items": frame_names(early_focus, "industry", limit=10),
    }


def build_validation_run_card(
    *,
    run_id: str,
    validation_target: str,
    start_date: str,
    end_date: str,
    sample_count: int,
    key_results: dict[str, Any],
    conclusion: str,
    output_files: list[str],
    control_groups: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "run_type": "historical_validation",
        "validation_target": validation_target,
        "start_date": start_date,
        "end_date": end_date,
        "generated_at": now_iso(),
        "rule_version": VALIDATION_RULE_VERSION,
        "sample_count": sample_count,
        "control_groups": control_groups or [],
        "key_results": key_results,
        "conclusion": conclusion,
        "limitations": [
            "非交易回测，不含仓位、滑点和交易成本",
            "行业或概念 beta 使用本地行情近似",
            "结果用于规则复核，不自动改写主线评级",
        ],
        "output_files": output_files,
    }
