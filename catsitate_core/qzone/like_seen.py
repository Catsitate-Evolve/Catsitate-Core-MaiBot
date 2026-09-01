"""赞事件去重(qzone_likes):同一人同一条说说只通知一次。

键=liker_owner_hash(取消赞再赞不重复通知);30 天修剪防表无限增长。
风格对齐 comment_seen.py(SQLiteStore 薄封装,异常直接抛出不静默)。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

_ISO = "%Y-%m-%dT%H:%M:%S"


class LikeSeenStore:
    """qzone_likes 表的薄封装(幂等主键 like_key)。"""

    def __init__(self, store) -> None:
        self.store = store
        self.store.execute(
            "CREATE TABLE IF NOT EXISTS qzone_likes ("
            "like_key TEXT PRIMARY KEY, liker_uin TEXT NOT NULL, "
            "target_tid TEXT NOT NULL, created_at TEXT NOT NULL)"
        )

    def is_new(self, like_key: str, *, liker_uin: str, target_tid: str) -> bool:
        """发现即登记(与评论去重同契约):新事件 True,重复 False。"""
        try:
            self.store.execute(
                "INSERT INTO qzone_likes (like_key, liker_uin, target_tid, created_at) "
                "VALUES (?, ?, ?, ?)",
                (like_key, liker_uin, target_tid, datetime.now().strftime(_ISO)),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def prune(self, days: int = 30, now: datetime | None = None) -> int:
        """清理过期赞事件记录。"""
        cutoff = (now or datetime.now()) - timedelta(days=days)
        before = self.store.query("SELECT COUNT(*) FROM qzone_likes")[0][0]
        self.store.execute(
            "DELETE FROM qzone_likes WHERE created_at < ?",
            (cutoff.strftime(_ISO),),
        )
        after = self.store.query("SELECT COUNT(*) FROM qzone_likes")[0][0]
        return before - after
