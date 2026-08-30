"""空间动态去重存储(spec §2.15):state=queued(已入队)/seen(已成功注入);interacted 独立标记。

seen 的语义是「成功进入过 planner 上下文」——窗口结束仍未注入的 queued 行回退删除
(下个窗口重新可见),防止窗口尾丢弃的动态永久丢失。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from catsitate_core.storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"


class SeenStore:
    """qzone_feeds 表的薄封装(幂等主键 tid)。"""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def ensure_schema(self) -> None:
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS qzone_feeds (
                tid TEXT PRIMARY KEY,
                abstime TEXT NOT NULL DEFAULT '',
                author_uin TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'seen')),
                interacted INTEGER NOT NULL DEFAULT 0,
                injected_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )

    def mark_queued(self, tid: str, *, abstime: str, author_uin: str, summary: str) -> bool:
        """入队标记;tid 已存在(任意状态)返回 False=重复,由调用方跳过。"""

        if not tid:
            return False
        rows = self.store.query("SELECT 1 FROM qzone_feeds WHERE tid = ?", (tid,))
        if rows:
            return False
        self.store.execute(
            "INSERT INTO qzone_feeds (tid, abstime, author_uin, summary, state, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (tid, abstime, author_uin, summary[:120], datetime.now().strftime(_ISO)),
        )
        return True

    def mark_seen(self, tid: str, injected_at_iso: str) -> None:
        self.store.execute("UPDATE qzone_feeds SET state = 'seen', injected_at = ? WHERE tid = ?", (injected_at_iso, tid))

    def mark_interacted(self, tid: str) -> None:
        self.store.execute("UPDATE qzone_feeds SET interacted = 1 WHERE tid = ?", (tid,))

    def revert_pending(self) -> int:
        """窗口结束:queued 行删除(回退未读),返回回退条数。"""

        rows = self.store.query("SELECT COUNT(*) FROM qzone_feeds WHERE state = 'queued'")
        n = int(rows[0][0]) if rows else 0
        if n:
            self.store.execute("DELETE FROM qzone_feeds WHERE state = 'queued'")
        return n

    def recent_seen(self, *, limit: int, days: int, now: datetime) -> list[dict]:
        """近 N 天已见动态(注入块摘要用),按注入时间倒序。"""

        since = (now - timedelta(days=max(days, 1))).strftime(_ISO)
        rows = self.store.query(
            "SELECT tid, abstime, author_uin, summary, injected_at FROM qzone_feeds "
            "WHERE state = 'seen' AND injected_at >= ? ORDER BY injected_at DESC LIMIT ?",
            (since, max(limit, 1)),
        )
        return [
            {"tid": r[0], "abstime": r[1], "author_uin": r[2], "summary": r[3], "injected_at": r[4]}
            for r in rows
        ]
