# -*- coding: utf-8 -*-
"""统计与用量聚合（只读查询，不建表、不写库）。

数据源均为平台既有表：jobs / accounts / account_credits / audit_log / checkin_runs。
约定：所有 SQL 参数化；聚合尽量下沉到 SQL 层；表缺失或查询异常时返回空结构，绝不抛出。
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.channels.registry import registry
from src.core.config import config
from src.platform.db import db, now

# 账号状态归类（与 accounts.py / channels 写入的状态字面量保持一致）
_ACTIVE_STATUS = ("active", "unknown")
_COOLING_STATUS = ("cooldown",)
_DISABLED_STATUS = ("disabled", "invalid")
# 任务状态归类
_PENDING_STATUS = ("queued", "submitted")
_RUNNING_STATUS = ("processing",)

_DAY = 86400.0


def _safe_all(sql: str, args: Tuple[Any, ...] = ()) -> List[dict]:
    """只读查询多行；表不存在或异常时返回空列表。"""
    try:
        return db.all(sql, args)
    except Exception:
        return []


def _safe_one(sql: str, args: Tuple[Any, ...] = ()) -> dict:
    """只读查询单行；异常时返回空字典。"""
    try:
        row = db.one(sql, args)
    except Exception:
        row = None
    return row or {}


class Stats:
    """平台统计聚合器：全部方法均为只读查询。"""

    # ---- 概览 ----
    def overview(self) -> dict:
        """全局概览：各渠道账号/任务分布 + 总计。"""
        acc_rows = _safe_all(
            "SELECT channel, status, COUNT(*) AS c FROM accounts GROUP BY channel, status")
        job_rows = _safe_all(
            "SELECT channel, status, COUNT(*) AS c FROM jobs GROUP BY channel, status")

        specs = {d.get("name"): d for d in registry.describe() if d.get("name")}
        names: List[str] = list(registry.names())
        for r in acc_rows + job_rows:
            ch = r.get("channel")
            if ch and ch not in names:
                names.append(ch)

        acc_map: Dict[str, Dict[str, int]] = {}
        for r in acc_rows:
            acc_map.setdefault(r.get("channel"), {})[r.get("status")] = int(r.get("c") or 0)
        job_map: Dict[str, Dict[str, int]] = {}
        for r in job_rows:
            job_map.setdefault(r.get("channel"), {})[r.get("status")] = int(r.get("c") or 0)

        channels: List[dict] = []
        for name in names:
            spec = specs.get(name) or {}
            a = acc_map.get(name, {})
            j = job_map.get(name, {})
            channels.append({
                "name": name,
                "display_name": spec.get("display_name") or name,
                "accounts": sum(a.values()),
                "active": sum(a.get(s, 0) for s in _ACTIVE_STATUS),
                "cooling": sum(a.get(s, 0) for s in _COOLING_STATUS),
                "disabled": sum(a.get(s, 0) for s in _DISABLED_STATUS),
                "jobs_total": sum(j.values()),
                "jobs_success": j.get("success", 0),
                "jobs_failed": j.get("failed", 0),
                "models": int(spec.get("model_count") or 0),
            })

        media_files, media_bytes = self._media_size()

        totals = {
            "accounts": sum(c["accounts"] for c in channels),
            "active": sum(c["active"] for c in channels),
            "cooling": sum(c["cooling"] for c in channels),
            "jobs_total": sum(c["jobs_total"] for c in channels),
            "jobs_success": sum(c["jobs_success"] for c in channels),
            "jobs_failed": sum(c["jobs_failed"] for c in channels),
            "jobs_running": sum(
                j.get(s, 0)
                for j in job_map.values()
                for s in _PENDING_STATUS + _RUNNING_STATUS
            ),
            "media_files": media_files,
            "media_bytes": media_bytes,
        }
        return {"channels": channels, "totals": totals, "generated_at": now()}

    @staticmethod
    def _media_size() -> Tuple[int, int]:
        """递归统计 media 目录的文件数与总字节；异常返回 (0, 0)。"""
        try:
            root = Path(config.media_dir)
            if not root.exists():
                return 0, 0
            files = 0
            total = 0
            for p in root.rglob("*"):
                try:
                    if p.is_file():
                        files += 1
                        total += p.stat().st_size
                except OSError:
                    continue
            return files, total
        except Exception:
            return 0, 0

    # ---- 用量 ----
    def usage(self, days: int = 7, channel: Optional[str] = None) -> dict:
        """窗口用量统计：按渠道 / 模型 / 账号 / 日期。"""
        days = max(1, int(days or 7))
        since = now() - days * _DAY

        where = ["created_at >= ?"]
        where_j = ["j.created_at >= ?"]
        args: List[Any] = [since]
        if channel:
            where.append("channel=?")
            where_j.append("j.channel=?")
            args.append(channel)
        where_sql = " AND ".join(where)
        where_jsql = " AND ".join(where_j)
        targs = tuple(args)

        by_channel: List[dict] = []
        for r in _safe_all(
            "SELECT channel, COUNT(*) AS total, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
            f"FROM jobs WHERE {where_sql} GROUP BY channel ORDER BY total DESC", targs):
            total = int(r.get("total") or 0)
            succ = int(r.get("success") or 0)
            by_channel.append({
                "channel": r.get("channel"),
                "jobs_total": total,
                "jobs_success": succ,
                "jobs_failed": int(r.get("failed") or 0),
                "success_rate": round(succ / total, 4) if total else 0.0,
            })

        by_model: List[dict] = []
        for r in _safe_all(
            "SELECT channel, model, COUNT(*) AS count, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success "
            f"FROM jobs WHERE {where_sql} GROUP BY channel, model ORDER BY count DESC", targs):
            by_model.append({
                "channel": r.get("channel"),
                "model": r.get("model"),
                "count": int(r.get("count") or 0),
                "success": int(r.get("success") or 0),
            })

        by_account: List[dict] = []
        for r in _safe_all(
            "SELECT j.account_id AS account_id, j.channel AS channel, a.name AS name, "
            "COUNT(*) AS count, "
            "SUM(CASE WHEN j.status='success' THEN 1 ELSE 0 END) AS success, "
            "SUM(CASE WHEN j.status='failed' THEN 1 ELSE 0 END) AS failed "
            "FROM jobs j LEFT JOIN accounts a ON a.id=j.account_id "
            f"WHERE {where_jsql} GROUP BY j.account_id, j.channel ORDER BY count DESC", targs):
            by_account.append({
                "account_id": r.get("account_id"),
                "channel": r.get("channel"),
                "name": r.get("name"),
                "count": int(r.get("count") or 0),
                "success": int(r.get("success") or 0),
                "failed": int(r.get("failed") or 0),
            })

        by_day = self._by_day(days, where_sql, targs)

        return {
            "by_channel": by_channel,
            "by_model": by_model,
            "by_account": by_account,
            "by_day": by_day,
            "window_days": days,
        }

    @staticmethod
    def _by_day(days: int, where_sql: str, args: Tuple[Any, ...]) -> List[dict]:
        """按天聚合（UTC），窗口内无数据的日期补 0。"""
        rows = _safe_all(
            "SELECT date(created_at,'unixepoch') AS d, COUNT(*) AS count, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success "
            f"FROM jobs WHERE {where_sql} GROUP BY d", args)
        day_map = {r["d"]: r for r in rows if r.get("d")}
        today = datetime.datetime.now(datetime.timezone.utc).date()
        out: List[dict] = []
        for i in range(days - 1, -1, -1):
            d = (today - datetime.timedelta(days=i)).isoformat()
            r = day_map.get(d) or {}
            out.append({
                "date": d,
                "count": int(r.get("count") or 0),
                "success": int(r.get("success") or 0),
            })
        return out

    # ---- 积分 / 额度 ----
    def credits(self) -> dict:
        """积分/额度构成：按渠道汇总 + 按账号明细。"""
        acc_count = {
            r.get("channel"): int(r.get("c") or 0)
            for r in _safe_all("SELECT channel, COUNT(*) AS c FROM accounts GROUP BY channel")
        }

        by_channel: Dict[str, dict] = {}
        by_account: List[dict] = []
        tot_total = 0.0
        tot_alloc = 0.0

        for r in _safe_all(
            "SELECT a.channel AS channel, c.account_id AS account_id, a.name AS name, "
            "c.total AS total, c.allocatable AS allocatable, c.checked_at AS checked_at, "
            "c.error AS error "
            "FROM account_credits c LEFT JOIN accounts a ON a.id=c.account_id"):
            ch = r.get("channel") or "unknown"
            bucket = by_channel.setdefault(ch, {
                "total": 0.0, "allocatable": 0.0,
                "accounts": acc_count.get(ch, 0), "with_credit": 0,
            })
            t = float(r.get("total") or 0)
            al = float(r.get("allocatable") or 0)
            bucket["total"] += t
            bucket["allocatable"] += al
            bucket["with_credit"] += 1
            tot_total += t
            tot_alloc += al
            by_account.append({
                "account_id": r.get("account_id"),
                "channel": r.get("channel"),
                "name": r.get("name"),
                "total": t,
                "allocatable": al,
                "checked_at": r.get("checked_at"),
                "error": r.get("error"),
            })

        # 补齐没有任何额度记录的渠道
        for ch, n in acc_count.items():
            by_channel.setdefault(ch, {
                "total": 0.0, "allocatable": 0.0, "accounts": n, "with_credit": 0,
            })

        by_account.sort(key=lambda x: (x.get("channel") or "", x.get("account_id") or ""))
        return {
            "by_channel": by_channel,
            "by_account": by_account,
            "totals": {"total": round(tot_total, 4), "allocatable": round(tot_alloc, 4)},
        }

    # ---- 运行态 ----
    def runtime(self) -> dict:
        """运行态：待处理任务、24h 审计/签到、会话健康。"""
        pending = _safe_one(
            "SELECT COUNT(*) AS c FROM jobs WHERE status IN ('queued','submitted')")
        running = _safe_one(
            "SELECT COUNT(*) AS c FROM jobs WHERE status='processing'")
        audit = _safe_one(
            "SELECT COUNT(*) AS c FROM audit_log WHERE ts>=?", (now() - _DAY,))
        checkins = _safe_one(
            "SELECT COUNT(*) AS c FROM checkin_runs WHERE started_at>=?", (now() - _DAY,))
        try:
            from src.platform.session import keeper
            sessions = keeper.status()
        except Exception:
            sessions = {}
        return {
            "pending_jobs": int(pending.get("c") or 0),
            "running_jobs": int(running.get("c") or 0),
            "audit_24h": int(audit.get("c") or 0),
            "checkins_24h": int(checkins.get("c") or 0),
            "sessions": sessions,
        }


stats = Stats()
