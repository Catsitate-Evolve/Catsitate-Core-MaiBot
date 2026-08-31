"""QQ空间组合层行为测试(终审修复波 I1/I4):plugin 接线的行为断言,非源码字符串。

_StubCtx 模式参照 test_integration.py:离线装配插件实例,依赖全部注入桩,
只验证「组合层」行为——窗口开启作废残留评论意图 / 网关出站意图消费 /
统一通知轮询三重守卫(自评跳过+判重+意图占用,T11 重写)与源B楼中楼路由。
终审修复波追加:qzone_like 通知隔离(I1)/源B请求间隔(I2)/窗口尾通知残留告警(I3)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from catsitate_core.config import CatsitateConfig
from catsitate_core.qzone.comment_seen import CommentSeenStore
from catsitate_core.qzone.discovery import FeedDiscovery
from catsitate_core.qzone.injector import FeedInjector
from catsitate_core.qzone.protocol import FeedItem
from catsitate_core.qzone.routing import OutboundIntent
from catsitate_core.qzone.seen_store import SeenStore
from catsitate_core.qzone.wire import CommentItem
from catsitate_core.storage import SQLiteStore

BOT_UIN = "10000"


class _CollectLogger:
    """收集日志的 stub logger。"""

    def __init__(self, logs):
        self._logs = logs

    def _record(self, level, a, k):
        self._logs.append((level, list(a)))

    def info(self, *a, **k):
        self._record("info", a, k)

    def warning(self, *a, **k):
        self._record("warning", a, k)

    def exception(self, *a, **k):
        self._record("exception", a, k)

    def error(self, *a, **k):
        self._record("error", a, k)

    def debug(self, *a, **k):
        self._record("debug", a, k)


class _StubGateway:
    """记录 route_message 注入调用的网关桩。"""

    def __init__(self):
        self.calls = []

    async def route_message(self, name, msg):
        self.calls.append((name, msg))
        return True


class _StubCtx:
    """组合层测试的最小 ctx 面:logger + gateway。"""

    def __init__(self, logs):
        self.logger = _CollectLogger(logs)
        self.gateway = _StubGateway()


class _StubCommentClient:
    """评论轮询输入桩:记录取数次数,get_own_feed_comments 返回固定输入。"""

    def __init__(self, comments, ctx_map):
        self._comments = comments
        self._ctx_map = ctx_map
        self.fetches = 0

    async def get_own_feed_comments(self, *, bot_uin, num=10):
        del bot_uin, num
        self.fetches += 1
        return self._comments, self._ctx_map

    async def get_unified_timeline(self, *, count=20):
        del count
        return []  # 发现层空:源B零拉取,聚焦源A行为


class _StubWriteClient:
    """出站动作桩:记录 do_comment/do_reply 调用,恒成功。"""

    def __init__(self):
        self.comment_calls = []
        self.reply_calls = []

    async def do_comment(self, *, fid, target_qq, content):
        self.comment_calls.append((fid, target_qq, content))
        return True

    async def do_reply(self, *, fid, target_qq, comment_tid, comment_uin, comment_nick, content):
        self.reply_calls.append((fid, target_qq, comment_tid, comment_uin, comment_nick, content))
        return True


def _make_plugin(tmp_path):
    """离线装配最小插件实例(qzone 相关状态手工补齐,不跑 on_load)。"""

    import plugin as plugin_mod

    logs: list = []
    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(logs)
    p._plugin_config_instance = CatsitateConfig()
    p.config.sleep.enabled = False  # 不依赖 self.sleep
    p.config.favorability.bot_user_id = BOT_UIN
    p._qzone_available = True
    p._qzone_outbound_intent = None
    p._qzone_seq = 0
    p._qzone_pump_lock = asyncio.Lock()  # 泵互斥锁(on_load 装配,离线测试手工补)
    p._background_tasks = set()  # 后台任务引用集(tick 派发 feeds/scan 用,on_load 装配)
    # 与生产同构:seen/comment_seen 共用一个 SQLiteStore(_qzone_data_prune 走 self.store)
    store = SQLiteStore(tmp_path / "catsitate.db")
    p.store = store
    p.qzone_seen = SeenStore(store)
    p.qzone_seen.ensure_schema()
    p.qzone_comment_seen = CommentSeenStore(store)
    p.qzone_comment_seen.ensure_schema()
    p.qzone_injector = FeedInjector(decision_window_s=75)
    p._qzone_session_ids = set()  # 实例级覆盖(类属性为共享 set,防测试间状态泄漏)
    p.qzone_client = _StubUnifiedClient([])  # 默认发现层桩:空时间线(各测试按需覆盖)
    p.logs = logs  # 测试侧便捷引用(非插件属性约定)
    return p


class _StubUnifiedClient:
    """发现层输入桩:get_unified_timeline 返回固定列表(默认空),记录调用参数。"""

    def __init__(self, discoveries):
        self._discoveries = discoveries
        self.discovery_calls = 0

    async def get_unified_timeline(self, *, count=20):
        del count
        self.discovery_calls += 1
        return list(self._discoveries)


def _patch_sleep(monkeypatch, record: list) -> None:
    """把 asyncio.sleep 换成记录桩(源B请求间隔断言用;不让测试真等 2 秒)。"""

    async def _fake_sleep(delay, *a, **k):
        record.append(float(delay))

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


def test_poll_tick_voids_pending_comment_intent_on_window_start(tmp_path):
    """终审 I1:窗口开始时作废未消费的 comment_reply 意图并告警——否则注入泵
    覆盖为 reaction 后,迟到的评论回复将错发为新动态的头评;窗口仍正常开启(不饿死)。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "qzone": True,
    }]}
    p._qzone_outbound_intent = OutboundIntent(
        kind="comment_reply", tid="feed1", target_qq=BOT_UIN,
        comment_tid="ct1", comment_uin="20000", comment_nick="小红",
    )

    # 发现层空时间线(默认桩):窗口开始分支后本轮提前返回,足以断言意图处置
    asyncio.run(p._qzone_poll_feeds())
    assert p._qzone_outbound_intent is None  # 残留评论意图作废
    assert p.qzone_injector.window_active is True  # 窗口正常开启(不跳过注入)
    assert any(
        level == "warning" and "窗口开始,未消费评论意图作废" in str(a[0]) and a[1] == "comment_reply"
        for level, a in p.logs
    )


