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
    # 下界由当天动态推导(登记时刻即当下,不早于当天):任意日期运行均成立,不与真实日历耦合
    assert s.last_fav_interaction("10001") >= today
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


# ---------- H-2 滚动窗查询(2026-09-03 v1 清理:结算取数与自然日解耦) ----------


def test_fav_events_since_daytime_event_reachable_from_yesterday_bound(tmp_path):
    """H-2 ①:白天记录的空间事件,以「昨日 window_start」为下界能取到——
    结算素材与聊天消息同窗口口径(favorability.window_start),不受自然日切换影响。"""
    from datetime import timedelta

    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    s.fav_event("10001", "COMMENT", "小明评论了你的说说:好看")
    yesterday_ws = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = s.fav_events_since("10001", yesterday_ws)
    assert any(e["kind"] == "COMMENT" and e["user_id"] == "10001" for e in rows)
    # 他人事件不串人;下界晚于事件时刻取不到
    assert s.fav_events_since("10002", yesterday_ws) == []
    future = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    assert s.fav_events_since("10001", future) == []


def test_fav_events_since_orders_by_created_at(tmp_path):
    """H-2:fav_events_since 按 created_at 升序(滚动窗语义,与 fav_events_on
    按写入序 id 不同)——结算素材按事件真实时间排序。"""
    from datetime import timedelta

    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    s.fav_event("10001", "COMMENT", "白天的事件")
    s.fav_event("10001", "LIKE", "更早的赞")
    s.store.execute(
        "UPDATE qzone_fav_events SET created_at = ? WHERE user_id = '10001' AND kind = 'LIKE'",
        ((datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),),
    )
    rows = s.fav_events_since("10001", (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"))
    assert [r["kind"] for r in rows] == ["LIKE", "COMMENT"]


def test_fav_events_on_today_misses_last_night_but_since_yesterday_gets(tmp_path):
    """H-2 ②(语义差异锁定):fav_events_on 按登记时写入的 day 自然日匹配;
    fav_events_since 按 created_at 滚动窗——昨晚 23:00 记录的事件,今天自然日
    取不到,昨日下界滚动窗能取到(见闻素材已改 fav_events_window 滚动窗,
    2026-09-04 翻案 H-2 自然日旧裁定,不再与 fav_events_on 同口径)。"""
    from datetime import timedelta

    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    now = datetime.now()
    yday_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    s.fav_event("10001", "COMMENT", "小红 评论了你的说说「心情」: 好看")
    # 回拨为昨晚 23:00 记录(day=登记日;fav_event 写入时 day=今天,须同步改 day 保持行内自洽)
    s.store.execute(
        "UPDATE qzone_fav_events SET day = ?, created_at = ? WHERE user_id = '10001'",
        (yday_day, f"{yday_day}T23:00:00"),
    )
    assert s.fav_events_on(now.strftime("%Y-%m-%d"), "10001") == []
    assert s.fav_events_since("10001", f"{yday_day}T22:00:00") != []


def test_fav_events_window_all_users_within_lookback(tmp_path):
    """C-N1:fav_events_window 不限 user 的回看窗查询(日终候选并集数据源)——
    窗口内(含恰好落在下界上的事件,>= 语义)全部用户返回,窗口外不返回。"""
    from datetime import timedelta

    s = CommentSeenStore(SQLiteStore(tmp_path / "t.db"))
    s.ensure_schema()
    now = datetime.now()
    fmt = "%Y-%m-%dT%H:%M:%S"
    since = (now - timedelta(hours=24)).strftime(fmt)
    s.fav_event("10001", "LIKE", "窗口内A")
    s.fav_event("10002", "COMMENT", "窗口内B")
    s.fav_event("30000", "LIKE", "恰在下界")
    s.fav_event("40000", "LIKE", "窗口外C")
    s.store.execute(
        "UPDATE qzone_fav_events SET created_at = ? WHERE user_id = '30000'", (since,)
    )
    s.store.execute(
        "UPDATE qzone_fav_events SET created_at = ? WHERE user_id = '40000'",
        ((now - timedelta(hours=30)).strftime(fmt),),
    )
    rows = s.fav_events_window(since)
    assert {r["user_id"] for r in rows} == {"10001", "10002", "30000"}  # 下界含等于,窗口外排除
