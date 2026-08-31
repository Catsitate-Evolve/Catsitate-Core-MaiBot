"""QQ空间组合层行为测试(终审修复波 I1/I4):plugin 接线的行为断言,非源码字符串。

_StubCtx 模式参照 test_integration.py:离线装配插件实例,依赖全部注入桩,
只验证「组合层」行为——窗口开启作废残留评论意图 / 网关出站意图消费 /
评论轮询三重守卫(自评跳过+判重+意图占用)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from catsitate_core.config import CatsitateConfig
from catsitate_core.qzone.comment_seen import CommentSeenStore
from catsitate_core.qzone.injector import FeedInjector
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
    p.qzone_seen = SeenStore(SQLiteStore(tmp_path / "seen.db"))
    p.qzone_seen.ensure_schema()
    p.qzone_comment_seen = CommentSeenStore(SQLiteStore(tmp_path / "comments.db"))
    p.qzone_comment_seen.ensure_schema()
    p.qzone_injector = FeedInjector(decision_window_s=75)
    p.logs = logs  # 测试侧便捷引用(非插件属性约定)
    return p


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


def test_comment_poll_self_skip_dedup_and_intent_occupied(tmp_path):
    """I4-3:评论轮询三重守卫一组断言——①bot 自评跳过不注入;②is_new 判重
    (二次轮询不重注入);③意图占用时不注入(且不再取数)。"""

    import time as _time

    comments = {"feed1": [
        CommentItem(comment_tid="c0", uin=BOT_UIN, nickname="我", content="自评", create_time=""),
        CommentItem(comment_tid="c1", uin="20000", nickname="小红", content="好友评论",
                    create_time=str(int(_time.time()))),
    ]}
    p = _make_plugin(tmp_path)
    p._schedule_data = {"date": "2000-01-01", "windows": []}  # 窗口外(评论轮询工作区)
    p.qzone_client = _StubCommentClient(comments, {"feed1": "今天的心情"})

    # ①首轮:自评跳过(登记不注入),好友评论注入恰好 1 条
    asyncio.run(p._qzone_comment_poll_tick())
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_info"]["user_info"]["user_id"] == "20000"
    assert p._qzone_outbound_intent is not None and p._qzone_outbound_intent.kind == "comment_reply"

    # ②意图占用:不注入且不再取数(上一条还在等回复,不叠加)
    asyncio.run(p._qzone_comment_poll_tick())
    assert len(p._ctx.gateway.calls) == 1 and p.qzone_client.fetches == 1

    # ③意图释放后重扫:自评仍跳过,好友评论 is_new 判重 → 不重注入(只多了一次取数)
    p._qzone_outbound_intent = None
    asyncio.run(p._qzone_comment_poll_tick())
    assert len(p._ctx.gateway.calls) == 1
    assert p.qzone_client.fetches == 2  # 取数发生(判重生效,不是早退)