# ---- I4:组合层行为测试(网关出站意图消费/无意图拒发/评论轮询三重守卫) ----


def _make_gateway_plugin(tmp_path):
    """qzone_gateway 直调装配:写路径客户端桩 + 真实 seen/comment_seen 存储。"""

    p = _make_plugin(tmp_path)
    p.qzone_client = _StubWriteClient()
    return p


def _out_msg(text):
    return {"raw_message": [{"type": "text", "data": text}]}


def test_gateway_comment_success_consumes_intent_and_marks_interacted(tmp_path):
    """I4-1:reaction 意图下出站→客户端 do_comment 被调(参数对位),远端成功后
    意图即刻置 None,seen.mark_interacted 落库(interacted=1)且自评登记入评论表。"""

    p = _make_gateway_plugin(tmp_path)
    p.qzone_seen.mark_queued("t1", abstime="1750000000", author_uin="10001", summary="今天天气好")
    p._qzone_outbound_intent = OutboundIntent(kind="reaction", tid="t1", target_qq="10001")
    res = asyncio.run(p.qzone_gateway(message=_out_msg("好看!"), route={}, metadata={}))
    assert res["success"] is True and res.get("external_message_id")
    assert p.qzone_client.comment_calls == [("t1", "10001", "好看!")]  # Stub client 记录调用
    assert p._qzone_outbound_intent is None  # 意图一次性消费(远端成功即刻置空)
    rows = p.qzone_seen.store.query("SELECT interacted FROM qzone_feeds WHERE tid = 't1'")
    assert rows and rows[0][0] == 1  # mark_interacted 已落库
    keys = p.qzone_comment_seen.store.query(
        "SELECT comment_key FROM qzone_comments WHERE comment_key LIKE 't1:bot:%'"
    )
    assert keys  # 自评登记(note_bot_comment)已入评论表


def test_gateway_rejects_when_intent_consumed(tmp_path):
    """I4-2:意图已消费(None)→出站 reject:返回 success=False 且 error 含「无出站意图」,
    写路径客户端零调用(不会误发任何评论)。"""

    p = _make_gateway_plugin(tmp_path)
    p._qzone_outbound_intent = None
    res = asyncio.run(p.qzone_gateway(message=_out_msg("迟到的回复"), route={}, metadata={}))
    assert res["success"] is False
    assert "无出站意图" in str(res.get("error", ""))
    assert p.qzone_client.comment_calls == [] and p.qzone_client.reply_calls == []  # 零写调用


def test_notify_poll_self_skip_dedup_and_intent_occupied(tmp_path):
    """I4-3(承 T11 重写,深度审查 B-1 守卫改判 awaiting):统一通知轮询三重守卫——
    ①bot 自评跳过不注入;②is_new 判重(二次轮询不重注入);③awaiting 占用时
    不取数(上一条还在等回复;意图在超时/窗口边界已清,awaiting 才是真互斥信号)。"""

    import time as _time

    comments = {"feed1": [
        CommentItem(comment_tid="c0", uin=BOT_UIN, nickname="我", content="自评", create_time=""),
        # 空 comment_tid 的畸形评论(T11 审查遗留):跳过不构造通知(防空 tid 畸形请求)
        CommentItem(comment_tid="", uin="20001", nickname="小蓝", content="畸形评论",
                    create_time=str(int(_time.time()))),
        CommentItem(comment_tid="c1", uin="20000", nickname="小红", content="好友评论",
                    create_time=str(int(_time.time()))),
    ]}
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()  # 通知经泵注入需窗口开启
    p.qzone_client = _StubCommentClient(comments, {"feed1": "今天的心情"})

    # ①首轮:自评跳过(登记不注入),好友评论通知经泵注入恰好 1 条
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_info"]["user_info"]["user_id"] == "20000"
    intent = p._qzone_outbound_intent
    assert intent is not None and intent.kind == "comment_reply"
    assert (intent.tid, intent.target_qq, intent.comment_tid) == ("feed1", BOT_UIN, "c1")

    # ②awaiting 占用(轮未完成):不取数不注入(上一条通知还在等回复,不叠加)
    assert p.qzone_injector.awaiting_feed is not None
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1 and p.qzone_client.fetches == 1

    # ③轮完成后重扫:释放 awaiting+清意图(bot 回复已消费),自评仍跳过,
    # 好友评论 is_new 判重 → 不重注入(只多了一次取数)
    p.qzone_injector.on_turn_complete(_time.monotonic())
    p._qzone_outbound_intent = None
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    assert p.qzone_client.fetches == 2  # 取数发生(判重生效,不是早退)
    assert p.qzone_comment_seen.is_new("feed1:c1:20000") is False  # 已登记,判重依据


