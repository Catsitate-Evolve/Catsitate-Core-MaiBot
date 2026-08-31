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
    from datetime import timedelta

    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    now = datetime.now()
    # bot_commented_friends 带时间下界(D-1):登记时刻须在保留期内,取当前时间防日期耦合
    s.note_bot_comment("f1", "3298178030", "bot评论", (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S"))
    s.note_bot_comment("f2", "3341299096", "另一条", (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S"))
    uins = s.bot_commented_friends()
    assert set(uins) == {"3298178030", "3341299096"}


def test_bot_commented_friends_excludes_plain_comments(tmp_path):
    """反查只认 bot 评论键:is_new 登记的好友评论行(friend_uin 空串)与
    friend_uin 为空的自评登记都不进结果(空 target 无法圈定楼中楼轮询目标)。"""
    from datetime import timedelta

    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    s.is_new("f1:c1:10001")                       # 好友评论(friend_uin 默认空串)
    s.note_bot_comment("f2", "", "旧格式自评", fresh)  # 空主人
    s.note_bot_comment("f3", "3298178030", "有效", fresh)
    assert s.bot_commented_friends() == ["3298178030"]


def test_bot_commented_friends_migration(tmp_path):
    # 旧表无 friend_uin 列 → ensure_schema 迁移补列
    from datetime import timedelta

    store = SQLiteStore(tmp_path / "old.db")
    store.execute("CREATE TABLE qzone_comments (comment_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    s = CommentSeenStore(store)
    s.ensure_schema()
    s.note_bot_comment("f1", "123", "x", (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S"))
    assert "123" in s.bot_commented_friends()


def test_bot_commented_friends_days_cutoff(tmp_path):
    """深度审查 D-1:反查带时间下界——超过保留期的旧登记不返回(限制源B轮询范围,
    bot 一个月前评论过的好友不该永远进每轮通知轮询);days 参数可收紧窗口。"""
    from datetime import timedelta

    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    now = datetime.now()
    s.note_bot_comment("f_old", "30000", "旧评论", (now - timedelta(days=61)).strftime("%Y-%m-%dT%H:%M:%S"))
    s.note_bot_comment("f_new", "30001", "新评论", (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S"))
    # 默认 days=30:61 天前的登记超窗不返回
    assert s.bot_commented_friends() == ["30001"]
    # days=1:30 小时前的登记也超窗——只剩空
    assert s.bot_commented_friends(days=1) == []
    # days 放宽到 90:旧登记重新可见
    assert sorted(s.bot_commented_friends(days=90)) == ["30000", "30001"]


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


def test_revert(tmp_path):
    """深度审查 B-4:回退去重键——通知注入被宿主拒绝后回退 is_new 登记的键,
    下轮通知轮询重新发现(is_new 再次 True),通知不因一次拒绝永久丢失。"""
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    key = "feed1:c1:20000"
    assert s.is_new(key) is True
    assert s.is_new(key) is False  # 已登记
    s.revert(key)  # 注入被拒:回退
    assert s.is_new(key) is True  # 下轮重新发现
    s.revert("不存在的键")  # 幂等:无该行不报错


def test_notify_retry_gives_up_after_three(tmp_path):
    """深度审查 A-N1:重试计数跨软回退累计——前两次被拒不丢(回退待下轮重发现),
    第 3 次仍被拒保留登记放弃(is_new 恒 False),不再无限重注入。"""
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    key = "feed1:c1:20000"
    for attempt in (1, 2):
        assert s.is_new(key) is True        # 首次发现 / 回退后重发现
        assert s.note_retry(key) == attempt  # 计数跨「回退→重发现」累计,不随重发现归零
        s.revert(key)                        # 泵侧:未达上限 → 回退待重试
    assert s.is_new(key) is True  # 第 3 次发现(第 2 次回退后)
    assert s.note_retry(key) == 3  # 达上限
    # 泵侧不再 revert:登记保留 → is_new 判 False,下轮轮询跳过
    assert s.is_new(key) is False
    assert s.is_new(key) is False  # 恒 False(非一次性语义)


def test_retry_columns_migration(tmp_path):
    """深度审查 A-N1 迁移:旧表无 retry_count/pending_retry 列 → ensure_schema 补列。"""
    store = SQLiteStore(tmp_path / "old.db")
    store.execute("CREATE TABLE qzone_comments (comment_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    s = CommentSeenStore(store)
    s.ensure_schema()
    s.is_new("f1:c1:20000")
    assert s.note_retry("f1:c1:20000") == 1  # 补列后计数可用
    s.revert("f1:c1:20000")
    assert s.is_new("f1:c1:20000") is True  # 软回退语义在迁移表上同样成立


def test_fav_event_same_day_dedup(tmp_path):
    """深度审查 A-N1:同日同 user+kind+text 只记一条——通知被拒回退后重发现时,
    发现侧会再次调用 fav_event,重复入库会放大结算素材与衰减计时。"""
    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    today = datetime.now().strftime("%Y-%m-%d")
    s.fav_event("20000", "COMMENT", "小红 评论了你的说说「心情」: 好看")
    s.fav_event("20000", "COMMENT", "小红 评论了你的说说「心情」: 好看")  # revert 重发现的重复
    s.fav_event("20000", "COMMENT", "小红 评论了你的说说「心情」: 再看一次")  # 不同内容正常记
    s.fav_event("20000", "OUT_LIKE", "你点赞了 20000 的说说")  # 不同 kind 正常记
    rows = s.fav_events_on(today, "20000")
    assert len(rows) == 3
