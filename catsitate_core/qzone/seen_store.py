"""空间动态去重存储:state=queued(已入队)/seen(已成功注入);interacted 独立标记。

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
                author_nickname TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'seen')),
                interacted INTEGER NOT NULL DEFAULT 0,
                injected_at TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 旧库迁移(M-2/通知 reply 段):CREATE IF NOT EXISTS 不更新既有表,PRAGMA 查列缺则 ALTER 补
        columns = {r[1] for r in self.store.query("PRAGMA table_info(qzone_feeds)")}
        if "author_nickname" not in columns:
            self.store.execute("ALTER TABLE qzone_feeds ADD COLUMN author_nickname TEXT NOT NULL DEFAULT ''")
        if "message_id" not in columns:
            # 注入消息 id(通知 reply 段引用原说说注入消息,napcat quote 式上下文关联)
            self.store.execute("ALTER TABLE qzone_feeds ADD COLUMN message_id TEXT NOT NULL DEFAULT ''")

    def mark_queued(
        self, tid: str, *, abstime: str, author_uin: str, summary: str, author_nickname: str = ""
    ) -> bool:
        """入队标记;tid 已存在(任意状态)返回 False=重复,由调用方跳过。"""

        if not tid:
            return False
        rows = self.store.query("SELECT 1 FROM qzone_feeds WHERE tid = ?", (tid,))
        if rows:
            return False
        self.store.execute(
            "INSERT INTO qzone_feeds (tid, abstime, author_uin, author_nickname, summary, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (tid, abstime, author_uin, author_nickname, summary, datetime.now().strftime(_ISO)),
        )
        return True

    def is_new_candidate(self, tid: str) -> bool:
        """纯查存在性(不登记)——统一时间线发现层用(发现≠注入,不提前标 queued)。

        与 mark_queued 的「登记并返回是否新」相对:发现层阶段只读判重,登记留给
        充实层 mark_queued(否则发现层预占主键会让充实层判重跳过,动态永不出队)。
        """

        rows = self.store.query("SELECT 1 FROM qzone_feeds WHERE tid = ?", (tid,))
        return not rows

    def mark_seen(self, tid: str, injected_at_iso: str, message_id: str | None = None) -> None:
        """标记已见;message_id 记录注入时的消息 id(通知 reply 段关联原说说用)。

        三态(2026-09-03 复审修复):真实 id=覆写注入锚;None(缺省)=只置 seen,
        经 COALESCE 保留旧锚(detail 查看路径——不该抹掉浏览注入落的引用锚);
        空串=显式清除覆写。
        """

        self.store.execute(
            "UPDATE qzone_feeds SET state = 'seen', injected_at = ?, "
            "message_id = COALESCE(?, message_id) WHERE tid = ?",
            (injected_at_iso, message_id, tid),
        )

    def get_message_id(self, tid: str) -> str:
        """查 tid 注入时的消息 id(通知注入据此构造 reply 段引用原说说)。

        未登记 tid / 旧库未记录 → 空串(调用方按无 reply 段回退,不静默臆造)。
        """

        rows = self.store.query("SELECT message_id FROM qzone_feeds WHERE tid = ?", (tid,))
        return str(rows[0][0]) if rows else ""

    def get_summary(self, tid: str) -> str:
        """按 tid 取已登记说说的摘要(通知 quote/点赞标题素材)。

        未登记 tid(他人说说未走本地发布回注)→ 空串(调用方按无标题回退)。
        """

        rows = self.store.query("SELECT summary FROM qzone_feeds WHERE tid = ?", (tid,))
        return str(rows[0][0]) if rows else ""

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
            "SELECT tid, abstime, author_uin, author_nickname, summary, injected_at FROM qzone_feeds "
            "WHERE state = 'seen' AND injected_at >= ? ORDER BY injected_at DESC LIMIT ?",
            (since, max(limit, 1)),
        )
        return [
            {
                "tid": r[0],
                "abstime": r[1],
                "author_uin": r[2],
                "author_nickname": r[3],
                "summary": r[4],
                "injected_at": r[5],
            }
            for r in rows
        ]