def test_notify_scan_guard_awaits_even_with_intent_cleared(tmp_path):
    """深度审查 B-1:守卫判 awaiting 而非意图——意图被清(超时/窗口边界)但 awaiting
    仍在(轮未完成)时,通知轮询仍不取数不叠加;awaiting 释放后才恢复。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue_priority([FeedItem(
        tid="notify_comment_fx_cx", abstime="1750000000", uin="20000",
        nickname="小红", content="(通知) 一", source="notify",
    )])
    popped = p.qzone_injector.next_to_inject(_time.monotonic())
    assert popped is not None
    p.qzone_injector.mark_injected(popped.tid, _time.monotonic())
    p._qzone_outbound_intent = None  # 意图已被清(模拟超时清意图/窗口边界)
    fetches: list = []

    class _ProbeClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([])  # 发现层空:不进源B拉取

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            fetches.append(1)
            return {}, {}

    p.qzone_client = _ProbeClient()
    asyncio.run(p._qzone_notify_scan())
    assert fetches == []  # awaiting 未释放:意图虽空仍不取数(旧 intent 守卫会漏判)
    p.qzone_injector.on_turn_complete(_time.monotonic())
    asyncio.run(p._qzone_notify_scan())
    assert fetches == [1]  # awaiting 释放后恢复取数


def test_notify_poll_source_b_reply_routes_to_friend_thread(tmp_path, monkeypatch):
    """T11/T4-② 源B:发现层显示被评论好友有新活动→拉取其说说→楼中楼回复(list_3)
    → 通知注入 → 意图 comment_reply 指向好友说说(target_qq=好友,commentId=回复 tid)。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)  # 源B每个好友前固定 2 秒间隔(I2),桩掉不真等

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 源B目标圈定:bot 曾在好友 30000 的说说下发过评论(note_bot_comment 留痕)
    # 登记时刻取当前时间(bot_commented_friends 带时间下界 D-1,硬编码日期会超窗失效)
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "我的评论", fresh)
    raw = {"usrinfo": {"uin": "30000"}, "msglist": [{"tid": "ffeed1", "commentlist": [
        # 空 reply_tid 的畸形回复(T11 审查遗留):跳过不构造通知(防空 tid 畸形请求)
        {"tid": "bc1", "uin": BOT_UIN, "list_3": [
            {"tid": "", "uin": "30000", "name": "阿好", "content": "畸形回复",
             "create_time": str(int(_time.time()))},
        ]},
        {"tid": "bc1", "uin": BOT_UIN, "list_3": [
            {"tid": "rr1", "uin": "30000", "name": "阿好", "content": "说得对",
             "create_time": str(int(_time.time()))},
        ]},
    ]}]}

    class _StubNotifyClient(_StubUnifiedClient):
        """源B输入桩:发现层显示 30000 有新活动,其原始载荷带一条楼中楼回复。"""

        def __init__(self):
            super().__init__([
                FeedDiscovery(tid="ffeed1", uin="30000", nickname="阿好",
                              abstime=str(int(_time.time())), appid=311),
            ])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            assert target_uin == "30000"  # 只拉「被评论过+发现层活跃」的好友
            return raw

    p.qzone_client = _StubNotifyClient()
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    msg = p._ctx.gateway.calls[0][1]
    assert msg["message_info"]["user_info"]["user_id"] == "30000"
    text = msg["raw_message"][0]["data"]
    assert "(通知) 阿好 回复了你在他人说说下的评论" in text and "阿好: 说得对" in text
    assert "你曾评论: 我的评论" in text  # T12 源B正文补 bot 原评论留痕(note_bot_comment 取回)
    assert "notify_reply_ffeed1_rr1" in msg["message_id"]
    intent = p._qzone_outbound_intent
    assert intent is not None and intent.kind == "comment_reply"
    # 源B 意图对位:fid=好友说说,target_qq=好友(说说主人),commentId=回复 tid
    assert (intent.tid, intent.target_qq, intent.comment_tid, intent.comment_uin) == \
        ("ffeed1", "30000", "rr1", "30000")
    # 楼中楼回复键已登记(下轮判重,不重复通知)
    assert p.qzone_comment_seen.is_new("ffeed1:bc1:reply:rr1") is False


# ---- 终审修复波 I1/I2/I3:组合层行为测试 ----


class _StubLikeClient:
    """点赞路径桩:记录 do_like 调用,恒成功。"""

    def __init__(self):
        self.like_calls = []

    async def do_like(self, *, fid, target_qq):
        self.like_calls.append((fid, target_qq))
        return True


