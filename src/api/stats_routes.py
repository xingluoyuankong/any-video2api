# -*- coding: utf-8 -*-
"""统计与用量 API。

    GET /api/stats/overview   全局概览（渠道 + 总计）
    GET /api/stats/usage      用量统计（按渠道/模型/账号/日期）
    GET /api/stats/credits    积分/额度构成
    GET /api/stats/runtime    运行态（待处理任务、审计、会话）
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from src.platform.stats import stats

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/overview")
def overview() -> dict:
    """全局概览：渠道分布 + 总计。"""
    return stats.overview()


@router.get("/usage")
def usage(
    days: int = Query(7, ge=1, le=365, description="统计窗口天数"),
    channel: Optional[str] = Query(None, description="限定渠道"),
) -> dict:
    """用量统计：按渠道 / 模型 / 账号 / 日期。"""
    return stats.usage(days=days, channel=channel)


@router.get("/credits")
def credits() -> dict:
    """积分/额度构成。"""
    return stats.credits()


@router.get("/runtime")
def runtime() -> dict:
    """运行态：待处理任务、审计、会话。"""
    return stats.runtime()
