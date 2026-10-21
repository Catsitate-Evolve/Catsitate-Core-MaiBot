"""WebUI 管理页数据服务:好感度与备忘录读写,好感度日志与空间事件只读。

纪律:
- 页面只经插件 @API 调本服务,不接触 SQL;本服务负责全部业务校验。
- 业务校验失败返回 (False, 中文原因) 形态的 ``{"ok": False, "error": ...}``,不抛异常;
  意外异常由 API 层捕获并记录,不静默。
- 只触碰用户数据表:favorability / favorability_log / memo / qzone_fav_events。
  去重账本(batch_counter 仅作为好感度重置的一部分清理)、qzone_* 去重表、
  llm_usage、weather_snapshot 均不开放管理。
- 管理操作日志只记 user_id/字段名/影响行数,不记备忘正文全文。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from .config import FavorabilitySection, MemoSection
from .favorability import EXCLUSIVE_LEVEL, LEVELS, level_for_score
from .memo import MemoService, validate_remind_at
from .storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_PAGE_SIZE = 50
_MAX_BATCH_DELETE = 200


def _now_iso() -> str:
    return datetime.now().strftime(_ISO)


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _page_args(page: Any, page_size: Any) -> tuple[int, int, str]:
    """校验分页参数;非法返回错误原因(不静默归整,避免把越界请求伪装成第 1 页)。"""

    try:
        current = int(page)
        size = int(page_size)
    except (TypeError, ValueError):
        return 1, 20, "分页参数必须是整数"
    if current < 1:
        return 1, 20, "页码必须大于等于 1"
    if size < 1 or size > MAX_PAGE_SIZE:
        return 1, 20, f"每页条数必须在 1~{MAX_PAGE_SIZE}"
    return current, size, ""


def _normalize_date(value: str) -> tuple[str, str]:
    """校验 YYYY-MM-DD;返回 (规范化值, 错误原因)。空串合法(不过滤)。"""

    text = str(value or "").strip()
    if not text:
        return "", ""
    if not _DATE_RE.fullmatch(text):
        return "", f"日期格式非法({text}),应为 YYYY-MM-DD"
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return "", f"日期不存在({text})"
    return text, ""


class AdminService:
    """管理页数据服务(所有方法同步执行;调用方在插件事件循环内,SQLite 操作为毫秒级)。"""

    def __init__(
        self,
        store: SQLiteStore,
        memo_config: MemoSection,
        fav_config: FavorabilitySection,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.memo_config = memo_config
        self.fav_config = fav_config
        self.logger = logger or logging.getLogger("catsitate_core.admin")

    # ─── 好感度(读写) ─────────────────────────────────────────────

    def list_favorability(self, page: Any = 1, page_size: Any = 20, keyword: str = "") -> dict[str, Any]:
        current, size, page_err = _page_args(page, page_size)
        if page_err:
            return _fail(page_err)
        kw = str(keyword or "").strip()
        where = ""
        params: list[Any] = []
        if kw:
            where = "WHERE user_id LIKE ? OR note LIKE ?"
            params = [f"%{kw}%", f"%{kw}%"]
        total = int(self.store.query(f"SELECT COUNT(*) FROM favorability {where}", params)[0][0])
        rows = self.store.query(
            f"SELECT user_id, level, score, note, window_start, judged_at FROM favorability {where} "
            "ORDER BY score DESC, judged_at DESC LIMIT ? OFFSET ?",
            [*params, size, (current - 1) * size],
        )
        items = []
        for user_id, level, score, note, window_start, judged_at in rows:
            level_name = LEVELS[level] if isinstance(level, int) and 0 <= level < len(LEVELS) else str(level)
            items.append({
                "user_id": str(user_id), "level": level, "level_name": level_name,
                "score": score, "note": note or "",
                "window_start": window_start or "", "judged_at": judged_at or "",
            })
        return {"ok": True, "items": items, "total": total, "page": current, "page_size": size}

    def update_favorability(
        self, user_id: str, score: int | None = None, note: str | None = None
    ) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            return _fail("缺少用户 QQ 号")
        if score is None and note is None:
            return _fail("没有可修改的字段(score / note 至少给一个)")
        row = self.store.query("SELECT score, level, note FROM favorability WHERE user_id = ?", (uid,))
        if not row:
            return _fail(f"未找到好感度记录:{uid}")
        updates: list[str] = []
        params: list[Any] = []
        new_score = row[0][0]
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, int):
                return _fail("分数必须是整数")
            if score < 0 or score > 100:
                return _fail("分数范围是 0~100(100 即「特别」)")
            if score >= 100:
                holder = self.store.query(
                    "SELECT user_id FROM favorability WHERE level >= ? AND user_id != ? LIMIT 1",
                    (EXCLUSIVE_LEVEL, uid),
                )
                if holder:
                    return _fail(f"「特别」等级全表独占,当前持有者:{holder[0][0]};请先重置该用户")
            new_score = score
            updates.extend(["score = ?", "level = ?"])
            params.extend([score, level_for_score(score)])
        new_note = row[0][2] or ""
        if note is not None:
            text = str(note).strip()
            if len(text) > self.fav_config.note_max_chars:
                return _fail(f"关系注记过长:{len(text)} 字,上限 {self.fav_config.note_max_chars}")
            new_note = text
            updates.append("note = ?")
            params.append(text)
        params.append(uid)
        self.store.execute(f"UPDATE favorability SET {', '.join(updates)} WHERE user_id = ?", params)
        self.logger.info(
            "[管理页] 修改好感度 user_id=%s 字段=%s 新分数=%s", uid,
            "score+note" if score is not None and note is not None else ("score" if score is not None else "note"),
            new_score,
        )
        return {
            "ok": True, "user_id": uid, "score": new_score,
            "level": level_for_score(new_score), "level_name": LEVELS[level_for_score(new_score)],
            "note": new_note,
        }

    def reset_favorability(self, user_id: str) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            return _fail("缺少用户 QQ 号")
        if not self.store.query("SELECT 1 FROM favorability WHERE user_id = ?", (uid,)):
            return _fail(f"未找到好感度记录:{uid}")
        now = _now_iso()
        self.store.execute(
            "UPDATE favorability SET score = 0, level = 0, note = '', window_start = ?, judged_at = ? "
            "WHERE user_id = ?",
            (now, now, uid),
        )
        # 积压批次若不清,下一次结算会立刻覆盖重置结果
        batch_rows = self.store.query("SELECT COUNT(*) FROM batch_counter WHERE user_id = ?", (uid,))[0][0]
        self.store.execute("DELETE FROM batch_counter WHERE user_id = ?", (uid,))
        self.logger.info("[管理页] 重置好感度 user_id=%s 清理批次行=%s", uid, batch_rows)
        return {"ok": True, "user_id": uid, "cleared_batch_rows": int(batch_rows)}

    # ─── 备忘录(读写) ─────────────────────────────────────────────

    def list_memo(self, page: Any = 1, page_size: Any = 20, keyword: str = "") -> dict[str, Any]:
        current, size, page_err = _page_args(page, page_size)
        if page_err:
            return _fail(page_err)
        kw = str(keyword or "").strip()
        where = ""
        params: list[Any] = []
        if kw:
            where = "WHERE user_id LIKE ? OR content LIKE ?"
            params = [f"%{kw}%", f"%{kw}%"]
        total = int(self.store.query(f"SELECT COUNT(*) FROM memo {where}", params)[0][0])
        rows = self.store.query(
            f"SELECT id, content, stream_id, user_id, expires_at, created_at, remind_at, extra_user_ids "
            f"FROM memo {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, size, (current - 1) * size],
        )
        now = datetime.now().strftime(_ISO)
        items = []
        for memo_id, content, stream_id, user_id, expires_at, created_at, remind_at, extra in rows:
            try:
                extra_ids = json.loads(extra or "[]")
            except (json.JSONDecodeError, TypeError):
                extra_ids = []
            items.append({
                "id": memo_id, "content": content or "", "stream_id": stream_id or "",
                "user_id": str(user_id or ""), "expires_at": expires_at or "",
                "created_at": created_at or "", "remind_at": remind_at or "",
                "extra_user_ids": extra_ids if isinstance(extra_ids, list) else [],
                "expired": bool(expires_at) and str(expires_at) <= now,
            })
        return {"ok": True, "items": items, "total": total, "page": current, "page_size": size}

    def create_memo(
        self, user_id: str, content: str, ttl_hours: float = 0,
        remind_at: str = "", extra_user_ids: str = "",
    ) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        if not uid:
            return _fail("缺少用户 QQ 号")
        text = str(content or "").strip()
        if not text:
            return _fail("备忘内容不能为空")
        if len(text) > self.memo_config.entry_max_chars:
            return _fail(f"备忘过长:请精简到 {self.memo_config.entry_max_chars} 字以内")
        try:
            ttl_value = float(ttl_hours or 0)
        except (TypeError, ValueError):
            return _fail("有效期必须是数字")
        if ttl_value < 0:
            return _fail("有效期不能为负数")
        ttl: float | None = None
        if ttl_value > 0:
            if ttl_value > self.memo_config.max_ttl_hours:
                return _fail(f"有效期过长:单条上限 {self.memo_config.max_ttl_hours} 小时")
            ttl = ttl_value
        remind = str(remind_at or "").strip()
        if err := validate_remind_at(remind):
            return _fail(err)
        current = datetime.now()
        effective_ttl = ttl_value if ttl_value > 0 else float(self.memo_config.default_ttl_hours)
        expires_at = (current + timedelta(hours=effective_ttl)).strftime(_ISO)
        if remind and remind > expires_at:
            return _fail(f"提醒时刻({remind})晚于到期时间({expires_at}),永远不会触发")
        extras = [
            part.strip() for part in re.split(r"[,，、\s]+", str(extra_user_ids or ""))
            if part.strip()
        ]
        service = MemoService(self.store, self.memo_config)
        ok, message, memo_id = service.write_with_id(
            text, "", uid, ttl, remind_at=remind, extra_user_ids=extras, now=lambda: current
        )
        if not ok:
            return _fail(message)
        self.logger.info(
            "[管理页] 新增备忘 id=%s user_id=%s 有效期=%s 提醒=%s",
            memo_id, uid, ttl or "默认", remind or "无",
        )
        return {"ok": True, "id": memo_id or 0, "message": message}

    def update_memo(
        self, memo_id: int, content: str | None = None,
        ttl_hours: float = 0, remind_at: str | None = None,
    ) -> dict[str, Any]:
        try:
            mid = int(memo_id)
        except (TypeError, ValueError):
            return _fail("备忘 ID 必须是整数")
        row = self.store.query(
            "SELECT content, expires_at, remind_at, user_id FROM memo WHERE id = ?", (mid,)
        )
        if not row:
            return _fail(f"未找到备忘:{mid}")
        try:
            ttl_value = float(ttl_hours or 0)
        except (TypeError, ValueError):
            return _fail("有效期必须是数字")
        if ttl_value < 0:
            return _fail("有效期不能为负数")
        if content is None and remind_at is None and ttl_value <= 0:
            return _fail("没有可修改的字段")
        updates: list[str] = []
        params: list[Any] = []
        if content is not None:
            text = str(content).strip()
            if not text:
                return _fail("备忘内容不能为空")
            if len(text) > self.memo_config.entry_max_chars:
                return _fail(f"备忘过长:请精简到 {self.memo_config.entry_max_chars} 字以内")
            updates.append("content = ?")
            params.append(text)
        if remind_at is not None:
            remind = str(remind_at).strip()
            if err := validate_remind_at(remind):
                return _fail(err)
            updates.append("remind_at = ?")
            params.append(remind)
        final_expires = str(row[0][1] or "")
        if ttl_value > 0:
            if ttl_value > self.memo_config.max_ttl_hours:
                return _fail(f"有效期过长:单条上限 {self.memo_config.max_ttl_hours} 小时")
            final_expires = (datetime.now() + timedelta(hours=ttl_value)).strftime(_ISO)
            updates.append("expires_at = ?")
            params.append(final_expires)
        if remind_at is not None and str(remind_at).strip() and final_expires:
            if str(remind_at).strip() > final_expires:
                return _fail(f"提醒时刻({remind_at})晚于到期时间({final_expires}),永远不会触发")
        params.append(mid)
        self.store.execute(f"UPDATE memo SET {', '.join(updates)} WHERE id = ?", params)
        self.logger.info(
            "[管理页] 修改备忘 id=%s user_id=%s 字段=%s",
            mid, row[0][3], ",".join(u.split(" = ")[0] for u in updates),
        )
        return {"ok": True, "id": mid}

    def delete_memo(self, memo_id: int) -> dict[str, Any]:
        try:
            mid = int(memo_id)
        except (TypeError, ValueError):
            return _fail("备忘 ID 必须是整数")
        row = self.store.query("SELECT user_id FROM memo WHERE id = ?", (mid,))
        if not row:
            return _fail(f"未找到备忘:{mid}")
        self.store.execute("DELETE FROM memo WHERE id = ?", (mid,))
        self.logger.info("[管理页] 删除备忘 id=%s user_id=%s", mid, row[0][0])
        return {"ok": True, "deleted": 1}

    def batch_delete_memos(self, ids: str) -> dict[str, Any]:
        parts = [p for p in re.split(r"[,，、\s]+", str(ids or "")) if p]
        parsed: list[int] = []
        for part in parts:
            if not part.isdigit():
                return _fail(f"ID 列表含非法值:{part}")
            value = int(part)
            if value not in parsed:
                parsed.append(value)
        if not parsed:
            return _fail("未选择要删除的备忘")
        if len(parsed) > _MAX_BATCH_DELETE:
            return _fail(f"单次最多删除 {_MAX_BATCH_DELETE} 条")
        placeholders = ",".join("?" for _ in parsed)
        rows = self.store.query(
            f"SELECT id, user_id FROM memo WHERE id IN ({placeholders})", parsed
        )
        if not rows:
            return _fail("所选备忘均不存在(可能已被删除)")
        existing = len(rows)
        users = sorted({str(r[1] or "") for r in rows if str(r[1] or "").strip()})
        self.store.execute(f"DELETE FROM memo WHERE id IN ({placeholders})", parsed)
        self.logger.info(
            "[管理页] 批量删除备忘 请求=%s 实际=%s 涉及用户=%s", len(parsed), existing, ",".join(users)
        )
        return {"ok": True, "deleted": existing}

    # ─── 好感度日志 / 空间事件(只读) ────────────────────────────────

    def list_favorability_log(
        self, page: Any = 1, page_size: Any = 20,
        user_id: str = "", date_from: str = "", date_to: str = "",
    ) -> dict[str, Any]:
        current, size, page_err = _page_args(page, page_size)
        if page_err:
            return _fail(page_err)
        uid = str(user_id or "").strip()
        start, err = _normalize_date(date_from)
        if err:
            return _fail(err)
        end, err = _normalize_date(date_to)
        if err:
            return _fail(err)
        if start and end and start > end:
            return _fail("开始日期不能晚于结束日期")
        clauses: list[str] = []
        params: list[Any] = []
        if uid:
            clauses.append("user_id = ?")
            params.append(uid)
        if start:
            clauses.append("substr(judged_at, 1, 10) >= ?")
            params.append(start)
        if end:
            clauses.append("substr(judged_at, 1, 10) <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(self.store.query(f"SELECT COUNT(*) FROM favorability_log {where}", params)[0][0])
        rows = self.store.query(
            f"SELECT judge_id, user_id, delta, note, judged_at FROM favorability_log {where} "
            "ORDER BY judged_at DESC, rowid DESC LIMIT ? OFFSET ?",
            [*params, size, (current - 1) * size],
        )
        items = [
            {"judge_id": r[0], "user_id": str(r[1] or ""), "delta": r[2],
             "note": r[3] or "", "judged_at": r[4] or ""}
            for r in rows
        ]
        return {"ok": True, "items": items, "total": total, "page": current, "page_size": size}

    def list_qzone_events(
        self, page: Any = 1, page_size: Any = 20,
        user_id: str = "", date_from: str = "", date_to: str = "",
    ) -> dict[str, Any]:
        current, size, page_err = _page_args(page, page_size)
        if page_err:
            return _fail(page_err)
        uid = str(user_id or "").strip()
        start, err = _normalize_date(date_from)
        if err:
            return _fail(err)
        end, err = _normalize_date(date_to)
        if err:
            return _fail(err)
        if start and end and start > end:
            return _fail("开始日期不能晚于结束日期")
        clauses: list[str] = []
        params: list[Any] = []
        if uid:
            clauses.append("user_id = ?")
            params.append(uid)
        if start:
            clauses.append("substr(day, 1, 10) >= ?")
            params.append(start)
        if end:
            clauses.append("substr(day, 1, 10) <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(self.store.query(f"SELECT COUNT(*) FROM qzone_fav_events {where}", params)[0][0])
        rows = self.store.query(
            f"SELECT id, day, user_id, kind, text, created_at FROM qzone_fav_events {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, size, (current - 1) * size],
        )
        items = [
            {"id": r[0], "day": r[1] or "", "user_id": str(r[2] or ""),
             "kind": r[3] or "", "text": r[4] or "", "created_at": r[5] or ""}
            for r in rows
        ]
        return {"ok": True, "items": items, "total": total, "page": current, "page_size": size}