def test_qzone_like_rejects_notify_awaiting(tmp_path):
    """终审 I1:awaiting 是 P1 通知(合成 tid)时 qzone_like 显式拒绝——
    不向 qzone 发畸形点赞请求(写客户端零调用),返回通知不可点赞提示。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.qzone_client = _StubLikeClient()
    p._qzone_session_ids.add("s1")
    # 通知项经真实入队→弹出→注入链进入 awaiting(source="notify" 完整保留)
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue_priority([FeedItem(
        tid="notify_comment_feed1_c1", abstime="1750000000", uin="20000",
        nickname="小红", content="(通知) 小红 评论了你的说说", source="notify",
    )])
    popped = p.qzone_injector.next_to_inject(_time.monotonic())
    assert popped is not None and popped.source == "notify"
    p.qzone_injector.mark_injected(popped.tid, _time.monotonic())

    res = asyncio.run(p.qzone_like(message_id="", stream_id="s1"))
    assert res == "当前是互动通知,不是说说,无法点赞。"
    assert p.qzone_client.like_calls == []  # 零写调用(不对合成 tid 发畸形请求)


def test_notify_poll_source_b_spaces_friend_requests(tmp_path, monkeypatch):
    """终审 I2(承 M3):源B活跃好友逐个拉取带 2 秒防风控间隔(每个好友前固定,
    含首个——源B发现层与源A都刚发过 HTTP,不再区分源A有无结果)。拉取范围按
    「发现层活跃 ∩ bot 评论过」圈定,且按时间线顺序去重。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)

    p = _make_plugin(tmp_path)
    # bot 曾在两位好友说说下评论:与发现层求交后圈定 30000/30001
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", fresh)
    p.qzone_comment_seen.note_bot_comment("ffeed2", "30001", "评论二", fresh)

    pulls: list = []
    now_s = str(int(_time.time()))

    class _StubSpacingClient(_StubUnifiedClient):
        """源A空结果;发现层含两好友(30000 重复出现验证去重)+未评论好友+bot 自己;
        源B记录拉取顺序,载荷无楼中楼回复(只断言圈定与间隔行为)。"""

        def __init__(self):
            super().__init__([
                FeedDiscovery(tid="fa1", uin="30000", nickname="好友甲", abstime=now_s, appid=311),
                FeedDiscovery(tid="fa2", uin="30001", nickname="好友乙", abstime=now_s, appid=311),
                FeedDiscovery(tid="fa3", uin="30000", nickname="好友甲", abstime=now_s, appid=311),  # 同好友再一条
                FeedDiscovery(tid="fb1", uin="40000", nickname="陌生好友", abstime=now_s, appid=311),  # 未评论过
                FeedDiscovery(tid="fme", uin=BOT_UIN, nickname="我", abstime=now_s, appid=311),  # bot 自己(源A覆盖)
            ])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            del num
            pulls.append(target_uin)
            return {"usrinfo": {"uin": target_uin}, "msglist": []}

    p.qzone_client = _StubSpacingClient()
    asyncio.run(p._qzone_notify_scan())
    assert pulls == ["30000", "30001"]  # 活跃∩被评论,时间线序,同好友去重,排除未评论/自己
    assert sleeps == [2.0, 2.0]  # 每个好友前各 2 秒


def test_notify_scan_source_b_zero_pulls_when_discovery_no_overlap(tmp_path, monkeypatch):
    """M3 T4-①:发现层无「被评论过且活跃」的好友(发现层空/无交集)→ 零源B拉取
    (get_user_feeds_raw 零调用,发现层恰 1 次);评论名单与发现层是必要交集。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)

    p = _make_plugin(tmp_path)
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", fresh)
    pulls: list = []

    class _StubNoOverlapClient(_StubUnifiedClient):
        """源A空;发现层只含未评论过的好友 40000——与评论名单无交集。"""

        def __init__(self, discoveries):
            super().__init__(discoveries)
            self.discovery_calls = 0

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            del num, target_uin
            pulls.append(1)
            return {"usrinfo": {"uin": "40000"}, "msglist": []}

    # 场景一:发现层有动态但作者未被 bot 评论过
    p.qzone_client = _StubNoOverlapClient([
        FeedDiscovery(tid="fb1", uin="40000", nickname="陌生好友",
                      abstime=str(int(datetime.now().timestamp())), appid=311),
    ])
    asyncio.run(p._qzone_notify_scan())
    assert p.qzone_client.discovery_calls == 1  # 发现层照常调用(便车入口)
    assert pulls == [] and sleeps == []  # 零源B拉取、零间隔等待

    # 场景二:发现层为空(无任何好友动态)→同样零源B拉取
    p2 = _make_plugin(tmp_path)
    p2.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", fresh)
    p2.qzone_client = _StubNoOverlapClient([])
    asyncio.run(p2._qzone_notify_scan())
    assert p2.qzone_client.discovery_calls == 1
    assert pulls == []


def test_poll_tick_warns_p1_notifications_dropped_at_window_end(tmp_path):
    """终审 I3:窗口结束时未注入的 P1 通知被清空须显式告警(通知 is_new 发现即
    登记,清空后不重检)——告警含条数,清空后 p1_queued 归零。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    # 日程窗口已结束(1 小时前收尾)→ poll_tick 走非窗口收泵分支
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "qzone": True,
    }]}
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue_priority([
        FeedItem(tid="notify_comment_f1_c1", abstime="1750000000", uin="20000",
                 nickname="小红", content="(通知) 一", source="notify"),
        FeedItem(tid="notify_comment_f2_c2", abstime="1750000100", uin="20001",
                 nickname="小蓝", content="(通知) 二", source="notify"),
    ])
    assert p.qzone_injector.stats()["p1_queued"] == 2

    asyncio.run(p._qzone_poll_feeds())
    assert p.qzone_injector.window_active is False
    assert p.qzone_injector.stats()["p1_queued"] == 0  # 队列已清空
    assert any(
        level == "warning" and "%d 条未注入通知被清空(已登记不重试)" in str(a[0]) and a[1] == 2
        for level, a in p.logs
    )


# ---- 深度审查修复波(F1/F2/F3/F5):组合层行为测试 ----


def _reply_msg(text, target_message_id):
    """带 reply 段的出站消息(planner 引用回复形态;quote 目标在 reply 段)。"""
    return {"raw_message": [
        {"type": "reply", "data": {"target_message_id": target_message_id}},
        {"type": "text", "data": text},
    ]}


