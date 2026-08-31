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
    p.qzone_seen = SeenStore(SQLiteStore(tmp_path / "seen.db"))
    p.qzone_seen.ensure_schema()
    p.qzone_comment_seen = CommentSeenStore(SQLiteStore(tmp_path / "comments.db"))
    p.qzone_comment_seen.ensure_schema()
    p.qzone_injector = FeedInjector(decision_window_s=75)
    p._qzone_session_ids = set()  # 实例级覆盖(类属性为共享 set,防测试间状态泄漏)
    p.logs = logs  # 测试侧便捷引用(非插件属性约定)
    return p


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

    async def _no_friends():
        return []  # 好友列表空:窗口开始分支后本轮提前返回,足以断言意图处置

    p._qzone_friend_list = _no_friends
    asyncio.run(p._qzone_poll_tick())
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
    """I4-3(承 T11 重写):统一通知轮询三重守卫——①bot 自评跳过不注入;
    ②is_new 判重(二次轮询不重注入);③意图占用时不取数(上一条还在等回复)。"""

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
    asyncio.run(p._qzone_notify_poll_tick())
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_info"]["user_info"]["user_id"] == "20000"
    intent = p._qzone_outbound_intent
    assert intent is not None and intent.kind == "comment_reply"
    assert (intent.tid, intent.target_qq, intent.comment_tid) == ("feed1", BOT_UIN, "c1")

    # ②意图占用:不取数不注入(上一条通知还在等回复,不叠加)
    asyncio.run(p._qzone_notify_poll_tick())
    assert len(p._ctx.gateway.calls) == 1 and p.qzone_client.fetches == 1

    # ③意图释放后重扫:自评仍跳过,好友评论 is_new 判重 → 不重注入(只多了一次取数)
    p._qzone_outbound_intent = None
    asyncio.run(p._qzone_notify_poll_tick())
    assert len(p._ctx.gateway.calls) == 1
    assert p.qzone_client.fetches == 2  # 取数发生(判重生效,不是早退)
    assert p.qzone_comment_seen.is_new("feed1:c1:20000") is False  # 已登记,判重依据


def test_notify_poll_source_b_reply_routes_to_friend_thread(tmp_path, monkeypatch):
    """T11 源B:bot 在好友说说下的评论收到楼中楼回复(list_3)→ 通知注入 →
    意图 comment_reply 指向好友说说(target_qq=好友,commentId=回复 tid)。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)  # 源A无结果→首个源B请求前有 2 秒间隔(I2),桩掉不真等

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 源B目标圈定:bot 曾在好友 30000 的说说下发过评论(note_bot_comment 留痕)
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "我的评论", "2026-08-31T10:00:00")
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

    class _StubNotifyClient:
        """源B输入桩:源A无评论,好友 30000 的原始载荷带一条楼中楼回复。"""

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            assert target_uin == "30000"  # 只拉 bot 评论过的好友
            return raw

    p.qzone_client = _StubNotifyClient()
    asyncio.run(p._qzone_notify_poll_tick())
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
    """终审 I2:源B逐好友拉取带 2 秒防风控间隔——源A无结果时首个请求前也间隔
    (与源A的 HTTP 拉取拉开);好友之间固定间隔。拉取顺序按反查好友圈定。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)

    p = _make_plugin(tmp_path)
    # bot 曾在两位好友说说下评论:源B反查圈定 30000/30001
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", "2026-08-31T10:00:00")
    p.qzone_comment_seen.note_bot_comment("ffeed2", "30001", "评论二", "2026-08-31T10:05:00")

    pulls: list = []

    class _StubSpacingClient:
        """源A空结果;源B记录拉取顺序,载荷无楼中楼回复(只断言间隔行为)。"""

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            del num
            pulls.append(target_uin)
            return {"usrinfo": {"uin": target_uin}, "msglist": []}

    p.qzone_client = _StubSpacingClient()
    asyncio.run(p._qzone_notify_poll_tick())
    assert sorted(pulls) == ["30000", "30001"]  # 逐好友各拉一次
    assert sleeps == [2.0, 2.0]  # 源A无结果:首个请求前 + 好友间,各 2 秒


def test_notify_poll_source_b_first_pull_no_wait_when_source_a_has_result(tmp_path, monkeypatch):
    """终审 I2 补充:源A已有通知结果时,首个源B请求前不额外等待(仅好友间间隔)。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)

    p = _make_plugin(tmp_path)
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", "2026-08-31T10:00:00")
    comments = {"myfeed": [
        CommentItem(comment_tid="c1", uin="20000", nickname="小红", content="好友评论",
                    create_time=str(int(_time.time()))),
    ]}

    class _StubSourceAClient:
        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return comments, {"myfeed": "我的说说"}

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            del num, target_uin
            return {"usrinfo": {"uin": target_uin}, "msglist": []}

    p.qzone_client = _StubSourceAClient()
    asyncio.run(p._qzone_notify_poll_tick())
    assert sleeps == []  # 单好友且源A有结果:无任何额外等待


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

    asyncio.run(p._qzone_poll_tick())
    assert p.qzone_injector.window_active is False
    assert p.qzone_injector.stats()["p1_queued"] == 0  # 队列已清空
    assert any(
        level == "warning" and "%d 条未注入通知被清空(已登记不重试)" in str(a[0]) and a[1] == 2
        for level, a in p.logs
    )
