"""评论去重+好感度事件存储测试。"""
from datetime import datetime

from catsitate_core.qzone.comment_seen import CommentSeenStore
from catsitate_core.storage import SQLiteStore


def test_is_new_dedup(tmp_path):
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    assert s.is_new("f1:c1:10001") is True
    assert s.is_new("f1:c1:10001") is False
    assert s.is_new("f1:c2:10001") is True


def test_note_bot_comment_prevents_echo(tmp_path):
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    s.note_bot_comment("f1", "3545773341", "我自己的评论", "2026-08-31T12:00:00")
    # bot 评论登记用独立键命名空间,不污染好友评论键({feed}:{tid}:{uin})
    assert s.is_new(f"f1:999:3545773341") is True  # 其它 tid 仍新


def test_fav_events_roundtrip(tmp_path):
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    today = datetime.now().strftime("%Y-%m-%d")  # fav_event 以当天为 day,动态取值防日期耦合
    s.fav_event("10001", "COMMENT", "小明评论了你的说说:好看")
    s.fav_event("10001", "OUT_COMMENT", "你评论了小明的说说")
    rows = s.fav_events_on(today, "10001")
    assert len(rows) == 2 and rows[0]["kind"] == "COMMENT"
    assert s.last_fav_interaction("10001") >= "2026-08-31"
    assert s.last_fav_interaction("99999") == ""


def test_note_bot_comment_stores_friend_uin(tmp_path):
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    s.note_bot_comment("f1", "3298178030", "bot评论", "2026-08-31T12:00:00")
    s.note_bot_comment("f2", "3341299096", "另一条", "2026-08-31T12:01:00")
    uins = s.bot_commented_friends()
    assert set(uins) == {"3298178030", "3341299096"}


def test_bot_commented_friends_excludes_plain_comments(tmp_path):
    """反查只认 bot 评论键:is_new 登记的好友评论行(friend_uin 空串)与
    friend_uin 为空的自评登记都不进结果(空 target 无法圈定楼中楼轮询目标)。"""
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    s.is_new("f1:c1:10001")                       # 好友评论(friend_uin 默认空串)
    s.note_bot_comment("f2", "", "旧格式自评", "2026-08-31T12:00:00")  # 空主人
    s.note_bot_comment("f3", "3298178030", "有效", "2026-08-31T12:01:00")
    assert s.bot_commented_friends() == ["3298178030"]


def test_bot_commented_friends_migration(tmp_path):
    # 旧表无 friend_uin 列 → ensure_schema 迁移补列
    store = SQLiteStore(tmp_path / "old.db")
    store.execute("CREATE TABLE qzone_comments (comment_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    s = CommentSeenStore(store)
    s.ensure_schema()
    s.note_bot_comment("f1", "123", "x", "2026-08-31T12:00:00")
    assert "123" in s.bot_commented_friends()


def test_get_bot_comment_text(tmp_path):
    """T12 源B通知正文引用:取 bot 在该说说下最近一条自评文本(键后缀剥出);
    无留痕返回空串;LIKE 通配符转义防 feed_tid 含 %/_ 时误匹配其它说说。"""
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    assert s.get_bot_comment_text("f1") == ""  # 无留痕 → 空串
    s.note_bot_comment("f1", "30000", "第一条", "2026-08-31T10:00:00")
    s.note_bot_comment("f1", "30000", "第二条(更近)", "2026-08-31T11:00:00")
    s.note_bot_comment("f2", "30000", "别的说说", "2026-08-31T12:00:00")
    assert s.get_bot_comment_text("f1") == "第二条(更近)"  # 取 created_at 最近
    assert s.get_bot_comment_text("f2") == "别的说说"
    # LIKE 通配安全:feed_tid 含 _ 不误匹配单字通配形态("axb" 不等于 "a_b")
    s.note_bot_comment("axb", "30000", "被误匹配的", "2026-08-31T13:00:00")
    assert s.get_bot_comment_text("a_b") == ""


def test_prune(tmp_path):
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    s.is_new("old:c:u")
    s.store.execute("UPDATE qzone_comments SET created_at = '2026-07-01T00:00:00'")
    now = datetime(2026, 8, 31, 12, 0, 0)
    assert s.prune(30, now) == 1  # 7月1日超出30天保留期
