# -*- coding: utf-8 -*-
"""统一任务队列 + Worker（合并三项目的 task_store/job_store + task_worker/job_worker）。

流程对任何渠道都一致：
    queued --submit--> submitted --poll--> processing --poll--> success/failed
渠道只负责 submit() 返回上游 ID、poll() 返回归一化 PollResult。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from src.core.config import config
from src.core.errors import UpstreamError, redact
from src.channels.base import GenerateRequest
from src.channels.registry import registry
from src.platform import media as media_store
from src.platform.accounts import manager
from src.platform.antiban import throttle
from src.platform.db import db, now, jd, jl

TERMINAL = ("success", "failed", "cancelled")


class JobStore:
    def __init__(self):
        self._subs: List[Callable[[dict], None]] = []
        self._lock = threading.RLock()

    # ---- 事件总线（供 SSE） ----
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event)
            except Exception:
                pass

    # ---- CRUD ----
    def create(self, channel: str, req: GenerateRequest, account_id: Optional[str] = None,
               prompt_redacted: str = "") -> dict:
        # 幂等
        if req.idempotency_key:
            existing = db.one("SELECT * FROM jobs WHERE idempotency_key=? AND channel=?",
                              (req.idempotency_key, channel))
            if existing:
                return existing
        jid = "job_" + uuid.uuid4().hex[:16]
        t = now()
        db.execute(
            "INSERT INTO jobs(id,channel,account_id,kind,model,mode,status,prompt_redacted,"
            "request_json,idempotency_key,transport,created_at,updated_at,next_poll_at,poll_attempts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (jid, channel, account_id, req.kind, req.model, req.mode, "queued",
             prompt_redacted or redact(req.prompt)[:200], jd(_req_to_json(req)),
             req.idempotency_key or None, "requests", t, t, t), True)
        job = self.get(jid)
        self.publish({"type": "job.created", "job": job})
        return job

    def get(self, jid: str) -> Optional[dict]:
        r = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        return _decode(r) if r else None

    def list(self, status: Optional[str] = None, channel: Optional[str] = None, limit: int = 100) -> List[dict]:
        sql, args = "SELECT * FROM jobs", []
        where = []
        if status:
            where.append("status=?"); args.append(status)
        if channel:
            where.append("channel=?"); args.append(channel)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(int(limit))
        return [_decode(r) for r in db.all(sql, tuple(args))]

    def pending(self) -> List[dict]:
        rows = db.all("SELECT * FROM jobs WHERE status IN ('queued','submitted','processing') "
                      "AND (next_poll_at IS NULL OR next_poll_at<=?) ORDER BY created_at", (now(),))
        return [_decode(r) for r in rows]

    def update(self, jid: str, **fields) -> None:
        fields["updated_at"] = now()
        cols = ",".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid), True)

    def cancel(self, jid: str) -> Optional[dict]:
        job = self.get(jid)
        if job and job["status"] not in TERMINAL:
            self.update(jid, status="cancelled", finished_at=now())
        return self.get(jid)


def _req_to_json(req: GenerateRequest) -> dict:
    return {
        "channel": req.channel, "kind": req.kind, "model": req.model, "mode": req.mode,
        "prompt": req.prompt,   # 必存：漏掉会导致 worker 恢复请求时 prompt 为空，
                                # 上游收到空提示词报参数错误（pixverse 表现为 400018）
        "duration": req.duration, "ratio": req.ratio, "resolution": req.resolution,
        "size": req.size, "images": req.images, "params": req.params,
        "account_mode": req.account_mode, "timeout": req.timeout,
        "account_id": req.account_id, "idempotency_key": req.idempotency_key,
    }


def _json_to_req(d: dict) -> GenerateRequest:
    return GenerateRequest(**{k: v for k, v in (d or {}).items()
                              if k in GenerateRequest.__dataclass_fields__})


def _decode(row: dict) -> dict:
    row = dict(row)
    row["request"] = jl(row.pop("request_json", None), {})
    row["response"] = jl(row.pop("response_json", None), {})
    row["media"] = jl(row.pop("media_json", None), [])
    return row


class JobWorker:
    """单线程循环（渠道内部可自行并发）；退避 4/8/15 秒，额度/并发错误快速终止。"""

    def __init__(self, store: JobStore, interval: Optional[float] = None):
        self.store = store
        self.interval = interval or config.poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.once()
            except Exception:
                worked = False
            self._stop.wait(0.5 if worked else self.interval)

    def once(self) -> bool:
        jobs = self.store.pending()
        if not jobs:
            return False
        for job in jobs:
            try:
                self._advance(job)
            except Exception as exc:
                # 兜底：非上游异常（渠道 bug 等）也必须推进调度，
                # 否则 pending() 会每 0.5s 重捞同一任务 -> 无退避无限重试。
                self.store.update(job["id"], error=str(exc)[:500])
                try:
                    self._reschedule(job, note="advance error")
                except Exception:  # noqa: BLE001
                    pass
        return True

    # ---- 单任务推进 ----
    def _advance(self, job: dict) -> None:
        channel_name = job["channel"]
        ch = registry.get(channel_name)
        if ch is None:
            self.store.update(job["id"], status="failed", error=f"channel {channel_name} not registered",
                              error_class="upstream", finished_at=now())
            return
        req = _json_to_req(job.get("request") or {})

        # 1) 选账号
        aid = job.get("account_id")
        if not aid:
            row = manager.pick(channel_name, mode=req.account_mode or "auto")
            if not row:
                self._reschedule(job, note="no eligible account")
                return
            aid = row["id"]
            self.store.update(job["id"], account_id=aid)
        row = manager.raw(aid)
        if not row:
            self.store.update(job["id"], status="failed", error="account not found", finished_at=now())
            return
        secret = manager.secret(aid)

        # 2) 提交 / 轮询
        if job["status"] == "queued":
            manager.acquire(aid)
            try:
                res = ch.submit(secret, req)
            except UpstreamError as e:
                manager.release(aid, False, e.error_class)
                self._fail(job, e)
                return
            except Exception as e:   # noqa: BLE001 渠道内部异常也必须释放计数，否则泄漏
                manager.release(aid, False, "internal")
                self._fail(job, UpstreamError(str(e)[:300], "internal"))
                return
            if not res.accepted:
                manager.release(aid, False, res.error_class)
                self._fail(job, UpstreamError(res.error or "submit rejected", res.error_class or "upstream"))
                return
            self.store.update(job["id"], status="submitted", upstream_task_id=res.upstream_task_id,
                              transport=res.transport, response_json=jd(redact(res.raw)),
                              next_poll_at=now() + self._poll_delay(channel_name, aid, self.interval),
                              last_poll_at=now())
            manager.release(aid, True)
            return

        # submitted / processing -> poll
        try:
            poll = ch.poll(secret, job.get("upstream_task_id") or "", req)
        except UpstreamError as e:
            manager.release(aid, False, e.error_class)
            if e.error_class in ("quota", "auth_or_risk"):
                self._fail(job, e)
            else:
                self._reschedule(job, error=e)
            return

        if poll.status == "success":
            media = self._materialize(job, poll, row)
            manager.release(aid, True)
            manager.bump_generation(aid)
            self.store.update(job["id"], status="success", media_json=jd(media),
                              response_json=jd(redact(poll.raw)), finished_at=now(),
                              next_poll_at=None)
            self.store.publish({"type": "job.succeeded", "job": self.store.get(job["id"])})
            # 生成成功 = 凭证有效：推进保活时间戳，避免保活循环再打一次 verify
            #（30min 一轮 verify 是上游风控源头，有真实成功流量就该免验）
            try:
                _session_keeper.record_activity(aid, "keepalive", True, "job success 免验")
            except Exception:  # noqa: BLE001
                pass
            return
        if poll.status == "failed":
            manager.release(aid, False, poll.error_class)
            self.store.update(job["id"], status="failed", error=poll.error[:500],
                              error_class=poll.error_class, response_json=jd(redact(poll.raw)),
                              finished_at=now(), next_poll_at=None)
            self.store.publish({"type": "job.failed", "job": self.store.get(job["id"])})
            return

        # 仍在处理中
        attempts = int(job.get("poll_attempts") or 0) + 1
        delay = min(config.max_poll_interval, self.interval * (2 ** min(attempts, 3)) / 2)
        base = max(self.interval, delay)
        self.store.update(job["id"], status="processing", poll_attempts=attempts,
                          last_poll_at=now(), next_poll_at=now() + self._poll_delay(channel_name, aid, base),
                          response_json=jd(redact(poll.raw)))

    @staticmethod
    def _poll_delay(channel: str, account_id: str, base: float) -> float:
        """轮询间隔 = max(base, 同账号节流等待)，并叠加人类化抖动，避免固定周期被识别。"""
        try:
            wait = throttle.plan(f"poll:{channel}:{account_id}", base, config.antiban_jitter)
            return max(float(base), float(wait))
        except Exception:
            return float(base)

    def _materialize(self, job: dict, poll, account_row: dict) -> List[dict]:
        out = []
        proxy = account_row.get("proxy") or None
        for i, url in enumerate(poll.media or []):
            bucket = "videos" if job["kind"] == "video" else "images"
            prefix = f"{job['channel']}_{job['id'][:8]}_{i}_"
            got = media_store.fetch_to_bucket(url, bucket=bucket, prefix=prefix, proxy=proxy)
            out.append(got or {"url": url, "local_path": None, "cached": False})
        return out

    def _reschedule(self, job: dict, note: str = "", error: Optional[UpstreamError] = None) -> None:
        attempts = int(job.get("poll_attempts") or 0) + 1
        if attempts > 200 or (now() - (job.get("created_at") or now())) > config.video_timeout:
            self.store.update(job["id"], status="failed", error=(note or "timeout")[:500],
                              error_class="timeout", finished_at=now(), next_poll_at=None)
            return
        delay = min(config.max_poll_interval, 2 * attempts)
        fields = {"poll_attempts": attempts, "next_poll_at": now() + delay, "last_poll_at": now()}
        if error:
            fields["error"] = error.error_class
            fields["error_class"] = error.error_class
        elif note:
            fields["error"] = note
        self.store.update(job["id"], **fields)

    # ---- 跨渠道降级（2026-10-18）--------------------------------------
    # 场景：一个渠道 cookie 失效/额度耗尽/上游 5xx，用户只要出片，不该就此失败。
    # 规则：只在「换渠道可能有效」的错误类型下降级；内容违规换渠道也一样被拒，不降级。
    # 上限：最多 3 个渠道，避免长尾空转。
    _FAILOVER_CLASSES = ("auth_or_risk", "quota", "upstream")
    _FAILOVER_MAX = 3

    def _maybe_failover(self, job: dict, e: UpstreamError) -> bool:
        if e.error_class not in self._FAILOVER_CLASSES:
            return False
        tried = job.get("tried_channels") or ""
        try:
            tried_list = json.loads(tried) if tried else []
        except Exception:
            tried_list = []
        if job["channel"] not in tried_list:
            tried_list.append(job["channel"])
        if len(tried_list) >= self._FAILOVER_MAX:
            return False
        # 候选：注册表内其它渠道，且有可用账号。
        # 顺序按「近 7 天成功率」降序（同分按成功数），避免把好渠道排到最后、
        # 先烧掉两个注定失败的渠道再轮到能出片的那个。
        from ..channels.registry import registry as _reg
        cands = [n for n in _reg.names() if n not in tried_list]
        cands = [n for n in cands if manager.pick(n, mode="auto")]
        try:
            rows = db.all(
                "SELECT channel,"
                " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok,"
                " COUNT(*) AS total FROM jobs"
                " WHERE created_at > ? GROUP BY channel", (now() - 7 * 86400,)) or []
            score = {r["channel"]: ((r["ok"] or 0) / max(1, r["total"] or 0),
                                   r["ok"] or 0) for r in rows}
            cands.sort(key=lambda n: score.get(n, (0.0, 0)), reverse=True)
        except Exception:  # noqa: BLE001 统计不可用则保持注册表顺序
            pass
        for name in cands:
            self.store.update(job["id"], channel=name, account_id=None, status="queued",
                              upstream_task_id=None, transport="requests",
                              tried_channels=json.dumps(tried_list, ensure_ascii=False),
                              error="failover: %s -> %s (%s)" % (job["channel"], name, e.error_class),
                              error_class=None, finished_at=None,
                              next_poll_at=now(), poll_attempts=0)
            self.store.publish({"type": "job.failover",
                                "job": self.store.get(job["id"]),
                                "from": job["channel"], "to": name})
            return True
        return False

    def _fail(self, job: dict, e: UpstreamError) -> None:
        if self._maybe_failover(job, e):
            return
        self.store.update(job["id"], status="failed", error=str(e)[:500],
                          error_class=e.error_class, finished_at=now(), next_poll_at=None)
        self.store.publish({"type": "job.failed", "job": self.store.get(job["id"])})


# 惰性加列：跨渠道降级记录（不修改 db.py 的 SCHEMA，遵循项目约定）
try:
    db.execute("ALTER TABLE jobs ADD COLUMN tried_channels TEXT", (), True)
except Exception:  # noqa: BLE001 已存在则忽略
    pass

store = JobStore()
worker = JobWorker(store)
