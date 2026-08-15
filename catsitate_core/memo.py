"""短时备忘录(规格 §4.4):单条 TTL 可传,写入长度源头强制。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .config import MemoSection
from .storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"


class MemoService:
    """备忘录读写与过期清理。"""

    def __init__(self, store: SQLiteStore, config: MemoSection) -> None:
        self.store = store
        self.config = config

    def ensure_schema(self) -> None:
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS memo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                stream_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    def write(
        self,
        content: str,
        stream_id: str,
        user_id: str,
        ttl_hours: float | None,
        now: Callable[[], datetime] | None = None,
    ) -> tuple[bool, str]:
        """写入备忘。失败返回 (False, 原因) 供工具/命令展示给用户。"""

        now_fn = now or datetime.now
        text = content.strip()
        if not text:
            return False, "备忘内容不能为空"
        if len(text) > self.config.entry_max_chars:
            return False, f"备忘过长:请精简到 {self.config.entry_max_chars} 字以内"
        if ttl_hours is None:
            ttl_hours = float(self.config.default_ttl_hours)
        if ttl_hours <= 0:
            return False, "有效期必须大于 0 小时"
        if ttl_hours > self.config.max_ttl_hours:
            return False, f"有效期过长:单条上限 {self.config.max_ttl_hours} 小时"
        current = now_fn()
        expires = current + timedelta(hours=ttl_hours)
        self.store.execute(
            "INSERT INTO memo (content, stream_id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (text, stream_id or "", user_id or "", expires.strftime(_ISO), current.strftime(_ISO)),
        )
        return True, f"已记下({ttl_hours:.0f} 小时内有效)"

    def read(
        self,
        stream_id: str,
        user_id: str,
        limit: int,
        now: Callable[[], datetime] | None = None,
    ) -> list[dict]:
        """读取未过期备忘(当前流相关 + 当前说话人相关),返回含剩余有效时间。"""

        now_fn = now or datetime.now
        current = now_fn()
        # 空维度 = 无此条件(非匹配空值行);双空无归属范围,直接返回空(审查 M1)
        # 非空维度保持 OR 语义:流相关 ∪ 说话人相关(规格 §4.4)
        conditions: list[str] = []
        params: list = []
        if stream_id:
            conditions.append("stream_id = ?")
            params.append(stream_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if not conditions:
            return []
        where = " OR ".join(conditions)
        params += [current.strftime(_ISO), limit]
        rows = self.store.query(
            f"""
            SELECT id, content, stream_id, user_id, expires_at FROM memo
            WHERE {where} AND expires_at > ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            params,
        )
        result: list[dict] = []
        for row in rows:
            expires = datetime.strptime(row[4], _ISO)
            result.append(
                {
                    "id": row[0],
                    "content": row[1],
                    "stream_id": row[2],
                    "user_id": row[3],
                    "remaining_hours": round((expires - current).total_seconds() / 3600, 1),
                }
            )
        return result

    def cleanup(self, now: Callable[[], datetime] | None = None) -> int:
        """删除过期项,返回删除条数。"""

        now_fn = now or datetime.now
        current = now_fn()
        before = self.store.query("SELECT COUNT(*) FROM memo")[0][0]
        self.store.execute("DELETE FROM memo WHERE expires_at <= ?", (current.strftime(_ISO),))
        after = self.store.query("SELECT COUNT(*) FROM memo")[0][0]
        return before - after