def test_gateway_rejects_quote_target_mismatch(tmp_path):
    """深度审查 A-1:意图绑定校验——出站 reply 段引用的目标消息与意图的注入消息
    不一致(如超时推进后旧轮回复错靶新注入)→ 拒发,零写调用,显式告警。"""

    p = _make_gateway_plugin(tmp_path)
    p.qzone_seen.mark_queued("t1", abstime="1750000000", author_uin="10001", summary="第一条")
    p.qzone_seen.mark_queued("t2", abstime="1750000100", author_uin="10002", summary="第二条")
    # 意图绑定第一条注入消息 qzone_t1_7;出站却引用第二条 qzone_t2_9(错靶)
    p._qzone_outbound_intent = OutboundIntent(
        kind="reaction", tid="t1", target_qq="10001", message_id="qzone_t1_7",
    )
    res = asyncio.run(p.qzone_gateway(message=_reply_msg("好看!", "qzone_t2_9"), route={}, metadata={}))
    assert res["success"] is False
    assert "不匹配" in str(res.get("error", ""))
    assert p.qzone_client.comment_calls == [] and p.qzone_client.reply_calls == []  # 零写调用
    assert any(
        level == "warning" and "出站目标不匹配意图" in str(a[0])
        for level, a in p.logs
    )
    assert p._qzone_outbound_intent is not None  # 拒发不消费意图(非成功出站)


def test_gateway_allows_quote_target_matching_intent(tmp_path):
    """深度审查 A-1 反例:quote 目标与意图注入消息一致(前缀匹配)→ 正常放行,
    绑定校验不误伤合法回复。"""

    p = _make_gateway_plugin(tmp_path)
    p.qzone_seen.mark_queued("t1", abstime="1750000000", author_uin="10001", summary="第一条")
    p._qzone_outbound_intent = OutboundIntent(
        kind="reaction", tid="t1", target_qq="10001", message_id="qzone_t1_7",
    )
    res = asyncio.run(p.qzone_gateway(message=_reply_msg("好看!", "qzone_t1_7"), route={}, metadata={}))
    assert res["success"] is True
    assert p.qzone_client.comment_calls == [("t1", "10001", "好看!")]
    assert p._qzone_outbound_intent is None


def test_gateway_binding_check_skipped_without_message_id_or_quote(tmp_path):
    """深度审查 A-1 覆盖面:意图无 message_id(旧意图形态)或出站无 reply 段时
    跳过绑定校验(大部分 planner 出站不带 reply 段,校验只覆盖带引用的高危场景)。"""

    p = _make_gateway_plugin(tmp_path)
    p.qzone_seen.mark_queued("t1", abstime="1750000000", author_uin="10001", summary="第一条")
    # 意图无 message_id + 出站带 reply 段 → 跳过校验,照常放行
    p._qzone_outbound_intent = OutboundIntent(kind="reaction", tid="t1", target_qq="10001")
    res = asyncio.run(p.qzone_gateway(message=_reply_msg("好看!", "qzone_tX_9"), route={}, metadata={}))
    assert res["success"] is True
    # 意图带 message_id + 出站无 reply 段(纯文本) → 跳过校验,照常放行
    p.qzone_seen.mark_queued("t2", abstime="1750000100", author_uin="10002", summary="第二条")
    p._qzone_outbound_intent = OutboundIntent(
        kind="reaction", tid="t2", target_qq="10002", message_id="qzone_t2_3",
    )
    res2 = asyncio.run(p.qzone_gateway(message=_out_msg("同感~"), route={}, metadata={}))
    assert res2["success"] is True


