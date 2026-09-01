"""评论观察存储(spec §3.7/§3.9):窗口外评论轮询的去重登记 + 好感度显式事件表。

两张表:qzone_comments(评论去重,幂等主键 comment_key)/ qzone_fav_events
(好感度显式事件——fav_count 已豁免虚拟流,空间互动不依赖 batch_counter)。
风格对齐 seen_store.py(SQLiteStore 薄封装,异常直接抛出不静默)。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from catsitate_core.storage import SQLiteStore

_ISO = "%Y-%m-%dT%H:%M:%S"
_DAY = "%Y-%m-%d"


class CommentSeenStore:
    """qzone_comments / qzone_fav_events 两表的薄封装。"""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def ensure_schema(self) -> None:
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS qzone_comments (
                comment_key TEXT PRIMARY KEY,
                friend_uin TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                retry_count INTEGER NOT NULL DEFAULT 0,
                pending_retry INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 幂等迁移(T9):旧表无 friend_uin 列则补列(存量行取默认空串)
        cols = {r[1] for r in self.store.query("PRAGMA table_info(qzone_comments)")}
        if "friend_uin" not in cols:
            self.store.execute("ALTER TABLE qzone_comments ADD COLUMN friend_uin TEXT NOT NULL DEFAULT ''")
        # 幂等迁移(深度审查 A-N1):旧表无重试计数/待重试标记则补列(存量行取默认 0)
        if "retry_count" not in cols:
            self.store.execute("ALTER TABLE qzone_comments ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "pending_retry" not in cols:
            self.store.execute("ALTER TABLE qzone_comments ADD COLUMN pending_retry INTEGER NOT NULL DEFAULT 0")
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS qzone_fav_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )

    def is_new(self, comment_key: str) -> bool:
        """新评论登记;comment_key=f"{feed_tid}:{comment_tid}:{uin}" 已存在返回
        False=重复,由轮询侧跳过(键缺省同样 False=跳过,与 SeenStore 一致)。

        待重试标记(深度审查 A-N1):被泵回退(注入被拒)的键重见时重新激活并
        返回 True——软回退保留行,retry_count 不随重发现归零。
        """

        if not comment_key:
            return False
        rows = self.store.query(
            "SELECT pending_retry FROM qzone_comments WHERE comment_key = ?", (comment_key,)
        )
        if rows:
            if not int(rows[0][0] or 0):
                return False
            self.store.execute(
                "UPDATE qzone_comments SET pending_retry = 0 WHERE comment_key = ?", (comment_key,)
            )
            return True
        self.store.execute(
            "INSERT INTO qzone_comments (comment_key, created_at) VALUES (?, ?)",
            (comment_key, datetime.now().strftime(_ISO)),
        )
        return True

    def revert(self, comment_key: str) -> None:
        """回退 is_new 登记的键(深度审查 B-4;软回退形态 A-N1):注入被宿主拒绝/
        异常时置待重试标记,令下轮通知轮询重新发现——通知不因一次拒绝永久丢失。

        不删行(A-N1):删行会让下次 is_new 重新 INSERT、retry_count 归零,重试
        上限永不生效(每轮 0→1 无限循环);软回退保留计数,note_retry 达上限后
        调用方不再 revert,登记留存 → is_new 恒 False 跳过。幂等:无该行不报错。
        """

        if not comment_key:
            return
        self.store.execute(
            "UPDATE qzone_comments SET pending_retry = 1 WHERE comment_key = ?", (comment_key,)
        )

    def note_retry(self, comment_key: str) -> int:
        """记录一次注入失败重试,返回累计次数(深度审查 A-N1)。

        计数与登记同表存活(软回退不删行),跨「回退→重发现」循环累计;超过
        上限由调用方放弃(保留登记不再 revert)。键无登记返回 0(防御,不改状态)。
        """

        if not comment_key:
            return 0
        self.store.execute(
            "UPDATE qzone_comments SET retry_count = retry_count + 1 WHERE comment_key = ?",
            (comment_key,),
        )
        rows = self.store.query(
            "SELECT retry_count FROM qzone_comments WHERE comment_key = ?", (comment_key,)
        )
        return int(rows[0][0]) if rows else 0

    def note_bot_comment(self, feed_tid: str, friend_uin: str, bot_text: str, at_iso: str) -> None:
        """登记 bot 自己发出的评论(发出成功后/轮询再见到时调用)。

        键用独立命名空间 "{feed_tid}:bot:{text}",不与好友评论键
        ({feed}:{tid}:{uin})冲突——bot 评论的服务端 tid 发出时不可知,防回环
        消费自己的评论由轮询侧 uin==bot_uin 前置判定承担(T6),本登记留存
        供核查与保留期清理。重复登记刷新时间戳(轮询每轮都会重见仍在
        commentlist 里的自评,幂等)。friend_uin=说说主人(T9):楼中楼轮询
        据此圈定该去哪些好友的说说下找 bot 评论的回复。
        """

        self.store.execute(
            "INSERT OR REPLACE INTO qzone_comments (comment_key, friend_uin, created_at) VALUES (?, ?, ?)",
            (f"{feed_tid}:bot:{bot_text}", friend_uin, at_iso),
        )

    def get_bot_comment_text(self, feed_tid: str) -> str:
        """取 bot 在该说说下最近一条自评的文本(源B通知正文引用,T12);无留痕返回空串。

        bot 评论无服务端 tid(发出时不可知),键为 "{feed_tid}:bot:{text}",
        文本从键后缀剥出(note_bot_comment 同款结构);同说说多条自评取
        created_at 最近(INSERT OR REPLACE 刷新时间戳,重见即最新)。LIKE
        通配符转义防 feed_tid 含 %/_ 时误匹配其它说说的键。
        """

        prefix = f"{feed_tid}:bot:"
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.store.query(
            "SELECT comment_key FROM qzone_comments "
            "WHERE comment_key LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 1",
            (escaped,),
        )
        if not rows:
            return ""
        return str(rows[0][0])[len(prefix):]

    def bot_commented_friends(self, *, days: int = 30) -> list[str]:
        """bot 近 N 天评论过的说说主人去重列表(楼中楼轮询的目标圈定,T9)。

        只认 bot 评论键({feed}:bot:{text});is_new 登记的好友评论行
        friend_uin 恒为默认空串,被 friend_uin != '' 排除。时间下界(深度审查
        D-1):只返回近 N 天(默认 30,与 prune 保留期对齐)的登记——超期旧
        登记不得永远圈定源B轮询范围,好友列表随时间无界增长会拖慢每轮通知轮询。
        """

        cutoff = (datetime.now() - timedelta(days=max(days, 1))).strftime(_ISO)
        rows = self.store.query(
            "SELECT DISTINCT friend_uin FROM qzone_comments "
            "WHERE comment_key LIKE '%:bot:%' AND friend_uin != '' AND created_at >= ?",
            (cutoff,),
        )
        return [str(r[0]) for r in rows]

    def prune(self, days: int = 30, now: datetime | None = None) -> int:
        """评论登记保留期清理(默认 30 天),返回删除条数。

        只清 qzone_comments;qzone_fav_events 不清——last_fav_interaction 是
        衰减计时基准(T8),清历史事件会把基准误回退到 batch 计时。
        """

        now = now or datetime.now()
        cutoff = (now - timedelta(days=max(days, 1))).strftime(_ISO)
        rows = self.store.query("SELECT COUNT(*) FROM qzone_comments WHERE created_at < ?", (cutoff,))
        n = int(rows[0][0]) if rows else 0
        if n:
            self.store.execute("DELETE FROM qzone_comments WHERE created_at < ?", (cutoff,))
        return n

    def fav_event(self, user_id: str, kind: str, text: str) -> None:
        """好感度显式事件(spec §3.9):评论/点赞/出站互动统一入表,日终结算
        素材与衰减计时基准的数据源;day=当天。

        同日去重(深度审查 A-N1):同 user+kind+text+day 只记一条——通知被拒
        回退后重发现时发现侧会重复调用本方法,不去重会重复放大结算素材。"""

        now = datetime.now()
        day = now.strftime(_DAY)
        existing = self.store.query(
            "SELECT 1 FROM qzone_fav_events WHERE day = ? AND user_id = ? AND kind = ? AND text = ? LIMIT 1",
            (day, user_id, kind, text),
        )
        if existing:
            return  # 同日同事件去重(重发现重复,非新互动)
        self.store.execute(
            "INSERT INTO qzone_fav_events (day, user_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (day, user_id, kind, text, now.strftime(_ISO)),
        )

    def fav_events_on(self, day: str, user_id: str) -> list[dict]:
        """某日某人的全部事件,按写入顺序(id 升序)。"""

        rows = self.store.query(
            "SELECT id, day, user_id, kind, text, created_at FROM qzone_fav_events "
            "WHERE day = ? AND user_id = ? ORDER BY id",
            (day, user_id),
        )
        return [
            {"id": r[0], "day": r[1], "user_id": r[2], "kind": r[3], "text": r[4], "created_at": r[5]}
            for r in rows
        ]

    def fav_events_day(self, day: str) -> list[dict]:
        """取某日全部好感度事件(见闻素材:谁与我互动/我做了什么)。"""

        rows = self.store.query(
            "SELECT user_id, kind, text FROM qzone_fav_events WHERE day = ? ORDER BY id", (day,)
        )
        return [{"user_id": r[0], "kind": r[1], "text": r[2]} for r in rows]

    def last_fav_interaction(self, user_id: str) -> str:
        """该人最近一次任一类事件的 created_at(ISO);无事件返回空串。"""

        rows = self.store.query(
            "SELECT created_at FROM qzone_fav_events WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        return str(rows[0][0]) if rows else ""
