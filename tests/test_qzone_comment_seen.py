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
    s.note_bot_comment("f1", "我自己的评论", "2026-08-31T12:00:00")
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


def test_prune(tmp_path):
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    s.is_new("old:c:u")
    s.store.execute("UPDATE qzone_comments SET created_at = '2026-07-01T00:00:00'")
    now = datetime(2026, 8, 31, 12, 0, 0)
    assert s.prune(30, now) == 1  # 7月1日超出30天保留期