def test_pump_force_release_clears_intent(tmp_path):
    """深度审查 A-1/B-1:awaiting 超时强制推进时联动清出站意图——原实现只释放
    awaiting 不清意图,超时后旧轮回复会按残留意图发向旧目标(公开错靶根因)。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue([FeedItem(
        tid="f9", abstime="1750000000", uin="10001", nickname="小明", content="老动态",
    )])
    popped = p.qzone_injector.next_to_inject(_time.monotonic())
    assert popped is not None
    # 注入时刻回拨到远超 decision_window(75s)→ awaiting_timed_out 命中
    p.qzone_injector.mark_injected("f9", _time.monotonic() - 1000)
    p._qzone_outbound_intent = OutboundIntent(
        kind="reaction", tid="f9", target_qq="10001", message_id="qzone_f9_1",
    )

    asyncio.run(p._qzone_pump())
    assert p.qzone_injector.awaiting_tid == ""  # 已强制释放
    assert p._qzone_outbound_intent is None  # 意图联动清空(队列空,无新注入接管)
    assert any(
        level == "warning" and "强制推进;清意图防错靶" in str(a[0])
        for level, a in p.logs
    )


def test_qzone_data_prune_behavior(tmp_path):
    """深度审查 D-1:qzone 数据保留期清理——评论去重 30 天 + qzone_feeds 的
    seen 行 7 天(queued 行不动:回退未读语义由窗口收泵负责,prune 不得越权)。"""

    from datetime import timedelta

    p = _make_plugin(tmp_path)
    now = datetime.now()
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731 - 测试局部便捷
    # 评论表:40 天前的去重行(超 30 天保留期)+ 昨天的行(保留)
    p.qzone_comment_seen.is_new("f1:c1:20000")
    p.store.execute("UPDATE qzone_comments SET created_at = ?", (iso(now - timedelta(days=40)),))
    p.qzone_comment_seen.is_new("f2:c2:20001")
    # seen 表:8 天前注入的 seen 行(超 7 天保留)+ 今天注入的 seen 行 + 旧的 queued 行
    p.qzone_seen.mark_queued("s_old", abstime="1", author_uin="1", summary="旧已见")
    p.qzone_seen.mark_queued("s_new", abstime="1", author_uin="1", summary="新已见")
    p.qzone_seen.mark_queued("q_old", abstime="1", author_uin="1", summary="旧未读")
    p.qzone_seen.mark_seen("s_old", iso(now - timedelta(days=8)))
    p.qzone_seen.mark_seen("s_new", iso(now))

    asyncio.run(p._qzone_data_prune())
    rows = p.store.query("SELECT comment_key FROM qzone_comments ORDER BY comment_key")
    assert [r[0] for r in rows] == ["f2:c2:20001"]  # 旧评论行已清,新行保留
    states = dict(p.store.query("SELECT tid, state FROM qzone_feeds"))
    assert states == {"s_new": "seen", "q_old": "queued"}  # 旧 seen 清、新 seen 留、queued 不动


def test_qzone_data_prune_registered_in_on_load():
    """深度审查 D-1:清理任务接线断言——调度器注册 qzone_data_prune(24h 周期)。"""
    import inspect

    import plugin as plugin_mod

    src = inspect.getsource(plugin_mod)
    assert 'register("qzone_data_prune", 24 * 3600, self._qzone_data_prune)' in src


# ---- 深度审查 A-2:长 IO 移出调度器 tick ----


def test_long_io_moved_out_of_scheduler_ticks():
    """深度审查 A-2 源码断言:两个轮询 tick 只做触发+防重入(_qzone_poll_running/
    _qzone_notify_running 标记),长 IO 抽到 _qzone_poll_feeds/_qzone_notify_scan
    后台任务执行——调度器串行 await factory(),长轮询会把 sleep_tick 等任务拖住。"""
    import inspect

    import plugin as plugin_mod

    src = inspect.getsource(plugin_mod)
    assert "_qzone_poll_running" in src and "_qzone_notify_running" in src
    assert "_spawn_background_task(self._qzone_poll_feeds())" in src
    assert "_spawn_background_task(self._qzone_notify_scan())" in src


def _active_qzone_schedule() -> dict:
    """覆盖当前时刻的 daily+qzone 日程(窗口开启分支用)。"""
    now = datetime.now()
    return {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "qzone": True,
    }]}


def test_poll_tick_returns_before_long_io_completes(tmp_path):
    """深度审查 A-2 行为:tick 派发后台拉取后立即返回——统一时间线还在拉(长 IO
    未完成)时 tick 协程已 done,调度器不被阻塞。"""

    started, release = asyncio.Event(), asyncio.Event()

    class _SlowDiscoveryClient:
        async def get_unified_timeline(self, *, count=20):
            del count
            started.set()
            await release.wait()  # 模拟发现层 HTTP 的长 IO
            return []

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    p.qzone_client = _SlowDiscoveryClient()

    async def scenario():
        tick_task = asyncio.create_task(p._qzone_poll_tick())
        for _ in range(20):
            await asyncio.sleep(0)  # 推进事件循环,让 tick 与后台任务各跑起来
        assert tick_task.done()  # tick 已返回(不等长 IO)
        assert started.is_set()  # 后台拉取已启动
        release.set()  # 放行后台任务收尾

    asyncio.run(scenario())
    assert p.qzone_injector.window_active is True  # 后台 feeds 完整跑了窗口开启逻辑


def test_poll_tick_reentrancy_guard(tmp_path):
    """深度审查 A-2 行为:上一轮后台拉取还在跑(_qzone_poll_running=True)时,
    tick 防重入直接返回——不派发第二轮,不重复拉统一时间线;标记清位只由后台
    feeds 的 finally 负责(tick 早退不动它)。"""

    calls: list = []

    class _CountingDiscoveryClient:
        async def get_unified_timeline(self, *, count=20):
            del count
            calls.append(1)
            return []

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    p.qzone_client = _CountingDiscoveryClient()

    async def scenario():
        p._qzone_poll_running = True  # 模拟上一轮仍在跑
        await p._qzone_poll_tick()  # 防重入:立即返回,不派发
        assert p._qzone_poll_running is True  # 早退不清标记(清位在 feeds 的 finally)
        p._qzone_poll_running = False  # 上一轮结束:下轮 tick 恢复派发
        await p._qzone_poll_tick()
        for _ in range(100):  # 等后台 feeds 跑完(无 IO 桩即刻返回)
            if not p._qzone_poll_running:
                break
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert calls == [1]  # 防重入零次 + 恢复后恰一次
    assert p._qzone_poll_running is False  # 后台 feeds 完成后标记复位


# ---- 深度审查 B-4:通知注入被拒不永久丢失 ----


class _RejectGateway:
    """route_message 恒拒的网关桩(模拟宿主 adapter policy/网关状态拒绝)。"""

    def __init__(self):
        self.calls = []

    async def route_message(self, name, msg):
        self.calls.append((name, msg))
        return False


class _ExplodingGateway:
    """route_message 抛异常的网关桩(模拟 RPC 帧异常等调用失败)。"""

    def __init__(self):
        self.calls = []

    async def route_message(self, name, msg):
        self.calls.append((name, msg))
        raise RuntimeError("E_UNKNOWN: rpc frame overflow")


def _notify_feed(dedup_key: str) -> FeedItem:
    """带去重键的 P1 通知项(源A 形态)。"""
    return FeedItem(
        tid="notify_comment_feedX_cX", abstime="1750000000", uin="20000",
        nickname="小红", content="(通知) 小红 评论了你的说说", source="notify",
        dedup_key=dedup_key,
    )


def test_pump_reverts_notify_dedup_key_when_rejected(tmp_path):
    """深度审查 B-4:通知项注入被宿主拒绝 → 回退 is_new 登记的去重键(下轮通知
    轮询重新发现,不永久丢失);浏览动态(source=feed)不回退(queued 行由窗口
    尾 revert_pending 回退未读,语义已有归属)。"""

    key = "feedX:cX:20000"
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    assert p.qzone_comment_seen.is_new(key) is True  # 轮询发现即登记(store 契约)
    p.qzone_injector.enqueue_priority([_notify_feed(key)])
    p._ctx.gateway = _RejectGateway()

    asyncio.run(p._qzone_pump())
    assert len(p._ctx.gateway.calls) == 1  # 注入尝试确实发生(不是早退)
    assert p.qzone_comment_seen.is_new(key) is True  # 键已回退:下轮重新发现
    assert p.qzone_injector.awaiting_tid == ""  # 未 mark_injected(拒绝不置 awaiting)
    assert any(
        level == "warning" and "注入被宿主拒绝" in str(a[0]) for level, a in p.logs
    )


def test_pump_reverts_notify_dedup_key_on_route_exception(tmp_path):
    """深度审查 B-4 异常分支:route_message 抛异常与被拒同款回退(通知不丢)。"""

    key = "feedX:cX:20000"
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    assert p.qzone_comment_seen.is_new(key) is True
    p.qzone_injector.enqueue_priority([_notify_feed(key)])
    p._ctx.gateway = _ExplodingGateway()

    asyncio.run(p._qzone_pump())
    assert len(p._ctx.gateway.calls) == 1
    assert p.qzone_comment_seen.is_new(key) is True  # 键已回退


def test_pump_keeps_feed_semantics_when_rejected(tmp_path):
    """深度审查 B-4 反例:浏览动态被拒不回退评论键(其无 dedup_key),seen 表
    queued 行保留——窗口尾 revert_pending 回退未读的既有语义不变。"""

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_seen.mark_queued("t_regular", abstime="1750000000", author_uin="10001", summary="普通动态")
    p.qzone_injector.enqueue([FeedItem(
        tid="t_regular", abstime="1750000000", uin="10001", nickname="小明", content="今天天气好",
    )])
    p._ctx.gateway = _RejectGateway()

    asyncio.run(p._qzone_pump())
    assert len(p._ctx.gateway.calls) == 1
    rows = p.qzone_seen.store.query("SELECT state FROM qzone_feeds WHERE tid = 't_regular'")
    assert rows and rows[0][0] == "queued"  # 未标记已见,窗口尾回退未读
    assert p.qzone_injector.awaiting_tid == ""


# ---- 深度审查 A-N1:通知重试上限 + fav_event 去重 ----


def test_pump_notify_retry_limit_gives_up(tmp_path):
    """深度审查 A-N1:宿主持续拒绝时重试有上限——同一通知经「发现→注入被拒→回退」
    循环 3 次后保留登记放弃(is_new False),第 4 轮扫描不再注入;同一事件在
    3 次发现中重复调用 fav_event 只记一条(发现侧去重)。"""

    import time as _time

    comments = {"feed1": [CommentItem(
        comment_tid="c1", uin="20000", nickname="小红",
        content="好友评论", create_time=str(int(_time.time())),
    )]}
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_client = _StubCommentClient(comments, {"feed1": "今天的心情"})
    gw = _RejectGateway()
    p._ctx.gateway = gw

    for _ in range(3):
        asyncio.run(p._qzone_notify_scan())
    assert len(gw.calls) == 3  # 恰三次注入尝试(每轮扫描重新发现被回退的键)
    assert p.qzone_comment_seen.is_new("feed1:c1:20000") is False  # 已放弃:登记保留判重
    assert any(
        level == "warning" and "放弃不再重试" in str(a[0]) for level, a in p.logs
    )
    assert sum(
        1 for level, a in p.logs if level == "info" and "待下轮重试" in str(a[0])
    ) == 2  # 前两次回退(第 1/2 次),第三次放弃
    asyncio.run(p._qzone_notify_scan())  # 第 4 轮:键已判重,不再注入
    assert len(gw.calls) == 3
    # 同一事件 3 次发现只记一条 fav_event(A-N1 去重,防重复放大结算素材)
    rows = p.store.query("SELECT COUNT(*) FROM qzone_fav_events WHERE user_id = '20000'")
    assert rows[0][0] == 1


# ---- 深度审查 C-1:睡眠门(pump/turn_signal) ----


class _SleepStub:
    """SleepManager.is_sleeping 最小桩。"""

    def __init__(self, sleeping: bool):
        self._sleeping = sleeping

    def is_sleeping(self) -> bool:
        return self._sleeping


def test_pump_silent_when_sleeping(tmp_path):
    """深度审查 C-1:入睡后在途泵静默退出——睡眠拦截链会把注入的消息拦进回顾
    缓冲(白注入),泵不得在睡眠期弹出/注入队列项。"""

    p = _make_plugin(tmp_path)
    p.config.sleep.enabled = True
    p.sleep = _SleepStub(True)
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue([FeedItem(
        tid="t_slp", abstime="1750000000", uin="10001", nickname="小明", content="深夜动态",
    )])
    assert p.qzone_injector.queue_size() == 1

    asyncio.run(p._qzone_pump())
    assert p._ctx.gateway.calls == []  # 静默退出:零注入
    assert p.qzone_injector.queue_size() == 1  # 队列未弹(睡眠后醒来可继续)


def test_turn_signal_does_not_advance_pump_when_sleeping(tmp_path):
    """深度审查 C-1:睡眠期轮完成信号不推进泵——消息已被拦,释放 awaiting 并
    注入下一条只会把新消息再送进拦截链;醒来后恢复推进。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.config.sleep.enabled = True
    p.sleep = _SleepStub(True)
    p._qzone_session_ids.add("s1")
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue([FeedItem(
        tid="t_first", abstime="1750000000", uin="10001", nickname="小明", content="第一条",
    )])
    popped = p.qzone_injector.next_to_inject(_time.monotonic())
    assert popped is not None
    p.qzone_injector.mark_injected(popped.tid, _time.monotonic())
    p.qzone_injector.enqueue([FeedItem(
        tid="t_next", abstime="1750000100", uin="10002", nickname="小红", content="下一条",
    )])
    assert p.qzone_injector.awaiting_tid == "t_first"

    # 睡眠中的轮完成信号:awaiting 不释放、泵不推进(下一条不被注入进拦截链)
    asyncio.run(p.qzone_turn_signal(session_id="s1", output_items=[]))
    assert p.qzone_injector.awaiting_tid == "t_first"  # 未释放
    assert p._ctx.gateway.calls == []  # 泵未注入下一条
    assert p.qzone_injector.queue_size() == 1  # 下一条仍在队列

    # 醒来后同款信号恢复推进(awaiting 释放);turn_signal 派发的后台泵与显式泵
    # 二者其一注入下一条(串行锁保证恰一条)
    p.sleep = _SleepStub(False)
    asyncio.run(p.qzone_turn_signal(session_id="s1", output_items=[]))
    assert p.qzone_injector.awaiting_tid != "t_first"  # 旧 awaiting 已释放
    asyncio.run(p._qzone_pump())
    assert len(p._ctx.gateway.calls) == 1  # 下一条正常注入(恰一次)
    assert p._ctx.gateway.calls[0][1]["message_id"].startswith("qzone_t_next_")


# ---- M3 T3:浏览流统一时间线架构(发现层→过滤→充实层) ----


def test_poll_feeds_unified_timeline_enriches_new_only_and_injects(tmp_path, monkeypatch):
    """M3 T3-①:统一时间线发现→仅新 tid 充实→入队注入——非说说(appid≠311)与
    seen 已登记的旧 tid 被过滤;充实层只对含新动态的作者调用且 num=组大小+2;
    充实页返回的同好友旧动态/未发现动态不注入;新动态恰好经泵注入一次。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    # 预登记旧动态为已见(发现层 is_new_candidate 判非新;queued 会被窗口开始的
    # revert_pending 回收,seen 才是稳定的判重基准)
    p.qzone_seen.mark_queued("oldtid", abstime="1750000000", author_uin="10001", summary="旧动态")
    p.qzone_seen.mark_seen("oldtid", "2026-08-31T10:00:00")

    discoveries = [
        FeedDiscovery(tid="newtid", uin="10001", nickname="小明", abstime="1750000200", appid=311),
        FeedDiscovery(tid="oldtid", uin="10001", nickname="小明", abstime="1750000000", appid=311),
        # 非说说(如分享/音乐):发现层保留全量条目,浏览流只认 appid=311
        FeedDiscovery(tid="shareid", uin="10002", nickname="小红", abstime="1750000100", appid=2023106),
    ]
    enrich_calls: list = []

    class _StubEnrichClient(_StubUnifiedClient):
        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            enrich_calls.append((target_uin, nickname, num))
            # 充实页含同好友的旧动态与发现层之外的说说:均不得注入
            return [
                FeedItem(tid="othertid", abstime="1749000000", uin="10001", nickname="小明", content="更旧的说说"),
                FeedItem(tid="newtid", abstime="1750000200", uin="10001", nickname="小明", content="新动态正文"),
            ]

    p.qzone_client = _StubEnrichClient(discoveries)
    asyncio.run(p._qzone_poll_feeds())

    # ①发现层 1 次调用;②充实层只对 uin=10001(唯一含新 tid 的作者)调用
    assert p.qzone_client.discovery_calls == 1
    assert enrich_calls == [("10001", "小明", 3)]  # num=组大小(1)+2,昵称取发现层
    # ③新动态入队并经泵注入恰一次(一动态一轮);旧/未发现/非说说不注入
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_id"].startswith("qzone_newtid_")
    states = dict(p.store.query("SELECT tid, state FROM qzone_feeds"))
    # newtid 已注入成功(泵 mark_seen);othertid/shareid 未登记
    assert states == {"oldtid": "seen", "newtid": "seen"}
    # ④入队计数日志(统一时间线口径:入队 1 条/发现 1 条)
    assert any(
        level == "info" and "新动态入队" in str(a[0]) and "统一时间线发现" in str(a[0])
        and list(a[1:]) == [1, 1]
        for level, a in p.logs
    )


def test_poll_feeds_falls_back_to_legacy_on_discovery_failure(tmp_path, monkeypatch):
    """M3 T3-②:发现层失败(非登录态异常)→显式告警+回退旧逐好友路径——好友列表
    →get_user_feeds→mark_queued→注入链照常工作,错误不静默。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    legacy_pulls: list = []

    class _ExplodingDiscoveryClient:
        async def get_unified_timeline(self, *, count=20):
            del count
            raise RuntimeError("空间统一时间线请求失败: HTTP 502")

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            legacy_pulls.append((target_uin, num))
            return [FeedItem(tid="lt1", abstime="1750000000", uin=target_uin,
                             nickname=nickname, content="旧路径动态")]

    p.qzone_client = _ExplodingDiscoveryClient()

    async def _friends():
        return [{"user_id": "10001", "nickname": "小明"}]

    p._qzone_friend_list = _friends
    asyncio.run(p._qzone_poll_feeds())

    assert any(
        level == "exception" and "统一时间线拉取失败" in str(a[0]) and "回退逐好友旧路径" in str(a[0])
        for level, a in p.logs
    )
    assert legacy_pulls == [("10001", 3)]  # legacy 路径逐好友拉取(num=3 原口径)
    assert len(p._ctx.gateway.calls) == 1  # 旧路径动态仍注入
    assert p._ctx.gateway.calls[0][1]["message_id"].startswith("qzone_lt1_")
    assert p.qzone_seen.is_new_candidate("lt1") is False  # 已登记
