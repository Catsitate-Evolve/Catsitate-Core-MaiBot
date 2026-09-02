"""QQ空间组合层行为测试(工具驱动架构 v0.7):plugin 接线的行为断言,非源码字符串。

_StubCtx 模式参照 test_integration.py:离线装配插件实例,依赖全部注入桩,
只验证「组合层」行为——工具目标解析(registry/seen_store/awaiting)/评论频控/
@ 前缀/AuthError 自愈/统一通知轮询守卫(T11)与源B楼中楼上下文登记。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timedelta

import pytest

from catsitate_core.config import CatsitateConfig
from catsitate_core.memo import MemoService
from catsitate_core.qzone import QZONE_GATEWAY_NAME, QZONE_PLATFORM
from catsitate_core.qzone.client import QzoneAuthError
from catsitate_core.qzone.comment_seen import CommentSeenStore
from catsitate_core.qzone.discovery import FeedDiscovery
from catsitate_core.qzone.injector import FeedInjector
from catsitate_core.qzone.like_seen import LikeSeenStore
from catsitate_core.qzone.protocol import FeedItem
from catsitate_core.qzone.registry import FeedContext, FeedContextRegistry
from catsitate_core.qzone.seen_store import SeenStore
from catsitate_core.qzone.wire import CommentItem, LikeEvent
from catsitate_core.storage import JsonSnapshot, SQLiteStore

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

    async def get_unified_timeline(self, *, count=20, begin=0):
        del count
        return []  # 发现层空:源B零拉取,聚焦源A行为

    async def get_like_events(self, *, count=30):
        del count
        return []  # 源C 空:聚焦源A行为(通知扫描三源都要经本接口取数)


class _StubWriteClient:
    """出站动作桩:记录 do_comment/do_reply/do_like/do_publish 调用,恒成功。

    publish_tid 为 do_publish 回传的新说说 tid(空串=响应缺 tid 形态,
    供回注缺锚路径测试)。
    """

    def __init__(self):
        self.comment_calls = []
        self.reply_calls = []
        self.like_calls = []
        self.publish_calls = []
        self.publish_tid = "newtid0001"

    async def do_comment(self, *, fid, target_qq, content):
        self.comment_calls.append((fid, target_qq, content))
        return True

    async def do_reply(self, *, fid, target_qq, comment_tid, comment_uin, comment_nick,
                       content, at_uin="", at_nick=""):
        self.reply_calls.append((fid, target_qq, comment_tid, comment_uin, comment_nick, content, at_uin, at_nick))
        return True

    async def do_like(self, *, fid, target_qq):
        self.like_calls.append((fid, target_qq))
        return True

    async def do_publish(self, *, content):
        self.publish_calls.append(content)
        return self.publish_tid


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
    p._qzone_registry = FeedContextRegistry()  # 实例级(类属性共享,防测试间泄漏)
    p._qzone_comment_counts = {}
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
    # 源C 赞事件去重(on_load 装配,离线测试手工补;Task 10)
    p.qzone_like_seen = LikeSeenStore(store)
    p.qzone_injector = FeedInjector(decision_window_s=75)
    p._qzone_session_ids = set()  # 实例级覆盖(类属性为共享 set,防测试间状态泄漏)
    p.qzone_client = _StubUnifiedClient([])  # 默认发现层桩:空时间线(各测试按需覆盖)
    p.qzone_cookie = _StubCookie()
    # 日记路径依赖(on_load 装配,离线测试手工补):memo 取数 + 待回注快照
    p.memo = MemoService(store, p.config.memo)
    p.memo.ensure_schema()
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")
    # 空间见闻快照(on_load 装配,离线测试手工补;_qzone_block 真实流分支读取)
    p._qzone_digest_snapshot = JsonSnapshot(tmp_path / "qzone_digest.json")
    p.logs = logs  # 测试侧便捷引用(非插件属性约定)
    return p


class _StubUnifiedClient:
    """发现层输入桩:get_unified_timeline 首页返回固定列表(默认空),记录调用参数。

    Task5 翻页语义:固定列表只在 begin=0 首页生效,第 2 页起返回空(模拟
    「更早页无积压」的稳态,翻页止步)——否则同一列表被每页重复消费。
    """

    def __init__(self, discoveries):
        self._discoveries = discoveries
        self.discovery_calls = 0

    async def get_unified_timeline(self, *, count=20, begin=0):
        del count
        self.discovery_calls += 1
        return list(self._discoveries) if begin == 0 else []

    async def get_like_events(self, *, count=30):
        del count
        return []  # 源C 空:各测试按需子类覆盖(通知扫描三源都要经本接口取数)


def _patch_sleep(monkeypatch, record: list) -> None:
    """把 asyncio.sleep 换成记录桩(源B请求间隔断言用;不让测试真等 2 秒)。"""

    async def _fake_sleep(delay, *a, **k):
        record.append(float(delay))

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


def test_poll_tick_resets_comment_counts_on_window_start(tmp_path):
    """工具驱动 v0.7:窗口开始时重置同说说评论频控计数——上限语义是「本轮逛空间
    期间」对同说说最多 3 条,跨窗口不得累计误伤;窗口仍正常开启(不饿死注入)。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "read_qzone": True,
    }]}
    p._qzone_comment_counts = {"oldfeed1": 3, "oldfeed2": 1}  # 上一窗口的计数残留

    # 发现层空时间线(默认桩):窗口开始分支后本轮提前返回,足以断言计数处置
    asyncio.run(p._qzone_poll_feeds())
    assert p._qzone_comment_counts == {}  # 计数已重置
    assert p.qzone_injector.window_active is True  # 窗口正常开启(不跳过注入)


# ---- 工具驱动 v0.7:qzone_comment/qzone_reply/qzone_like 工具行为测试 ----


def _make_tool_plugin(tmp_path):
    """工具直调装配:写路径客户端桩 + 真实 seen/comment_seen 存储 + 表达生成
    LLM 桩(润色回显:把【待发内容】草稿原样返回,等价于润色后等于草稿)。"""

    p = _make_plugin(tmp_path)
    p.qzone_client = _StubWriteClient()
    p._qzone_session_ids.add("s1")  # 虚拟流会话(硬门控放行)
    expr_llm_calls: list = []

    async def _fake_side_llm(messages, model, module, timeout_ms=None):
        expr_llm_calls.append({"messages": messages, "model": model, "module": module})
        for m in messages:
            c = str(m.get("content") or "")
            if c.startswith("【待发内容】"):
                return {"success": True, "response": c.split("\n", 1)[-1]}
        return {"success": True, "response": "ok"}

    p._side_llm_call = _fake_side_llm
    p.expr_llm_calls = expr_llm_calls
    # 人设缓存预置(离线桩无 ctx.config,不走异常兜底路径;实例级覆盖防测试间泄漏)
    p._persona_cache = "猫耳少女"
    p._style_cache = ""
    return p


def _register_feed(p, tid="fulltid0001", owner="10001", nickname="小明", **kw):
    """登记一条浏览 FeedContext(等价泵注入成功后的登记动作)。"""
    p._qzone_registry.register(FeedContext(tid=tid, owner_uin=owner, owner_nickname=nickname, **kw))


def test_qzone_comment_success_via_registry(tmp_path):
    """qzone_comment 成功路径(两段式):registry 解析目标(锚前缀→全量 tid 回填),
    正文经表达生成层产出(旁路 qzone_expression 模块/人设前置/场景素材入 prompt);
    do_comment 参数对位(fid=全量 tid,target_qq=说说主人,正文=生成结果);
    记账三件套落库(interacted/自评登记/好感度事件),计数累计。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="fulltid0001abc", owner="10001",
                   content_summary="今天去了海边", recent_comments=["小红:羡慕"])
    # seen 预登记(mark_interacted 的落库对象;registry-only 通知说说无行则 UPDATE 空转,无害)
    p.qzone_seen.mark_queued("fulltid0001abc", abstime="1", author_uin="10001", summary="动态")
    res = asyncio.run(p.qzone_comment(feed_id="fulltid0001", content="好看!", stream_id="s1"))
    assert res.startswith("评论成功,已发出:")
    # fid 回填为全量 tid(锚前缀不可直接发 API);target_qq=说说主人;正文=润色结果
    assert p.qzone_client.comment_calls == [("fulltid0001abc", "10001", "好看!")]
    # 表达润色调用:模块/模型走 qzone 节表达配置;人设+表达方式为稳定上下文;
    # 草稿进【待发内容】素材段(润色只管怎么说,场景由 planner 草稿承载)
    assert p.expr_llm_calls[0]["module"] == "qzone_expression"
    assert p.expr_llm_calls[0]["model"] == p.config.qzone.expression_llm_model
    contents = [m["content"] for m in p.expr_llm_calls[0]["messages"]]
    assert "猫耳少女" in contents[1]  # 人设=稳定上下文首段
    assert any(c.startswith("【待发内容】\n好看!") for c in contents)
    rows = p.qzone_seen.store.query("SELECT interacted FROM qzone_feeds WHERE tid = 'fulltid0001abc'")
    assert rows and rows[0][0] == 1  # mark_interacted 已落库
    keys = p.qzone_comment_seen.store.query(
        "SELECT comment_key FROM qzone_comments WHERE comment_key LIKE 'fulltid0001abc:bot:%'"
    )
    assert keys  # 自评登记(note_bot_comment)已入评论表(正文=生成结果)
    assert p._qzone_comment_counts == {"fulltid0001abc": 1}  # 频控计数累计


def test_qzone_comment_polish_failure_falls_back_to_draft(tmp_path):
    """润色失败路径:旁路 LLM 失败(不成功)→ 告警后以草稿直发(显式回退,
    不阻断动作);发布/记账照常,频控计数正常累计。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="genfail1", owner="10001")

    async def _fail(messages, model, module, timeout_ms=None):
        return {"success": False}

    p._side_llm_call = _fail
    res = asyncio.run(p.qzone_comment(feed_id="genfail1", content="夸一下", stream_id="s1"))
    assert res.startswith("评论成功,已发出:")
    assert p.qzone_client.comment_calls == [("genfail1", "10001", "夸一下")]  # 草稿直发
    assert p._qzone_comment_counts == {"genfail1": 1}
    assert any(level == "warning" and "表达润色失败" in str(a[0]) for level, a in p.logs)


def test_qzone_comment_at_prefix_uses_commenter_nickname(tmp_path):
    """@ 前缀:通知场景回应评论——at_user_id=registry 登记的评论者时用其昵称,
    @ 格式与 napcat 适配器一致(uin/nick/auto);好感度事件记 COMMENT 指向被@者。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="notifyfeed01", owner=BOT_UIN, commenter_uin="20000", commenter_nickname="小红")
    res = asyncio.run(p.qzone_comment(feed_id="notifyfeed01", content="谢谢你!",
                                      at_user_id="20000", stream_id="s1"))
    assert res.startswith("评论成功,已发出:") and "@小红" in res
    # 源A:说说主人=bot 自己,评论带 @ 前缀(napcat 适配器同格式)
    assert p.qzone_client.comment_calls == [
        ("notifyfeed01", BOT_UIN, "@{uin:20000,nick:小红,auto:1}谢谢你!")
    ]
    events = p.qzone_comment_seen.store.query(
        "SELECT user_id, kind FROM qzone_fav_events WHERE user_id = '20000'"
    )
    assert events and events[0][1] == "COMMENT"  # @ 互动记向被@者


def test_qzone_comment_frequency_limit_three(tmp_path):
    """频控:同说说评论上限 3 次——第 4 次拒绝(零写调用),提示适可而止;
    不同说说互不影响(计数按 tid 分键)。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedA", owner="10001")
    _register_feed(p, tid="feedB", owner="10002")
    for i in range(3):
        res = asyncio.run(p.qzone_comment(feed_id="feedA", content=f"第{i}条", stream_id="s1"))
        assert res.startswith("评论成功,已发出:")
    res4 = asyncio.run(p.qzone_comment(feed_id="feedA", content="第四条", stream_id="s1"))
    assert res4 == "这条说说你已经评论过 3 次了,适可而止～"
    assert len(p.qzone_client.comment_calls) == 3  # 第 4 条零写调用
    res_b = asyncio.run(p.qzone_comment(feed_id="feedB", content="另一条", stream_id="s1"))
    assert res_b.startswith("评论成功,已发出:")  # 其它说说不受影响
    assert len(p.qzone_client.comment_calls) == 4


def test_qzone_comment_target_resolution_failure_and_fallback(tmp_path):
    """目标解析失败:registry/seen_store/awaiting 均未命中 → 显式失败提示
    (零写调用,不臆造目标);seen_store 回退:7 天内浏览过的动态按前缀命中。"""

    p = _make_tool_plugin(tmp_path)
    res = asyncio.run(p.qzone_comment(feed_id="nosuchid", content="?", stream_id="s1"))
    assert "未找到说说" in res
    assert p.qzone_client.comment_calls == []
    # 回退:registry 无记录,但 seen_store 有 7 天内浏览记录(前缀匹配)
    p.qzone_seen.mark_queued("seentid0001", abstime="1", author_uin="10003", summary="旧动态")
    p.qzone_seen.mark_seen("seentid0001", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    res2 = asyncio.run(p.qzone_comment(feed_id="seentid0001", content="补一条", stream_id="s1"))
    assert res2.startswith("评论成功,已发出:")
    assert p.qzone_client.comment_calls == [("seentid0001", "10003", "补一条")]


def test_qzone_comment_auth_error_invalidates_cookie(tmp_path):
    """AuthError 自愈链:登录态失效→cookie 作废(下轮重取)+ 明确提示;
    频控计数不累计(远端未成功),seen_store 回退链不动。"""

    class _AuthFailClient(_StubWriteClient):
        async def do_comment(self, *, fid, target_qq, content):
            raise QzoneAuthError("登录态失效(code=-3000)")

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedX", owner="10001")
    p.qzone_client = _AuthFailClient()
    res = asyncio.run(p.qzone_comment(feed_id="feedX", content="你好", stream_id="s1"))
    assert "登录态失效已重置" in res
    assert p.qzone_cookie.invalidate_calls == 1  # cookie 已作废(下轮重取)
    assert p._qzone_comment_counts == {}  # 失败不计入频控


def test_qzone_reply_real_thread_with_correct_pair(tmp_path):
    """qzone_reply 真实楼中楼(源A 形态):commentId+commentUin 二元组精确匹配
    主评论(通知登记:主评论作者=评论好友),@ 目标=评论者昵称;do_reply 正式接线
    (不再降级头评)。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedR1", owner=BOT_UIN,
                   commenter_uin="20000", commenter_nickname="小红",
                   comment_tid="ct9", comment_uin="20000")
    res = asyncio.run(p.qzone_reply(feed_id="feedR1", comment_id="ct9", content="谢谢!", stream_id="s1"))
    assert res.startswith("回复成功,已回复 小红 的评论:")
    # 二元组对位:fid=全量 tid,target_qq=说说主人(源A=bot),commentUin=主评论作者
    assert p.qzone_client.reply_calls == [
        ("feedR1", BOT_UIN, "ct9", "20000", "小红", "谢谢!", "20000", "小红")
    ]
    assert p.qzone_client.comment_calls == []  # 不再降级头评
    events = p.qzone_comment_seen.store.query(
        "SELECT user_id, kind FROM qzone_fav_events WHERE user_id = '20000'"
    )
    assert events and events[0][1] == "COMMENT"  # 回复互动记向评论者


def test_qzone_reply_source_b_pair_bot_head_commenter_target(tmp_path):
    """源B 形态:被回复的主评论是 bot 自己的(二元组 commentUin=bot),@
    的是回复者——二元组与 @ 目标解耦(wire.build_reply_form 承载)。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedR2", owner="30000",
                   commenter_uin="30000", commenter_nickname="阿好",
                   comment_tid="bc1", comment_uin=BOT_UIN)
    res = asyncio.run(p.qzone_reply(feed_id="feedR2", comment_id="bc1", content="说得对", stream_id="s1"))
    assert res.startswith("回复成功,已回复 阿好 的评论:")
    fid, target_qq, ctid, cuin, cnick, content, at_uin, at_nick = p.qzone_client.reply_calls[0]
    assert (fid, target_qq, ctid) == ("feedR2", "30000", "bc1")
    assert cuin == BOT_UIN  # 二元组:主评论作者=bot(线程头)
    assert at_uin == "30000" and at_nick == "阿好"  # @ 目标=回复者(解耦)


def test_qzone_reply_requires_all_params_and_session(tmp_path):
    """入参校验:缺说说ID/评论ID/内容 → 显式提示;非虚拟流会话 → 拒绝(零写调用)。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedR3", owner="10001")
    res = asyncio.run(p.qzone_reply(feed_id="", comment_id="c1", content="x", stream_id="s1"))
    assert "都不能为空" in res
    res2 = asyncio.run(p.qzone_reply(feed_id="feedR3", comment_id="c1", content="x", stream_id="other"))
    assert res2 == "这个工具只能在QQ空间动态流里使用。"
    assert p.qzone_client.reply_calls == []


# ---- M3 表达:qzone_post 工具行为测试 ----


def test_qzone_post_success_publishes_and_echoes(tmp_path):
    """qzone_post 成功路径(两段式):正文经表达生成层产出(表达方向→人设正文),
    do_publish 收到生成结果;回注 self 消息进虚拟流——user_id=bot 自己、
    无 is_mentioned(bot 自己发的不触发 planner 决策轮,仅入历史供后续互动引用
    上下文);尾部带〔说说ID=前12位〕锚(模型照抄锚值即可对该说说评论/点赞);
    成功日志含前 30 字预览。"""

    p = _make_tool_plugin(tmp_path)
    res = asyncio.run(p.qzone_post(content="  今天散步看到一只很亲人的猫  ", stream_id="s1"))
    assert res.startswith("发布成功,已发出说说:")
    assert p.qzone_client.publish_calls == ["今天散步看到一只很亲人的猫"]  # 生成正文(首尾空白已剥)
    # 表达生成调用:模块/模型走 qzone 节表达配置
    assert p.expr_llm_calls[0]["module"] == "qzone_expression"
    assert p.expr_llm_calls[0]["model"] == p.config.qzone.expression_llm_model
    # 回注:经网关进虚拟流,qzone_self_ 前缀 message_id
    assert len(p._ctx.gateway.calls) == 1
    gw_name, msg = p._ctx.gateway.calls[0]
    assert gw_name == QZONE_GATEWAY_NAME
    assert msg["message_id"].startswith("qzone_self_")
    assert msg["platform"] == QZONE_PLATFORM
    assert msg["message_info"]["user_info"]["user_id"] == BOT_UIN
    assert msg["message_info"]["group_info"]["group_id"] == p.config.qzone.virtual_group_id
    # 无 is_mentioned:主程序只读 message_info.additional_config 位置,回注不设即不触发决策轮
    assert "is_mentioned" not in (msg["message_info"].get("additional_config") or {})
    assert msg["raw_message"] == [
        {"type": "text", "data": "我发布了一条说说:今天散步看到一只很亲人的猫\n〔说说ID=newtid0001〕"}
    ]
    assert any(
        level == "info" and "QQ空间说说发布成功" in str(a[0]) and a[1] == "今天散步看到一只很亲人的猫"[:30]
        for level, a in p.logs
    )


def test_qzone_post_anchors_seen_and_registry(tmp_path):
    """发布后本地锚定三连:seen 库 queued→seen(own-post 摘要=正文前 50 字,
    message_id=回注消息 id,后续通知 reply 段据此引用);registry 登记
    kind="self" 自锚(content_summary=正文前 100 字,表达生成场景素材)——
    模型照抄回注锚(前 12 位)即可解析到该说说。"""

    p = _make_tool_plugin(tmp_path)
    p.qzone_client.publish_tid = "fullpubtid000123456"
    asyncio.run(p.qzone_post(content="今天的心情很不错,写点什么好呢", stream_id="s1"))
    _, msg = p._ctx.gateway.calls[0]
    assert msg["raw_message"][0]["data"].endswith("\n〔说说ID=fullpubtid00〕")  # 锚取前 12 位
    rows = p.qzone_seen.recent_seen(limit=5, days=1, now=datetime.now())
    assert any(
        r["tid"] == "fullpubtid000123456" and r["author_uin"] == BOT_UIN
        and r["author_nickname"] == "我" and r["summary"] == "今天的心情很不错,写点什么好呢"
        for r in rows
    )  # summary=正文前 50 字(Task 6 own-post 摘要依赖)
    assert p.qzone_seen.get_message_id("fullpubtid000123456") == msg["message_id"]
    ctx = p._qzone_registry.resolve("fullpubtid00")  # 前缀锚解析(registry 两级口径)
    assert ctx is not None and ctx.tid == "fullpubtid000123456"
    assert ctx.owner_uin == BOT_UIN and ctx.owner_nickname == "我" and ctx.kind == "self"
    assert ctx.content_summary == "今天的心情很不错,写点什么好呢"  # 表达生成场景素材


def test_qzone_post_route_failure_skips_local_anchor(tmp_path):
    """Task 4 缓议修正:回注 route 失败(异常)时本地锚定三连跳过——注入没成功
    就无 message_id 可挂,锚定必落空(与日记补注路径对称);发布回执不受影响
    (说说已远端发布成功,谎报失败会诱导重复发布)。"""

    p = _make_tool_plugin(tmp_path)
    p.qzone_client.publish_tid = "routefailtid0001"
    p._ctx.gateway = _ExplodingGateway()
    res = asyncio.run(p.qzone_post(content="回注会炸", stream_id="s1"))
    assert res.startswith("发布成功,已发出说说:")
    assert p.qzone_client.publish_calls == ["回注会炸"]
    assert p.qzone_seen.recent_seen(limit=10, days=1, now=datetime.now()) == []  # seen 零登记
    assert p._qzone_registry.resolve("routefailtid0001") is None  # registry 零登记


def test_qzone_post_missing_tid_warns_and_skips_anchor(tmp_path):
    """响应缺 tid:发布不误报失败(回执仍成功),告警显式暴露;回注文本无锚,
    seen/registry 零登记(tid 未知无从登记,浏览发现层后续自行补)。"""

    p = _make_tool_plugin(tmp_path)
    p.qzone_client.publish_tid = ""
    res = asyncio.run(p.qzone_post(content="发了但拿不到 tid", stream_id="s1"))
    assert res.startswith("发布成功,已发出说说:")
    _, msg = p._ctx.gateway.calls[0]
    assert msg["raw_message"][0]["data"] == "我发布了一条说说:发了但拿不到 tid"
    assert any(
        level == "warning" and "未含新说说 tid" in str(a[0]) and "回注缺锚" in str(a[0])
        for level, a in p.logs
    )
    assert p.qzone_seen.recent_seen(limit=10, days=1, now=datetime.now()) == []
    assert p._qzone_registry.resolve("") is None


def test_qzone_post_echo_content_truncated_to_sixty(tmp_path):
    """回注正文只带前 60 字预览:说说全文已真实发布在空间,回注只是让 bot 记得
    自己发过什么,超长正文整段塞进虚拟流会挤占上下文。"""

    long_content = "字" * 80
    p = _make_tool_plugin(tmp_path)
    asyncio.run(p.qzone_post(content=long_content, stream_id="s1"))
    assert p.qzone_client.publish_calls == [long_content]  # 全文发布(表达方向即正文的桩形态)
    _, msg = p._ctx.gateway.calls[0]
    # 回注截 60 字;锚行独立成行,不受预览截断影响
    assert msg["raw_message"][0]["data"] == f"我发布了一条说说:{'字' * 60}...\n〔说说ID=newtid0001〕"


def test_qzone_post_validation_empty_content_and_session(tmp_path):
    """入参校验:空内容 → 显式拒绝;模块未启用 → 拒绝;两种拒绝形态均零
    发布调用、零回注。2026-09-02 起全域工具:不再有流门控(真实流亦可发,
    任意 stream_id 一视同仁)。"""

    p = _make_tool_plugin(tmp_path)
    res = asyncio.run(p.qzone_post(content="   ", stream_id="s1"))
    assert "说说内容不能为空" in res
    p._qzone_available = False
    assert asyncio.run(p.qzone_post(content="你好", stream_id="s1")) == "QQ空间模块未启用。"
    assert p.qzone_client.publish_calls == []
    assert p._ctx.gateway.calls == []
    # 全域化回归锁:恢复可用后,非虚拟流 stream_id 不再被流门控拒绝(直通发布)
    p._qzone_available = True
    res = asyncio.run(p.qzone_post(content="你好", stream_id="other"))
    assert "发布成功" in res and p.qzone_client.publish_calls


def test_qzone_post_auth_error_invalidates_cookie(tmp_path):
    """AuthError 自愈链(发布路径):登录态失效→cookie 作废(下轮重取)+明确提示;
    不回注(bot 没发出去,虚拟流里不该出现「我发了」的假上下文)。"""

    class _AuthFailPostClient(_StubWriteClient):
        async def do_publish(self, *, content):
            raise QzoneAuthError("登录态失效(code=-3000)")

    p = _make_tool_plugin(tmp_path)
    p.qzone_client = _AuthFailPostClient()
    res = asyncio.run(p.qzone_post(content="想发点什么都发不出去", stream_id="s1"))
    assert "登录态失效已重置" in res
    assert p.qzone_cookie.invalidate_calls == 1
    assert p._ctx.gateway.calls == []


def test_qzone_post_generic_failure_logs_and_no_echo(tmp_path):
    """普通失败:异常显式记录日志(不静默),回执失败提示,零回注。"""

    class _BoomPostClient(_StubWriteClient):
        async def do_publish(self, *, content):
            raise RuntimeError("空间写请求业务错误: code=-3")

    p = _make_tool_plugin(tmp_path)
    p.qzone_client = _BoomPostClient()
    res = asyncio.run(p.qzone_post(content="测试", stream_id="s1"))
    assert res == "发布失败,已记录日志。"
    assert any(level == "exception" for level, _a in p.logs)
    assert p._ctx.gateway.calls == []


def test_qzone_post_echo_failure_still_reports_success(tmp_path):
    """回注失败不影响发布回执:说说已真实发布在空间,回注只是本地上下文注入
    (网关拒绝/异常时告警即可,不能向 bot 谎报「发布失败」导致重复发布)。"""

    p = _make_tool_plugin(tmp_path)
    p._ctx.gateway = _ExplodingGateway()
    res = asyncio.run(p.qzone_post(content="发布成功但回注会炸", stream_id="s1"))
    assert res.startswith("发布成功,已发出说说:")
    assert p.qzone_client.publish_calls == ["发布成功但回注会炸"]
    assert any(
        level == "exception" and "回注失败" in str(a[0]) and "发布已成功" in str(a[0])
        for level, a in p.logs
    )


# ---- M3 表达:日记(入睡旁路生成 + API 直发 + 延迟回注) ----


def _make_diary_plugin(tmp_path):
    """日记路径装配:发布客户端桩 + 旁路 LLM 记录桩 + 当日素材(日程/备忘/见闻)。"""

    p = _make_tool_plugin(tmp_path)  # _StubWriteClient(记录 do_publish)
    # 聊天时间线素材依赖(on_load 装配,离线手工补):流缓存置空=素材行省略,
    # 各测试按需注入;_stream_cache_at 置 0(时间线不刷流列表,不受 TTL 影响)
    p._stream_cache = {}
    p._stream_cache_at = 0.0
    p._schedule_data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "windows": [
            {"kind": "daily", "start": f"{datetime.now().strftime('%Y-%m-%d')}T09:00",
             "end": f"{datetime.now().strftime('%Y-%m-%d')}T12:00",
             "activity": "窝着刷手机", "plan_speak": False, "topic": "", "read_qzone": True},
            {"kind": "sleep", "start": f"{datetime.now().strftime('%Y-%m-%d')}T23:00",
             "end": "2026-12-31T07:30"},
        ],
    }
    p.memo.write("周四交作业", "s1", "10001", 24,
                 remind_at=f"{datetime.now().strftime('%Y-%m-%d')}T20:00")
    p.qzone_seen.mark_queued("diaryfeed1", abstime="1", author_uin="10001",
                             summary="今天去公园散步,拍了很多照片", author_nickname="小明")
    p.qzone_seen.mark_seen("diaryfeed1", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    llm_calls: list = []

    async def _fake_side_llm(messages, model, module, timeout_ms=None):
        llm_calls.append({"messages": messages, "model": model, "module": module})
        return {"success": True, "response": "今天窝着刷手机,看到小明去公园散步,有点懒洋洋的。"}

    p._side_llm_call = _fake_side_llm
    p.llm_calls = llm_calls
    return p


def test_diary_generation_publishes_with_daily_material(tmp_path):
    """日记成功路径:入睡任务用当日素材(日程活动+备忘+空间见闻)组装 prompt
    (模板 qzone_diary),LLM 产出正文直接 do_publish(API 直发不经消息链,
    不受睡眠拦截);发布后正文+发布时刻存 pending 快照供醒来回注。"""

    p = _make_diary_plugin(tmp_path)
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == ["今天窝着刷手机,看到小明去公园散步,有点懒洋洋的。"]
    # 旁路调用:模块名与模型走 qzone 节日记配置
    assert p.llm_calls[0]["module"] == "qzone_diary"
    assert p.llm_calls[0]["model"] == p.config.qzone.diary_llm_model
    # 素材蓝本形态(v6,diary_plugin prompts.py):「我的名字是」头 + 聊天记录
    # 回顾引入语 + 当日素材 +「日记内容:」收尾引导
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert stable.startswith("我的名字是")
    assert "回顾一下到现在为止的聊天记录:" in stable
    assert "(今天没和人聊天)" in stable  # 无聊天素材的诚实占位(不臆造)
    assert "今天的日程:" in stable and "窝着刷手机" in stable
    assert "23:00" not in stable  # 睡眠窗口不进素材
    assert "周四交作业" in stable
    assert "今天去公园散步" in stable
    assert stable.endswith("日记内容:")  # 生成引导紧跟素材(蓝本单串布局等价位)
    # 快照已存(醒来回注素材):正文 + 发布时刻 + 新说说 tid(醒后锚定用)
    data = p._pending_diary_snapshot.load()
    assert data.get("text") == "今天窝着刷手机,看到小明去公园散步,有点懒洋洋的。"
    assert data.get("published_at")
    assert data.get("tid") == "newtid0001"
    assert any(
        level == "info" and "QQ空间日记发布成功" in str(a[0]) for level, a in p.logs
    )


def _diary_msg(user_id: str, nickname: str, text: str, ts: float) -> dict:
    """get_recent/get_by_time 桩消息(与实机形态对齐:message_info.user_info + timestamp + raw_message)。"""

    return {
        "message_info": {"user_info": {"user_id": user_id, "user_nickname": nickname}},
        "timestamp": str(int(ts)),
        "raw_message": [{"type": "text", "data": text}],
    }


def test_diary_chat_timeline_full_day_global_fetch(tmp_path):
    """日记聊天时间线(2026-09-02 对齐 diary_plugin message_fetcher 蓝本):
    message.get_by_time **全局**拉当日全部消息(跨流,limit=0 不限条数,流缓存
    不再是覆盖面瓶颈);逐条「[HH:MM] 谁说了什么」时间序铺开;单条截 100 字尾
    加"...";bot 标「我:」,他人标昵称;空间虚拟流消息(平台 qzone-qq)剔除;
    昨日消息不进素材。"""

    import time as _time

    now = datetime.now()
    p = _make_diary_plugin(tmp_path)
    p._stream_cache = {}  # 主路径不依赖流缓存(全局取数)
    p._stream_cache_at = _time.time()

    # 当日内取两个时刻(不早于今天 00:00——测试跑在凌晨时也不落到昨日被日过滤剔除)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t1 = max(day_start + timedelta(minutes=5), now - timedelta(minutes=30))
    t2 = now - timedelta(seconds=30)
    label1, label2 = f"[{t1:%H:%M}]", f"[{t2:%H:%M}]"
    msgs = [
        _diary_msg("40000", "群友", "早上冒个泡", t1.timestamp()),
        # bot 一条(标「我:」)+好友一条(标昵称)+超长单条(截 100 字加"...")
        _diary_msg(BOT_UIN, "我", "早上好呀", t2.timestamp()),
        _diary_msg("20000", "小红", "今天去公园了吗", t2.timestamp()),
        _diary_msg("30000", "小蓝", "长" * 120, t2.timestamp()),
        # 昨日消息(整一天前,日期不同)不进素材
        _diary_msg("20000", "小红", "昨天的消息", (t2 - timedelta(days=1)).timestamp()),
    ]
    # 空间虚拟流消息剔除:全局取数跨未知流,按平台判定(qzone-qq)
    virtual_msg = _diary_msg("50000", "空间好友", "这条是虚拟流的消息", t2.timestamp())
    virtual_msg["platform"] = "qzone-qq"
    msgs.append(virtual_msg)

    calls: list[dict] = []

    async def _capability(name, **kw):
        calls.append({"name": name, **kw})
        if name == "message.get_by_time":
            return {"success": True, "messages": msgs}
        raise AssertionError(f"主路径可用时不应调用其它能力:{name}")

    p._ctx.call_capability = _capability
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    timeline = stable.split("回顾一下到现在为止的聊天记录:\n", 1)[1].split("\n\n今天的日程:", 1)[0]
    assert f"{label1} 群友:早上冒个泡" in timeline  # 带时间戳逐条铺开
    assert f"{label2} 我:早上好呀" in timeline  # bot 消息标「我:」
    assert f"{label2} 小红:今天去公园了吗" in timeline  # 他人标昵称
    assert "长" * 100 + "..." in timeline and "长" * 101 not in timeline  # 单条截 100 加"..."
    assert "昨天的消息" not in timeline  # 昨日剔除
    assert "虚拟流的消息" not in timeline  # 虚拟流剔除(平台判定)
    # 主路径参数(蓝本 message_fetcher 同款):当日时间窗 + 不限条数 + 最早优先
    assert calls and calls[0]["name"] == "message.get_by_time"
    assert calls[0]["limit"] == 0 and calls[0]["limit_mode"] == "earliest"
    assert calls[0]["start_time"] < t1.timestamp() and calls[0]["end_time"] > t2.timestamp()


def test_diary_chat_timeline_fallback_to_per_stream(tmp_path):
    """get_by_time 失败(能力异常)→ 显式告警后回退旧逐流 get_recent 路径,
    时间线仍可用(显式回退不静默);虚拟流会话仍被排除(名单判定)。"""

    import time as _time

    today = datetime.now()
    p = _make_diary_plugin(tmp_path)
    virtual_sid = "qzone-virtual-stream"
    p._qzone_session_ids = {virtual_sid}
    p._stream_cache = {
        "g1": {"session_id": "g1", "is_group_session": True, "user_id": ""},
        virtual_sid: {"session_id": virtual_sid, "is_group_session": True, "user_id": ""},
    }
    p._stream_cache_at = _time.time()

    ten_five = today.replace(hour=10, minute=5, second=0, microsecond=0)
    msgs = [_diary_msg("40000", "群友", "回退路径消息", ten_five.timestamp())]
    requested: list[str] = []

    async def _capability(name, **kw):
        if name == "message.get_by_time":
            raise RuntimeError("能力不可用")
        if name == "message.get_recent":
            requested.append(str(kw.get("chat_id")))
            return list(msgs)
        return {"success": True, "response": "{}"}

    p._ctx.call_capability = _capability
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "[10:05] 群友:回退路径消息" in stable
    assert requested == ["g1"]  # 虚拟流取数不发生(排除名单生效)
    # 显式回退告警(不静默)
    assert any(level == "exception" and "get_by_time" in str(a[0]) for level, a in p.logs)

    # 反例:流内无当日消息 → 素材行整体省略
    async def _empty(name, **kw):
        del name, kw
        return []

    p._ctx.call_capability = _empty
    p.llm_calls.clear()
    asyncio.run(p._generate_and_publish_diary())
    assert "今天的聊天:" not in p.llm_calls[0]["messages"][1]["content"]


def test_diary_weather_line_and_random_target_words(tmp_path, monkeypatch):
    """M3 修正(规格 §5.2/§5.3):日记素材补真实天气(time_aware 快照,无数据
    省略该行)+目标字数随机化——stable_ctx 注入「本次目标约 {n} 字」,
    n=random.randint(80,200)(模板「80~200字」表述保留)。"""

    import re as _re

    p = _make_diary_plugin(tmp_path)
    # 快照表缺位(离线未建表):素材行省略并告警,不臆造天气
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "当前天气" not in stable
    assert any(
        level == "exception" and "日记天气素材" in str(a[0]) for level, a in p.logs
    )
    m = _re.search(r"\(目标 (\d+) 字左右\)", stable)
    assert m and 80 <= int(m.group(1)) <= 200  # 随机目标字数注入(区间锁定)
    assert "80~200字左右" in p.llm_calls[0]["messages"][0]["content"]  # 模板区间表述保留

    # 有快照:素材行出现(温度/天气码来自快照);随机字数可确定性注入
    p.store.execute(
        "CREATE TABLE IF NOT EXISTS weather_snapshot ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), city TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL, data TEXT NOT NULL)"
    )
    p.store.execute(
        "INSERT INTO weather_snapshot (id, city, fetched_at, data) VALUES (1, '珠海', "
        "'2026-09-01T12:00:00', '{\"temperature_2m\": 26.5, \"weather_code\": 1}')"
    )
    import plugin as plugin_mod

    monkeypatch.setattr(plugin_mod.random, "randint", lambda a, b: 123)
    p.llm_calls.clear()
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "当前天气:温度 26.5°C(天气码 1)" in stable
    assert "(目标 123 字左右)" in stable


def test_diary_generation_disabled_or_unavailable_skips(tmp_path):
    """开关守卫:diary_enabled 关闭 / qzone 模块停用时零 LLM 零发布零快照。"""

    p = _make_diary_plugin(tmp_path)
    p.config.qzone.diary_enabled = False
    asyncio.run(p._generate_and_publish_diary())
    assert p.llm_calls == [] and p.qzone_client.publish_calls == []
    p.config.qzone.diary_enabled = True
    p._qzone_available = False
    asyncio.run(p._generate_and_publish_diary())
    assert p.llm_calls == [] and p.qzone_client.publish_calls == []
    assert p._pending_diary_snapshot.load() == {}


def test_diary_generation_llm_failure_skips_publish(tmp_path):
    """LLM 失败(异常/不成功)显式告警跳过:零发布零快照(不编造日记)。"""

    p = _make_diary_plugin(tmp_path)

    async def _fail(messages, model, module, timeout_ms=None):
        return {"success": False, "response": "boom"}

    p._side_llm_call = _fail
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == []
    assert p._pending_diary_snapshot.load() == {}
    assert any(
        level == "warning" and "QQ空间日记 LLM 失败" in str(a[0]) for level, a in p.logs
    )


def test_diary_generation_abnormal_length_skips_publish(tmp_path):
    """内容护栏:模板要求 80~200 字,超 300 字视为异常输出跳过发布(不截断硬发)。"""

    p = _make_diary_plugin(tmp_path)

    async def _long(messages, model, module, timeout_ms=None):
        return {"success": True, "response": "字" * 301}

    p._side_llm_call = _long
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == []
    assert p._pending_diary_snapshot.load() == {}
    assert any(
        level == "warning" and "内容异常" in str(a[0]) for level, a in p.logs
    )


def test_diary_generation_publish_failure_no_snapshot(tmp_path):
    """发布 API 失败:告警跳过,不落 pending 快照——没发出去的日记不该在
    醒来后以「昨晚发布的日记」回注成假上下文。"""

    class _BoomPublish(_StubWriteClient):
        async def do_publish(self, *, content):
            raise RuntimeError("空间写请求业务错误: code=-3")

    p = _make_diary_plugin(tmp_path)
    p.qzone_client = _BoomPublish()
    asyncio.run(p._generate_and_publish_diary())
    assert p._pending_diary_snapshot.load() == {}
    assert any(
        level == "exception" and "QQ空间日记发布失败" in str(a[0]) for level, a in p.logs
    )


def test_echo_pending_diary_routes_self_message_and_clears(tmp_path):
    """醒来补注:pending 快照非空 → self 消息经网关注入虚拟流(user=bot 自己,
    无 is_mentioned 不触发决策轮,仅入历史供后续互动引用);正文截 60 字预览;
    成功后快照清空(只补注一次)。"""

    p = _make_diary_plugin(tmp_path)
    p._pending_diary_snapshot.save({"text": "昨晚的日记正文", "published_at": "2026-09-01T23:05:00"})
    asyncio.run(p._echo_pending_diary())
    assert len(p._ctx.gateway.calls) == 1
    gw_name, msg = p._ctx.gateway.calls[0]
    assert gw_name == QZONE_GATEWAY_NAME
    assert msg["message_id"].startswith("qzone_self_diary_")
    assert msg["platform"] == QZONE_PLATFORM
    assert msg["message_info"]["user_info"]["user_id"] == BOT_UIN
    assert msg["message_info"]["group_info"]["group_id"] == p.config.qzone.virtual_group_id
    assert "is_mentioned" not in (msg["message_info"].get("additional_config") or {})
    assert msg["raw_message"] == [{"type": "text", "data": "我昨晚发布的日记:昨晚的日记正文"}]
    assert p._pending_diary_snapshot.load() == {}
    assert any(
        level == "info" and "日记醒来补注完成" in str(a[0]) for level, a in p.logs
    )


def test_echo_pending_diary_truncates_to_sixty(tmp_path):
    """补注正文只带前 60 字预览:全文已真实发布在空间,回注只是上下文锚,
    超长正文整段塞进虚拟流会挤占上下文(与 qzone_post 回注同纪律)。"""

    p = _make_diary_plugin(tmp_path)
    p._pending_diary_snapshot.save({"text": "字" * 80})
    asyncio.run(p._echo_pending_diary())
    _, msg = p._ctx.gateway.calls[0]
    assert msg["raw_message"][0]["data"] == f"我昨晚发布的日记:{'字' * 60}..."  # 旧快照无 tid:无锚行(截断尾加"...")


def test_echo_pending_diary_with_tid_anchors_seen_and_registry(tmp_path):
    """快照带 tid 的补注(本任务起的新形态):回注文本尾部带〔说说ID=前12位〕
    锚;route 成功后 seen(queued→seen,summary=日记正文前 50 字)+
    registry kind="self" 同款锚定(Task 6/10 own-post 依赖);锚定失败不拦
    快照清空(远端已成功,补注一次即清)。"""

    p = _make_diary_plugin(tmp_path)
    p._pending_diary_snapshot.save({"text": "昨晚的日记正文", "published_at": "2026-09-01T23:05:00",
                                    "tid": "diarytid000456789"})
    asyncio.run(p._echo_pending_diary())
    _, msg = p._ctx.gateway.calls[0]
    assert msg["raw_message"][0]["data"] == "我昨晚发布的日记:昨晚的日记正文\n〔说说ID=diarytid0004〕"
    rows = p.qzone_seen.recent_seen(limit=5, days=1, now=datetime.now())
    assert any(
        r["tid"] == "diarytid000456789" and r["summary"] == "昨晚的日记正文"
        and r["author_uin"] == BOT_UIN and r["author_nickname"] == "我"
        for r in rows
    )
    assert p.qzone_seen.get_message_id("diarytid000456789") == msg["message_id"]
    ctx = p._qzone_registry.resolve("diarytid0004")  # 前缀锚解析
    assert ctx is not None and ctx.kind == "self" and ctx.owner_uin == BOT_UIN
    assert ctx.content_summary == "昨晚的日记正文"  # 表达生成场景素材(正文前 100 字)
    assert p._pending_diary_snapshot.load() == {}


def test_echo_pending_diary_empty_noop(tmp_path):
    """快照为空(无待回注日记):零注入零日志,静默返回。"""

    p = _make_diary_plugin(tmp_path)
    asyncio.run(p._echo_pending_diary())
    assert p._ctx.gateway.calls == []


def test_echo_pending_diary_failure_keeps_pending(tmp_path):
    """补注失败(网关异常):快照保留,醒态 sleep_tick 下轮重试——上下文锚
    丢了会让 bot 忘记自己发过日记,值得重试而非放弃。"""

    p = _make_diary_plugin(tmp_path)
    p._pending_diary_snapshot.save({"text": "待补注日记"})
    p._ctx.gateway = _ExplodingGateway()
    asyncio.run(p._echo_pending_diary())
    assert p._pending_diary_snapshot.load().get("text") == "待补注日记"
    assert any(
        level == "exception" and "补注失败" in str(a[0]) for level, a in p.logs
    )


def test_diary_wired_into_sleep_flow():
    """接线断言:入睡与「睡眠窗口终点未入睡补执行」两条路径都派生日记任务;
    sleep_tick 醒态(自然醒瞬间与醒着兜底)补注待回注日记(失败下轮重试)。"""
    import inspect

    import plugin as plugin_mod

    assert "_generate_and_publish_diary" in inspect.getsource(plugin_mod.CatsitatePlugin._enter_sleep)
    assert "_generate_and_publish_diary" in inspect.getsource(
        plugin_mod.CatsitatePlugin._maybe_settle_passed_sleep_window
    )
    assert "_echo_pending_diary" in inspect.getsource(plugin_mod.CatsitatePlugin._sleep_tick)


def test_qzone_like_via_feed_id_and_notify_origin(tmp_path):
    """qzone_like v0.7:feed_id 参数(锚前缀)经 registry 解析为全量 tid;
    通知 awaiting 不再拒赞——缺省目标取 origin_tid(真实说说),owner=源A=bot。"""

    import time as _time

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="likefeed0001", owner="10001", nickname="小明")
    res = asyncio.run(p.qzone_like(feed_id="likefeed0001", stream_id="s1"))
    assert res.startswith("点赞成功:小明 的说说")
    assert p.qzone_client.like_calls == [("likefeed0001", "10001")]  # 全量 tid+主人

    # 通知项 awaiting(合成 tid):缺省目标=origin_tid 真实说说,可点其原说说;
    # 泵注入时会登记 FeedContext(owner=源A主人=bot,昵称=评论者)——此处按同款登记模拟
    p2 = _make_tool_plugin(tmp_path)
    _register_feed(p2, tid="realtid", owner=BOT_UIN, nickname="小红")
    p2.qzone_injector.window_started()
    p2.qzone_injector.enqueue_priority([FeedItem(
        tid="notify_comment_realtid_c1", abstime="1750000000", uin="20000",
        nickname="小红", content="评论了你的说说:好棒(说说 realtid · 评论 c1 · QQ 20000)",
        source="notify", origin_tid="realtid", friend_uin="",
    )])
    popped = p2.qzone_injector.next_to_inject(_time.monotonic())
    assert popped is not None
    p2.qzone_injector.mark_injected(popped.tid, _time.monotonic())
    res2 = asyncio.run(p2.qzone_like(stream_id="s1"))
    assert res2.startswith("点赞成功:小红 的说说")
    assert p2.qzone_client.like_calls == [("realtid", BOT_UIN)]  # 真实 tid+源A主人=bot


def test_notify_poll_self_skip_dedup_and_awaiting_occupied(tmp_path):
    """统一通知轮询三重守卫(T11):①bot 自评跳过不注入;②is_new 判重(二次轮询
    不重注入);③awaiting 占用时不取数(上一条还在等回复——工具驱动下无需意图
    互斥,registry 登记常驻,awaiting 即串行信号)。"""

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
    p.qzone_injector.window_started()  # 浏览路径走泵需窗口(通知推送语义本不依赖)
    p.qzone_client = _StubCommentClient(comments, {"feed1": "今天的心情"})

    # ①首轮:自评跳过(登记不注入),好友评论通知经泵注入恰好 1 条
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_info"]["user_info"]["user_id"] == "20000"
    ctx = p._qzone_registry.resolve("feed1")
    assert ctx is not None  # 泵登记 FeedContext(源A:主人=bot,评论者=小红)
    assert (ctx.owner_uin, ctx.commenter_uin, ctx.comment_tid, ctx.comment_uin) == \
        (BOT_UIN, "20000", "c1", "20000")

    # ②awaiting 占用(轮未完成):不取数不注入(上一条通知还在等回复,不叠加)
    assert p.qzone_injector.awaiting_feed is not None
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1 and p.qzone_client.fetches == 1

    # ③轮完成后重扫:释放 awaiting(bot 已用工具回应或保持沉默),自评仍跳过,
    # 好友评论 is_new 判重 → 不重注入(只多了一次取数)
    p.qzone_injector.on_turn_complete(_time.monotonic())
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    assert p.qzone_client.fetches == 2  # 取数发生(判重生效,不是早退)
    assert p.qzone_comment_seen.is_new("feed1:c1:20000") is False  # 已登记,判重依据


def test_notify_scan_guard_awaits_until_turn_complete(tmp_path):
    """深度审查 B-1:守卫判 awaiting——轮未完成时通知轮询不取数不叠加;
    awaiting 释放后才恢复(工具驱动:registry 上下文常驻,无意图互斥概念)。"""

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
    fetches: list = []

    class _ProbeClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([])  # 发现层空:不进源B拉取

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            fetches.append(1)
            return {}, {}

    p.qzone_client = _ProbeClient()
    asyncio.run(p._qzone_notify_scan())
    assert fetches == []  # awaiting 未释放:不取数(不叠加)
    p.qzone_injector.on_turn_complete(_time.monotonic())
    asyncio.run(p._qzone_notify_scan())
    assert fetches == [1]  # awaiting 释放后恢复取数


def test_notify_poll_source_b_reply_registers_friend_thread_context(tmp_path, monkeypatch):
    """T11/T4-② 源B:发现层显示被评论好友有新活动→拉取其说说→楼中楼回复(list_3)
    → 通知注入 → registry 登记指向好友说说(owner=好友,主评论二元组=bot 的评论,
    评论者=回复者——qzone_reply 的完整素材)。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)  # 源B每个好友前固定 2 秒间隔(I2),桩掉不真等

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 源B目标圈定:bot 曾在好友 30000 的说说下发过评论(note_bot_comment 留痕)
    # 登记时刻取当前时间(bot_commented_friends 带时间下界 D-1,硬编码日期会超窗失效)
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "我的评论", fresh)
    raw = {"usrinfo": {"uin": "30000"}, "msglist": [{"tid": "ffeed1", "content": "好友的说说正文" * 10,
        "commentlist": [
        # 空 reply_tid 的畸形回复(T11 审查遗留):跳过不构造通知(防空 tid 畸形请求)
        {"tid": "bc1", "uin": BOT_UIN, "list_3": [
            {"tid": "", "uin": "30000", "name": "阿好", "content": "畸形回复",
             "create_time": str(int(_time.time()))},
        ]},
        {"tid": "bc1", "uin": BOT_UIN, "content": "我的评论", "list_3": [
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
    # 原说说 ffeed1 曾注入过(seen 记录 message_id)→ 通知注入消息带 reply 段引用它
    p.qzone_seen.mark_queued("ffeed1", abstime="1750000000", author_uin="30000", summary="好友的说说正文")
    p.qzone_seen.mark_seen("ffeed1", "2026-09-01T10:00:00", "qzone_ffeed1_2")
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    msg = p._ctx.gateway.calls[0][1]
    assert msg["message_info"]["user_info"]["user_id"] == "30000"
    # reply 段置首引用原说说注入消息(napcat quote 式上下文关联;源B sender=说说主人);
    # 引用内容=原说说正文前 60 字(feed.origin_content,非通知文本)
    reply = msg["raw_message"][0]
    assert reply["type"] == "reply"
    assert reply["data"]["target_message_id"] == "qzone_ffeed1_2"
    assert reply["data"]["target_message_sender_id"] == "30000"
    assert reply["data"]["target_message_content"] == ("好友的说说正文" * 10)[:60] + "..."
    text = msg["raw_message"][1]["data"]
    # 工具驱动+可读性优化:楼中楼上下文(bot 原评论前 20 字)+参数独立尾行;
    # 评论ID=主评论 tid(bc1,bot 的评论),评论者QQ=回复者;尾段动作时间
    # (回复于…)=create_time(注入同刻→今天,HH:MM 随运行时刻→前缀+后缀断言)
    assert text.startswith("回复了你的评论「我的评论」:说得对\n〔说说ID=ffeed1 评论ID=bc1 评论者QQ=30000 回复于(今天")
    assert text.endswith(")〕")
    assert "你曾评论" not in text and "(通知)" not in text
    assert "notify_reply_ffeed1_rr1" in msg["message_id"]
    ctx = p._qzone_registry.resolve("ffeed1")
    assert ctx is not None
    # 源B 登记对位:owner=好友(说说主人);主评论二元组=bot 的评论(bc1,作者=bot);
    # 评论者=回复者阿好(@ 目标)
    assert (ctx.owner_uin, ctx.comment_tid, ctx.comment_uin) == ("30000", "bc1", BOT_UIN)
    assert (ctx.commenter_uin, ctx.commenter_nickname) == ("30000", "阿好")
    # 楼中楼回复键已登记(下轮判重,不重复通知)
    assert p.qzone_comment_seen.is_new("ffeed1:bc1:reply:rr1") is False


def test_notify_scan_source_b_reply_without_parent_content_falls_back(tmp_path, monkeypatch):
    """楼中楼上下文兜底(Q3=a):主评论条目缺 content(非实测形态容错)时,
    通知正文引用段降级「你之前的评论」,不空引号也不静默丢上下文。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed9", "30000", "我的评论", fresh)
    raw = {"usrinfo": {"uin": "30000"}, "msglist": [{"tid": "ffeed9", "content": "说说正文",
        "commentlist": [
        {"tid": "bc9", "uin": BOT_UIN, "list_3": [  # 无 content 键:parent_comment_content 空
            {"tid": "rr9", "uin": "30000", "name": "阿好", "content": "说得对",
             "create_time": str(int(_time.time()))},
        ]},
    ]}]}

    class _StubNoParentClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([
                FeedDiscovery(tid="ffeed9", uin="30000", nickname="阿好",
                              abstime=str(int(_time.time())), appid=311),
            ])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            assert target_uin == "30000"
            return raw

    p.qzone_client = _StubNoParentClient()
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    text = p._ctx.gateway.calls[0][1]["raw_message"][-1]["data"]
    # 同款参数行+动作时间(create_time=注入同刻→今天);HH:MM 随运行时刻→前缀+后缀断言
    assert text.startswith("回复了你的评论「你之前的评论」:说得对\n〔说说ID=ffeed9 评论ID=bc9 评论者QQ=30000 回复于(今天")
    assert text.endswith(")〕")


def test_notify_scan_source_c_like_events(tmp_path):
    """源C(Task 10):「与我相关」流赞事件 → 去重(qzone_likes 发现即登记)/
    跳过自己/新鲜度截断(过旧已登记不重扫)/fav_event(LIKE)记账/通知构造
    (「摘要」标题 + 点赞于参数行,ownfeed 未注入过则无 reply 段纯文本)。"""

    import time as _time

    now = int(_time.time())
    events = [
        LikeEvent(like_key="20000_10000_aaaa1111", liker_uin="20000", liker_nickname="小红",
                  owner_uin=BOT_UIN, target_tid="ownfeed1", create_time=str(now)),
        # bot 自己的赞:跳过不注入(自愈式自我互动排除)
        LikeEvent(like_key=f"{BOT_UIN}_10000_bbbb2222", liker_uin=BOT_UIN, liker_nickname="我",
                  owner_uin=BOT_UIN, target_tid="ownfeed1", create_time=str(now)),
        # 过旧(10 天前,默认 summary_days=3):新鲜度截断跳过(仍登记,下轮判重)
        LikeEvent(like_key="30000_10000_cccc3333", liker_uin="30000", liker_nickname="小蓝",
                  owner_uin=BOT_UIN, target_tid="ownfeed2", create_time=str(now - 10 * 86400)),
    ]

    class _StubLikeClient(_StubUnifiedClient):
        """源C 输入桩:发现层空(不进源B拉取),「与我相关」流返回固定赞事件。"""

        def __init__(self):
            super().__init__([])
            self.like_fetches = 0

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_like_events(self, *, count=30):
            del count
            self.like_fetches += 1
            return list(events)

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 自己的说说已发布回注(Task 4):qzone_feeds 有 summary → 通知带「摘要」标题
    p.qzone_seen.mark_queued("ownfeed1", abstime=str(now), author_uin=BOT_UIN, summary="我的晚间思绪")
    p.qzone_client = _StubLikeClient()

    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1  # 只有好友的赞注入(自己跳过/过旧截断)
    msg = p._ctx.gateway.calls[0][1]
    assert msg["message_info"]["user_info"]["user_id"] == "20000"
    assert "notify_like_20000_10000_aaaa1111" in msg["message_id"]
    # ownfeed1 无注入记录(未 mark_seen)→ 无 reply 段,首段即通知文本;
    # 正文=「摘要」标题 + 参数行(说说ID 锚 + 点赞于动作时间,create_time=注入
    # 同刻→今天,HH:MM 随运行时刻→前缀+后缀断言)
    text = msg["raw_message"][0]["data"]
    assert text.startswith("赞了你的说说「我的晚间思绪」\n〔说说ID=ownfeed1 点赞于(今天")
    assert text.endswith(")〕")
    # fav_event 记账(kind=LIKE):源C 好感度显式事件
    today = datetime.now().strftime("%Y-%m-%d")
    evs = p.qzone_comment_seen.fav_events_on(today, "20000")
    assert any(e["kind"] == "LIKE" and "小红" in e["text"] and "我的晚间思绪" in e["text"] for e in evs)
    # 过旧事件仍被登记(is_new 已 False,下轮判重不重扫)
    assert p.qzone_like_seen.is_new("30000_10000_cccc3333", liker_uin="30000", target_tid="ownfeed2") is False

    # 去重:释放 awaiting 后重扫,同 like_key 不再注入(取数发生=判重生效)
    p.qzone_injector.on_turn_complete(_time.monotonic())
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    assert p.qzone_client.like_fetches == 2


def test_notify_scan_source_c_like_auth_error_keeps_source_a_notifications(tmp_path):
    """源C 自愈链:登录态失效作废 cookie 但不 return——源A 已得通知照常入队注入
    (源C 仅是增量来源,同源B 纪律)。"""

    import time as _time

    from catsitate_core.qzone.wire import CommentItem

    comments = {"feeda": [CommentItem(
        comment_tid="ca1", uin="20000", nickname="小红", content="好友评论",
        create_time=str(int(_time.time())),
    )]}

    class _AuthFailLikeClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return comments, {"feeda": "说说正文"}

        async def get_like_events(self, *, count=30):
            del count
            raise QzoneAuthError("空间登录态失效(源C)")

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_client = _AuthFailLikeClient()
    asyncio.run(p._qzone_notify_scan())
    # 源A 通知未丢:正常注入 1 条
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_info"]["user_info"]["user_id"] == "20000"
    # 自愈链:cookie 已作废(下轮重取)+ 失效告警
    assert p.qzone_cookie.invalidate_calls == 1
    assert any(level == "warning" and "通知轮询源C" in " ".join(str(x) for x in a)
               for level, a in p.logs)


def test_notify_retry_backoff_source_c_like_gives_up_without_misleading_retry(tmp_path):
    """源C 无重试通道(M3 修正 I-4):赞事件通知注入被拒不走 note_retry/revert
    空转(去重在 qzone_likes 表,无 pending_retry 通道,revert 作用于
    qzone_comments 为无效更新),显式 warning 放弃——不误报「待下轮重试」;
    源A/B 评论通知的回退重试语义不变(对照)。"""

    import time as _time

    p = _make_plugin(tmp_path)
    like_feed = FeedItem(
        tid="notify_like_20000_10000_ab12cd34", abstime="", uin="20000",
        nickname="小红", content="赞了你的说说", source="notify",
        dedup_key="20000_10000_ab12cd34",
    )
    p._qzone_notify_retry_backoff(like_feed)
    assert any(
        level == "warning" and "源C 赞事件通知被拒,放弃(源C 无重试通道)" in str(a[0])
        for level, a in p.logs
    )
    # 不误报待重试;源C 键在 qzone_comments 无登记(note_retry/revert 未空转写状态)
    assert not any("待下轮重试" in str(a[0]) for _, a in p.logs)
    rows = p.store.query(
        "SELECT COUNT(*) FROM qzone_comments WHERE comment_key = '20000_10000_ab12cd34'"
    )
    assert rows[0][0] == 0

    # 对照:源A 评论通知(键含冒号形态)被拒仍走软回退重试
    p.logs.clear()
    key = "feedX:cX:20000"
    assert p.qzone_comment_seen.is_new(key) is True
    p._qzone_notify_retry_backoff(_notify_feed(key))
    assert any(
        level == "info" and "待下轮重试" in str(a[0]) for level, a in p.logs
    )
    assert p.qzone_comment_seen.is_new(key) is True  # 键已回退,下轮重新发现


def test_notify_scan_source_c_observation_line_and_drift_warn_once(tmp_path):
    """源C 解析观测线(M3 修正 I-5):每轮取数成功后 info「源C 赞事件解析 {N}
    条」;连续 3 轮 N==0 且模块可用(取数成功)打一次锚点漂移 warning(去重
    标记,后续空轮不重复告警);恢复有事件后计数与标记复位,再次连续 3 轮空
    才会告警第二次(新漂移段落)。"""

    import time as _time

    class _MutableLikeClient(_StubUnifiedClient):
        """源C 输入桩:赞事件列表可变(观测线的空/非空轮切换)。"""

        def __init__(self):
            super().__init__([])
            self.events: list = []

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

        async def get_like_events(self, *, count=30):
            del count
            return list(self.events)

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 观测线状态实例级重置(类属性共享可变态,防其他测试的扫描轮数泄漏)
    p._qzone_sourcec_empty_rounds = 0
    p._qzone_sourcec_drift_warned = False
    client = _MutableLikeClient()
    p.qzone_client = client

    def _drift_warnings() -> int:
        return sum(
            1 for level, a in p.logs
            if level == "warning" and "锚点可能漂移" in str(a[0])
        )

    def _parse_infos(n: int) -> int:
        # 桩 logger 记录 printf 原参:模板串+计数逐参比对
        return sum(
            1 for level, a in p.logs
            if level == "info" and str(a[0]) == "源C 赞事件解析 %d 条" and a[1:] == [n]
        )

    for _ in range(4):  # 连续 4 轮空:第 3 轮触发一次,第 4 轮不重复
        asyncio.run(p._qzone_notify_scan())
    assert _parse_infos(0) == 4  # 观测线每轮都有
    assert _drift_warnings() == 1  # warn-once

    client.events = [LikeEvent(
        like_key="20000_10000_aaaa1111", liker_uin="20000", liker_nickname="小红",
        owner_uin=BOT_UIN, target_tid="ownfeed1", create_time=str(int(_time.time())),
    )]
    asyncio.run(p._qzone_notify_scan())
    assert _parse_infos(1) == 1
    p.qzone_injector.on_turn_complete(_time.monotonic())  # 释放 awaiting,后续轮可继续
    assert _drift_warnings() == 1  # 恢复非空不告警

    client.events = []
    for _ in range(3):
        asyncio.run(p._qzone_notify_scan())
    assert _drift_warnings() == 2  # 复位后的新漂移段落:第二次告警


def test_qzone_block_virtual_stream_state_only(tmp_path):
    """注入块去重(可读性优化 2026-09-01):场景全文已由 apply_scene_surgery 进
    system 段(场景替换),虚拟流注入块只保留动态状态——不再重复拼场景文案。"""

    p = _make_plugin(tmp_path)
    p._qzone_session_ids = {"qz_stream"}
    key, text = p._qzone_block("qz_stream")
    assert key.startswith("qzone:v:")
    assert text.startswith("[空间] ")
    assert "暂无新动态" in text  # 无 awaiting/队列时的动态状态
    assert "刷QQ空间" not in text and "qzone_comment" not in text  # 场景文案不重复注入
    # 真实聊天摘要分支保持不变:无见闻 → None
    key2 = p._qzone_block("real_stream")
    assert key2 is None


def test_qzone_block_real_chat_summary_narrative_format(tmp_path):
    """M3 表达:真实聊天见闻摘要用「昵称发了「摘要」」叙事格式——与浏览动态的
    自然文本一致(比「昵称:摘要」键值对更像转述见闻);摘要截 20 字,纯图说说
    (无摘要)以「图片」占位;键仍按 tid 集合去重(内容不变即缓存复用)。"""

    p = _make_plugin(tmp_path)
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_seen.mark_queued("sumtid1", abstime="1", author_uin="10001",
                             summary="今天去公园散步拍了好多照片晚霞很好看心情也很好", author_nickname="小明")
    p.qzone_seen.mark_queued("sumtid2", abstime="1", author_uin="10002",
                             summary="", author_nickname="")  # 纯图说说:无摘要无昵称
    p.qzone_seen.mark_seen("sumtid1", now_iso)
    p.qzone_seen.mark_seen("sumtid2", now_iso)
    key, text = p._qzone_block("real_stream")
    assert key == "qzone:s:sumtid1|sumtid2"
    # 叙事格式:昵称发了「摘要前20字」(超长截断);缺昵称回退QQ号;空摘要以「图片」占位
    assert text == "[空间] 近期刷到: 小明发了「今天去公园散步拍了好多照片晚霞很好看心情...」;10002发了「图片」"


# ---- M3 Task9:见闻系统(窗口结束旁路 LLM 摘要,注入真实聊天) ----


def test_digest_generated_on_window_end(tmp_path):
    """M3 见闻系统:read_qzone 窗口结束旁路 LLM 把当日浏览+互动摘要为「空间见闻」
    持久化(qzone_digest.json,键 date/text)——虚拟流 receive-only 无发言投递,
    主程序记忆层不会为它产出摘要,由插件在窗口边界自行生成;生成失败保留上一份。"""

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 当日素材两源:近 1 天已注入浏览(seen)+ 当日互动事件(fav_events)
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_seen.mark_queued("dtid1", abstime="1", author_uin="10001",
                             summary="发了新猫图", author_nickname="小明")
    p.qzone_seen.mark_seen("dtid1", now_iso)
    p.qzone_comment_seen.fav_event("10001", "COMMENT", "小明 评论了你的说说:好看")

    async def llm(messages, model, module, timeout_ms=None):
        return {"success": True, "response": "今天看到小明发的新猫图,还收到两条评论。"}

    p._side_llm_call = llm
    # 人设缓存预置(离线桩无 ctx.config,不走异常兜底路径;实例级覆盖防测试间泄漏)
    p._persona_cache = "猫耳少女"
    p._style_cache = ""
    asyncio.run(p._qzone_generate_digest())
    data = p._qzone_digest_snapshot.load()
    assert data.get("date") == datetime.now().strftime("%Y-%m-%d")
    assert data.get("text", "").startswith("今天看到小明")
    # 窗口结束触发(源码断言):_qzone_poll_feeds 的 window_ended 之后派发生成任务
    import inspect

    import plugin as plugin_mod

    src = inspect.getsource(plugin_mod)
    assert "_spawn_background_task(self._qzone_generate_digest())" in src


def test_qzone_block_prefers_today_digest(tmp_path):
    """M3 见闻系统:_qzone_block 真实流分支优先输出当日「[空间见闻]」(LLM 摘要的
    当日空间印象);无当日见闻(日期过期)回退既有「近期刷到」路径不变。"""

    p = _make_plugin(tmp_path)
    p._qzone_digest_snapshot = JsonSnapshot(tmp_path / "qzone_digest.json")
    p._qzone_digest_snapshot.save({
        "date": datetime.now().strftime("%Y-%m-%d"), "text": "见闻正文",
    })
    qz = p._qzone_block(stream_id="非虚拟流id")
    assert qz and qz[1].startswith("[空间见闻] 见闻正文")
    # 回退分支:快照日期过期(昨日的旧见闻)→ 既有近期刷到叙事不变
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    p._qzone_digest_snapshot.save({"date": yesterday, "text": "昨天的旧见闻"})
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_seen.mark_queued("sumtid1", abstime="1", author_uin="10001",
                             summary="今天去公园散步", author_nickname="小明")
    p.qzone_seen.mark_seen("sumtid1", now_iso)
    qz2 = p._qzone_block(stream_id="非虚拟流id")
    assert qz2 and qz2[1].startswith("[空间] 近期刷到: ")


# ---- 终审修复波 I1/I2/I3:组合层行为测试 ----


class _StubLikeClient:
    """点赞路径桩:记录 do_like 调用,恒成功。"""

    def __init__(self):
        self.like_calls = []

    async def do_like(self, *, fid, target_qq):
        self.like_calls.append((fid, target_qq))
        return True


def test_qzone_like_rejects_malformed_notify_without_origin(tmp_path):
    """畸形通知防护(承终审 I1):awaiting 是无 origin_tid 的 P1 通知(合成 tid)
    时 qzone_like 缺省路径显式拒绝——不向 qzone 发畸形点赞请求(零写调用);
    有 origin_tid 的通知则点赞其原说说(见 test_qzone_like_via_feed_id_and_notify_origin)。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.qzone_client = _StubLikeClient()
    p._qzone_session_ids.add("s1")
    # 通知项经真实入队→弹出→注入链进入 awaiting(source="notify",无 origin_tid 畸形形态)
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue_priority([FeedItem(
        tid="notify_comment_feed1_c1", abstime="1750000000", uin="20000",
        nickname="小红", content="(通知) 小红 评论了你的说说", source="notify",
    )])
    popped = p.qzone_injector.next_to_inject(_time.monotonic())
    assert popped is not None and popped.source == "notify"
    p.qzone_injector.mark_injected(popped.tid, _time.monotonic())

    res = asyncio.run(p.qzone_like(stream_id="s1"))
    assert res == "当前是互动通知且缺少原说说信息,无法点赞。"
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


def test_poll_tick_keeps_p1_notifications_at_window_end(tmp_path):
    """M3-r2 通知推送语义:窗口结束只清浏览队列(P2),通知队列(P1)保留等待
    注入条件(bot 醒着/泵空闲)——不再有「未注入通知被清空」告警分支。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    # 日程窗口已结束(1 小时前收尾)→ poll_tick 走非窗口收泵分支
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "read_qzone": True,
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
    assert p.qzone_injector.stats()["p1_queued"] == 2  # 通知队列保留
    assert any(
        level == "info" and "QQ空间浏览窗口结束,浏览队列回退未读" in str(a[0]) for level, a in p.logs
    )
    assert not any("未注入通知被清空" in str(a[0]) for _, a in p.logs)  # 告警分支已删


# ---- 深度审查修复波(F1/F2/F3/F5):组合层行为测试 ----


def test_qzone_like_auth_error_invalidates_cookie(tmp_path):
    """AuthError 自愈链(点赞路径):登录态失效→cookie 作废+明确提示,零成功记账。"""

    class _AuthFailLikeClient:
        async def do_like(self, *, fid, target_qq):
            raise QzoneAuthError("登录态失效(code=-3000)")

    p = _make_plugin(tmp_path)
    p._qzone_session_ids.add("s1")
    _register_feed(p, tid="likefail1", owner="10001")
    p.qzone_client = _AuthFailLikeClient()
    res = asyncio.run(p.qzone_like(feed_id="likefail1", stream_id="s1"))
    assert res == "点赞失败:登录态失效已重置,稍后再试。"
    assert p.qzone_cookie.invalidate_calls == 1
    rows = p.qzone_comment_seen.store.query("SELECT 1 FROM qzone_fav_events")
    assert rows == []  # 失败不记好感度事件


def test_pump_timeout_force_release_keeps_registry(tmp_path):
    """超时强制推进(工具驱动):awaiting 超时释放后 registry 上下文保留——
    模型在后续轮次仍可对已注入说说调用工具(TTL 48h 兜底),无「清意图」概念。"""

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
    p._qzone_registry.register(FeedContext(tid="f9", owner_uin="10001", owner_nickname="小明"))

    asyncio.run(p._qzone_pump())
    assert p.qzone_injector.awaiting_tid == ""  # 已强制释放
    assert p._qzone_registry.resolve("f9") is not None  # 上下文保留(工具仍可解析)
    assert any(
        level == "warning" and "强制推进" in str(a[0])
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


def test_on_load_whitelist_warning_covers_view_friend_feeds(tmp_path):
    """白名单语义告警(2026-09-02 全域化后):缺 view_friend_feeds 仍告警
    (虚拟流无法查看好友说说);残留 qzone_*/reply 项提示可移除(不再消费);
    齐全新语义白名单零告警。"""

    p = _make_plugin(tmp_path)
    p.config.qzone.tool_whitelist = ["wait"]
    p._warn_qzone_tool_whitelist()
    assert any(
        level == "warning" and "view_friend_feeds" in " ".join(str(x) for x in a)
        for level, a in p.logs
    )

    # 齐全新语义白名单(无 qzone_*/reply 项):零告警
    p.logs.clear()
    p.config.qzone.tool_whitelist = list(CatsitateConfig().qzone.tool_whitelist)
    p._warn_qzone_tool_whitelist()
    assert not any(level == "warning" for level, a in p.logs)

    # 旧配置残留(qzone_* 与废弃 reply 混入):一并提示可移除
    p.config.qzone.tool_whitelist += ["qzone_like", "reply"]
    p._warn_qzone_tool_whitelist()
    assert any(
        level == "warning" and "不再由白名单管理" in str(a[0]) for level, a in p.logs
    )


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
        "activity": "逛空间", "plan_speak": False, "topic": "", "read_qzone": True,
    }]}


def test_poll_tick_returns_before_long_io_completes(tmp_path):
    """深度审查 A-2 行为:tick 派发后台拉取后立即返回——统一时间线还在拉(长 IO
    未完成)时 tick 协程已 done,调度器不被阻塞。"""

    started, release = asyncio.Event(), asyncio.Event()

    class _SlowDiscoveryClient:
        async def get_unified_timeline(self, *, count=20, begin=0):
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
        async def get_unified_timeline(self, *, count=20, begin=0):
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


def test_pump_registers_content_summary_and_recent_comments(tmp_path):
    """M3-r2 Task 6 表达生成层素材:泵登记 FeedContext 带 content_summary(说说
    正文前 100 字)与 recent_comments(get_user_feeds 合并的「昵称:内容」摘要)
    ——qzone_comment 生成正文时的场景素材;纯图说说以「(无文字)」占位。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_seen.mark_queued("t_cs", abstime="1750000000", author_uin="10001", summary="正文")
    p.qzone_injector.enqueue([FeedItem(
        tid="t_cs", abstime="1750000000", uin="10001", nickname="小明", content="今天去了海边玩,晒黑了",
        comments=["小红:羡慕", "小刚:哈哈"],
    )])
    p.qzone_seen.mark_queued("t_pic", abstime="1750000100", author_uin="10002", summary="图片")
    p.qzone_injector.enqueue([FeedItem(
        tid="t_pic", abstime="1750000100", uin="10002", nickname="小红", content="",  # 纯图说说
    )])
    asyncio.run(p._qzone_pump())  # 注入较新的 t_pic(队列按时间序)
    p.qzone_injector.on_turn_complete(_time.monotonic())  # 释放 awaiting(一动态一轮)
    asyncio.run(p._qzone_pump())  # 注入 t_cs

    ctx = p._qzone_registry.resolve("t_cs")
    assert ctx is not None
    assert ctx.content_summary == "今天去了海边玩,晒黑了"
    assert ctx.recent_comments == ["小红:羡慕", "小刚:哈哈"]
    ctx_pic = p._qzone_registry.resolve("t_pic")
    assert ctx_pic is not None and ctx_pic.content_summary == "(无文字)"  # 空正文占位


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

    # ①发现层 2 次调用(Task5 翻页:首页有新动态→翻 1 页确认无更早积压后止步);
    # ②充实层只对 uin=10001(唯一含新 tid 的作者)调用
    assert p.qzone_client.discovery_calls == 2
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
        async def get_unified_timeline(self, *, count=20, begin=0):
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


# ---- M3 终审修复波:I1(浏览流bot自我排除)+ I2(发现层登录态失效行为) ----


class _StubCookie:
    """cookie 管理桩:只记录 invalidate 调用次数(登录态自愈链断言用)。"""

    def __init__(self):
        self.invalidate_calls = 0

    def invalidate(self):
        self.invalidate_calls += 1


def test_poll_feeds_excludes_bot_own_feed_from_enrichment(tmp_path, monkeypatch):
    """终审 I1:发现层过滤须排除 bot 自己的说说(自己动态不充实不注入)——
    源B侧交叉已有同款排除(1211 行 d.uin != bot_uin),浏览流对齐;否则 bot 自己
    发的说说会被当「新动态」充实注入,形成自我围观。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()

    enrich_calls: list = []

    class _StubEnrichClient(_StubUnifiedClient):
        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            enrich_calls.append((target_uin, nickname, num))
            return [FeedItem(tid="friendtid", abstime="1750000200", uin="10001",
                             nickname="小明", content="好友新动态")]

    # 发现层:bot 自己的新说说(mytid)+好友新说说(friendtid)——只有后者应被充实注入
    p.qzone_client = _StubEnrichClient([
        FeedDiscovery(tid="mytid", uin=BOT_UIN, nickname="我",
                      abstime="1750000300", appid=311),
        FeedDiscovery(tid="friendtid", uin="10001", nickname="小明",
                      abstime="1750000200", appid=311),
    ])
    asyncio.run(p._qzone_poll_feeds())

    # 充实层只对好友调用(bot 自己零充实);好友动态恰好注入一次,bot 动态不注入
    assert enrich_calls == [("10001", "小明", 3)]
    assert [c[1]["message_id"] for c in p._ctx.gateway.calls] == ["qzone_friendtid_1"]
    # bot 自己的 tid 未登记未入队(不占判重键);好友 tid 已随注入标记已见
    assert p.qzone_seen.is_new_candidate("mytid") is True
    states = dict(p.store.query("SELECT tid, state FROM qzone_feeds"))
    assert states == {"friendtid": "seen"}


def test_poll_feeds_auth_error_invalidates_cookie_and_skips_legacy(tmp_path, monkeypatch):
    """终审 I2-①:发现层抛 QzoneAuthError → cookie invalidate 被调 + 不回退
    legacy(cookie 失效对两路径同源,回退只会重复失败多打一轮 API)——
    _qzone_poll_feeds_legacy 零调用,显式告警。"""

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    cookie = _StubCookie()
    p.qzone_cookie = cookie

    class _AuthFailClient:
        async def get_unified_timeline(self, *, count=20, begin=0):
            del count
            raise QzoneAuthError("登录态失效(code=-3000)")

    p.qzone_client = _AuthFailClient()

    legacy_calls: list = []

    async def _legacy_probe():
        legacy_calls.append(1)

    monkeypatch.setattr(p, "_qzone_poll_feeds_legacy", _legacy_probe)
    asyncio.run(p._qzone_poll_feeds())

    assert cookie.invalidate_calls == 1  # cookie 已作废(下轮重取)
    assert legacy_calls == []  # 不回退 legacy 旧路径
    assert p._ctx.gateway.calls == []  # 本轮零注入
    assert any(
        level == "warning" and "登录态失效" in str(a[0]) and "cookie 已作废" in str(a[0])
        for level, a in p.logs
    )


def test_notify_scan_source_b_discovery_failure_does_not_block_source_a(tmp_path, monkeypatch):
    """终审 I2-②:源B发现层抛普通 Exception → 源A已得通知照常入队注入(源B仅是
    增量来源,失败不阻断源A),并显式告警源B跳过。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # bot 曾在好友 30000 说说下评论——若源B不失败,会再多一次发现层后的拉取路径;
    # 本测试断言失败路径:发现层抛错后本轮源B静默跳过,不影响源A通知
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "我的评论", fresh)

    class _SourceBFailClient(_StubUnifiedClient):
        """源A返回一条好友评论;源B发现层抛 RuntimeError。"""

        def __init__(self):
            super().__init__([])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {"feed1": [
                CommentItem(comment_tid="c1", uin="20000", nickname="小红",
                            content="好友评论", create_time=str(int(_time.time()))),
            ]}, {"feed1": "今天的心情"}

        async def get_unified_timeline(self, *, count=20, begin=0):
            del count
            raise RuntimeError("空间统一时间线请求失败: HTTP 502")

    p.qzone_client = _SourceBFailClient()
    asyncio.run(p._qzone_notify_scan())

    # 源A通知照常入队注入恰一次;registry 登记对位源A(owner=bot 自己的说说)
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_info"]["user_info"]["user_id"] == "20000"
    ctx = p._qzone_registry.resolve("feed1")
    assert ctx is not None and ctx.owner_uin == BOT_UIN and ctx.comment_tid == "c1"
    # 源B失败显式告警(不静默)
    assert any(
        level == "exception" and "源B发现层失败" in str(a[0]) for level, a in p.logs
    )


def test_notify_scan_source_b_auth_error_invalidates_cookie_and_keeps_source_a(tmp_path, monkeypatch):
    """M3 终审 Minor1:源B发现层抛 QzoneAuthError → cookie invalidate 被调且
    显式告警,但不 return——源A已得通知照常入队注入(源B增量终止,源A不受阻)。"""

    import time as _time

    _patch_sleep(monkeypatch, [])
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    cookie = _StubCookie()
    p.qzone_cookie = cookie
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "我的评论", fresh)  # 名单非空→进源B

    class _SourceBAuthFailClient(_StubUnifiedClient):
        """源A返回一条好友评论;源B发现层抛 QzoneAuthError。"""

        def __init__(self):
            super().__init__([])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {"feed1": [
                CommentItem(comment_tid="c1", uin="20000", nickname="小红",
                            content="好友评论", create_time=str(int(_time.time()))),
            ]}, {"feed1": "今天的心情"}

        async def get_unified_timeline(self, *, count=20, begin=0):
            del count
            raise QzoneAuthError("登录态失效(code=-3000)")

    p.qzone_client = _SourceBAuthFailClient()
    asyncio.run(p._qzone_notify_scan())

    assert cookie.invalidate_calls == 1  # cookie 已作废(下轮重取)
    assert len(p._ctx.gateway.calls) == 1  # 源A通知照常注入(不因登录态失效被阻断)
    ctx = p._qzone_registry.resolve("feed1")
    assert ctx is not None and ctx.owner_uin == BOT_UIN  # 源A登记对位(主人=bot)
    assert any(
        level == "warning" and "源B登录态失效" in str(a[0]) and "cookie 已作废" in str(a[0])
        for level, a in p.logs
    )


def test_notify_scan_source_b_skips_discovery_when_no_commented_friends(tmp_path, monkeypatch):
    """M3 终审 Minor2:bot 近期未在任何好友说说下评论(名单空)→ 跳过源B发现层
    调用(本地反查先行省 API:发现层 feeds3_html_more 一次 HTTP 省掉)。"""

    _patch_sleep(monkeypatch, [])
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 不登记 note_bot_comment:bot_commented_friends 名单为空

    class _ProbeClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}

    p.qzone_client = _ProbeClient()
    asyncio.run(p._qzone_notify_scan())
    assert p.qzone_client.discovery_calls == 0  # 名单空:零发现层调用


# ---- M3-r2 Task5:发现层翻页 + 拉取数量配置 ----


def _disc(tid, uin, abstime, appid=311):
    """发现层条目速造(翻页测试用;默认说说类动态)。"""
    return FeedDiscovery(tid=tid, uin=uin, nickname=f"好友{uin}", abstime=abstime, appid=appid)


def test_discovery_pagination_stops_on_all_seen(tmp_path):
    """Task5:本页无新说说 tid 即止步——稳态(无新动态)恒 1 次调用不翻页;
    页大小取自 discovery_count(默认 50)。窗口预置开启,绕开窗口开始的
    revert_pending 回收(queued 判重基准保持稳定,同 T3 测试注释口径)。"""

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    p.qzone_injector.window_started()  # 预置窗口:mark_queued 登记不被窗口开始回收
    calls = []

    async def page(*, count, begin=0):
        calls.append((count, begin))
        if begin == 0:
            return [_disc("t1", "100", "300"), _disc("t2", "101", "200")]  # t1/t2 已登记
        return [_disc("t3", "102", "100")]

    p.qzone_client.get_unified_timeline = page
    p.qzone_seen.mark_queued("t1", abstime="300", author_uin="100", summary="a")
    p.qzone_seen.mark_queued("t2", abstime="200", author_uin="101", summary="b")
    asyncio.run(p._qzone_poll_feeds())
    # 第 1 页全部已见(更早页只会更旧),翻页止步:恰 1 次调用,页大小=配置值
    assert calls == [(50, 0)]


def test_discovery_pagination_stops_when_second_page_all_seen(tmp_path, monkeypatch):
    """Task5:第 1 页含新动态 → 翻第 2 页;第 2 页全旧 → 止步(不再第 3 页)。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    p.qzone_injector.window_started()
    p.qzone_seen.mark_queued("t2", abstime="200", author_uin="101", summary="旧")
    p.qzone_seen.mark_queued("t3", abstime="100", author_uin="102", summary="旧")
    calls = []

    class _PagedClient(_StubUnifiedClient):
        async def get_unified_timeline(self, *, count=20, begin=0):
            calls.append(begin)
            if begin == 0:
                return [_disc("t1", "100", "300"), _disc("t2", "101", "200")]
            return [_disc("t3", "102", "100")]  # 第 2 页全旧

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            return []  # 充实页空:聚焦翻页调用序列

    p.qzone_client = _PagedClient([])
    asyncio.run(p._qzone_poll_feeds())
    assert calls == [0, 50]  # 第 2 页无新说说即止步


def test_discovery_pagination_fetches_backlog_until_max_pages(tmp_path, monkeypatch):
    """Task5:长时间离线积压补全——每页都有新说说时逐页翻至 discovery_max_pages
    上限(默认 3 页);begin=页序×页大小,页大小=discovery_count。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    p.qzone_injector.window_started()
    calls = []

    class _BacklogClient(_StubUnifiedClient):
        async def get_unified_timeline(self, *, count=20, begin=0):
            calls.append((count, begin))
            # 每页一条不同作者的新动态:has_new 恒真,翻页直到配置上限
            return [_disc(f"t{begin}", f"1{begin:03d}", str(300 - begin))]

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            return []  # 充实页空:不引入注入链

    p.qzone_client = _BacklogClient([])
    asyncio.run(p._qzone_poll_feeds())
    # 默认上限 3 页止步(即便第 3 页仍有新动态);两页积压场景由上一用例覆盖
    assert calls == [(50, 0), (50, 50), (50, 100)]


def test_discovery_pagination_respects_configured_page_size(tmp_path, monkeypatch):
    """Task5:discovery_count/discovery_max_pages 可配置——页大小与翻页深度随配置走。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p.config.qzone.discovery_count = 7
    p.config.qzone.discovery_max_pages = 2
    p._schedule_data = _active_qzone_schedule()
    p.qzone_injector.window_started()
    calls = []

    class _PagedClient(_StubUnifiedClient):
        async def get_unified_timeline(self, *, count=20, begin=0):
            calls.append((count, begin))
            return [_disc(f"t{begin}", f"1{begin:03d}", "300")]  # 恒有新动态

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            return []

    p.qzone_client = _PagedClient([])
    asyncio.run(p._qzone_poll_feeds())
    assert calls == [(7, 0), (7, 7)]  # 页大小 7、上限 2 页


# ---- M3-r2 Task 7:view_friend_feeds 全域查看工具 + inspect_image hash 路径 ----


def test_view_friend_feeds_returns_media_dict(tmp_path):
    """view_friend_feeds 成功路径(全域查看工具,任何聊天流可用):拉好友说说→
    dict 回执(content 文本摘要+content_items 图片媒体项,宿主按 tool result
    media 入 Images 表供 inspect_image hash 路径反查);图标注为 sha256 前 8 位
    (inspect_image 的 image_hash 前缀即来源于此);成功即 registry 登记
    (content_summary/recent_comments=表达生成层场景素材)。"""

    p = _make_plugin(tmp_path)
    feed = FeedItem(tid="tid1", abstime="1750000000", uin="100", nickname="小明",
                    content="今天天气好", image_urls=["http://img.qpic.cn/a.jpg"],
                    comments=["小红:不错"])
    feed_calls: list = []

    async def get_user_feeds(*, target_uin, nickname, num=3):
        feed_calls.append((target_uin, nickname, num))
        return [feed]

    downloads: list = []

    async def download(url):
        downloads.append(url)
        return b"fakejpeg"

    p.qzone_client.get_user_feeds = get_user_feeds
    p.qzone_client.download_image = download

    result = asyncio.run(p.view_friend_feeds(qq="100", count=2))
    assert isinstance(result, dict)
    # 拉取参数对位:nickname 回退 QQ 号(全域工具无昵称上下文),num=请求条数
    assert feed_calls == [("100", "100", 2)]
    assert downloads == ["http://img.qpic.cn/a.jpg"]  # 每图恰下载一次
    # content 摘要(2026-09-02 可读性格式):头部同主点名一次+条目编号+
    # 发布时间前缀(浏览注入同款相对时间)+正文,图标注(sha256 前 8 位,
    # inspect_image 前缀口径不变)/说说ID 锚契约不变
    content = result["content"]
    assert "小明(QQ:100)最近的说说,共 1 条:" in content
    assert "〔1〕" in content and "今天天气好" in content and "说说ID=tid1" in content
    assert "(" in content.split("〔1〕", 1)[1].split("今天天气好", 1)[0]  # 时间前缀在正文前
    expected_tag = f"图1({hashlib.sha256(b'fakejpeg').hexdigest()[:8]})"
    assert expected_tag in content and "图2" not in content
    # 媒体项:base64 图 + mime(宿主 _parse_tool_content_items 消费形态)
    assert result["content_items"] == [{
        "content_type": "image",
        "data": base64.b64encode(b"fakejpeg").decode("ascii"),
        "mime_type": "image/jpeg",
    }]
    # registry 登记:owner=好友,素材字段=正文前 100 字+近期评论(qzone_comment 素材)
    resolved = p._qzone_registry.resolve("tid1")
    assert resolved is not None and resolved.owner_uin == "100" and resolved.owner_nickname == "小明"
    assert resolved.content_summary == "今天天气好" and resolved.recent_comments == ["小红:不错"]
    # count 上限 10(防单次拉爆);非法 QQ 号显式拒绝(零拉取)
    asyncio.run(p.view_friend_feeds(qq="100", count=99))
    assert feed_calls[-1][2] == 10
    assert asyncio.run(p.view_friend_feeds(qq="notnum")) == "请提供好友的QQ号(纯数字)。"
    assert len(feed_calls) == 2


def test_inspect_image_by_hash_prefix_skips_message_search(tmp_path, monkeypatch):
    """inspect_image hash 路径(M3-r2 Task 7):image_hash 给定时跳过消息搜索
    (_fetch_recent 零调用——主断言,hash 覆盖 view_friend_feeds 等非消息来源经
    tool result media 入库的图片);database.get 拉表(single_result=False)+
    插件侧前缀过滤,命中唯一复用 full_path→读文件→relook 链(旁路 LLM 收到补图)。"""

    import plugin as plugin_mod

    p = _make_tool_plugin(tmp_path)
    p.config.plugin.enabled = True  # inspect_image 开关门禁(默认关,工具直调测试放开)
    full_hash = "abcd1234" + "0" * 56
    db_calls: list = []

    async def cap(capability, **kwargs):
        if capability == "database.get":
            db_calls.append(kwargs)
            return [
                {"image_hash": "ffff0000" + "0" * 56, "full_path": "other.jpg"},  # 非命中行
                {"image_hash": full_hash, "full_path": "x.jpg"},
            ]
        return {}

    p._ctx.call_capability = cap
    (tmp_path / "x.jpg").write_bytes(b"img")
    monkeypatch.setattr(plugin_mod, "Path", lambda s: tmp_path)  # /MaiMBot 根 → 测试目录
    fetch_calls: list = []

    async def _fetch_probe(stream_id, limit):
        fetch_calls.append((stream_id, limit))
        return []

    p._fetch_recent = _fetch_probe

    result = asyncio.run(p.inspect_image(image_hash="abcd1234", question="图里是什么"))
    assert fetch_calls == []  # 主断言:hash 路径不做消息搜索
    assert result == "ok"  # 旁路 LLM 桩回显(relook 链完整走通,非取图失败串)
    # 拉表形态:Images 表 + single_result=False(capability 无 LIKE,插件侧前缀过滤)
    assert len(db_calls) == 1
    assert db_calls[0]["model_name"] == "Images" and db_calls[0]["single_result"] is False


def test_inspect_image_hash_prefix_zero_and_multi_hit_errors(tmp_path, monkeypatch):
    """hash 路径不变量(错误显式暴露):零命中/多命中/返回形态异常都显式报错
    不猜,且一律不回退消息搜索(_fetch_recent 零调用——hash 语义失败不是消息
    路径的失败,静默换路会掩盖真实原因)。"""

    import plugin as plugin_mod

    p = _make_tool_plugin(tmp_path)
    p.config.plugin.enabled = True  # inspect_image 开关门禁(默认关,工具直调测试放开)
    rows: list = []

    async def cap(capability, **kwargs):
        if capability == "database.get":
            return list(rows)
        return {}

    p._ctx.call_capability = cap
    monkeypatch.setattr(plugin_mod, "Path", lambda s: tmp_path)
    fetch_calls: list = []

    async def _fetch_probe(stream_id, limit):
        fetch_calls.append((stream_id, limit))
        return []

    p._fetch_recent = _fetch_probe

    # 零命中:表里无该前缀的图
    rows = [{"image_hash": "ffff" + "0" * 60, "full_path": "other.jpg"}]
    res0 = asyncio.run(p.inspect_image(image_hash="abcd1234", question="?"))
    assert "未找到 hash 前缀 abcd1234" in res0
    # 多命中:同前缀两张图 → 歧义报错并列候选(提示加长前缀),不任选其一
    rows = [
        {"image_hash": "abcd1234" + "1" * 56, "full_path": "a.jpg"},
        {"image_hash": "abcd1234" + "2" * 56, "full_path": "b.jpg"},
    ]
    res2 = asyncio.run(p.inspect_image(image_hash="abcd1234", question="?"))
    assert "歧义" in res2 and "命中 2 张" in res2 and "abcd1234" in res2
    # 返回形态异常(错误 dict 而非列表):显式报错,不静默当空表
    async def cap_bad(capability, **kwargs):
        return {"success": False, "error": "未找到数据模型: Images"}

    p._ctx.call_capability = cap_bad
    res_bad = asyncio.run(p.inspect_image(image_hash="abcd1234", question="?"))
    assert "异常形态" in res_bad
    assert fetch_calls == []  # 三种失败形态都不触发消息搜索


# ---- M3-r2 Task 8:说说发布主动触发(分窗口形态)与冷启动种子自举 ----


class _StubProactive:
    """proactive.trigger 桩:记录调用,默认恒成功(各测试按需替换)。"""

    def __init__(self):
        self.calls = []

    async def trigger(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True}


class _StubMaisaka:
    """maisaka 桩:仅暴露 proactive.trigger(_window_trigger 同款调用面)。"""

    def __init__(self):
        self.proactive = _StubProactive()


def _make_plugin_with_qzone(tmp_path, window=None):
    """发布触发测试装配:_make_plugin 基础上补 maisaka 桩,并铺设覆盖当前
    时刻的日程窗口(window 缺省为 read_qzone 浏览窗口,start/end 可显式传
    以构造多窗口邻接/切换场景)。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    win = dict(window or {"kind": "daily", "read_qzone": True})
    win.setdefault("kind", "daily")
    win.setdefault("start", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
    win.setdefault("end", (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
    win.setdefault("activity", "逛空间")
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [win]}
    p._ctx.maisaka = _StubMaisaka()
    return p, p._ctx


@pytest.mark.asyncio
async def test_send_trigger_sendonly_window_fires_immediately(tmp_path):
    """仅 send_qzone 窗口:窗口开始即触发,不等待浏览;仅 send 窗口不做
    发现/充实拉取(read_qzone=False 不浏览),tick 早退。"""

    p, ctx = _make_plugin_with_qzone(
        tmp_path, window={"kind": "daily", "read_qzone": False, "send_qzone": True})
    fired = []

    async def trigger(**kwargs):
        fired.append(kwargs)
        return {"success": True}

    ctx.maisaka.proactive.trigger = trigger
    await p._qzone_poll_feeds()  # 首个 tick:窗口开始→武装→立即触发(浏览轮询照常空转)
    await asyncio.sleep(0)  # 武装派发的后台触发任务在下一调度槽执行
    assert len(fired) == 1 and "qzone_post" in fired[0]["intent"]
    assert fired[0]["reason"] == "日程窗口send_qzone"
    # 仅 send 窗口不浏览:发现层零调用,注入泵窗口未激活
    assert p.qzone_client.discovery_calls == 0
    assert p.qzone_injector.window_active is False


@pytest.mark.asyncio
async def test_send_trigger_cold_start_seed_retry(tmp_path):
    """会话不存在→种子自举→重试一次成功。"""

    p, ctx = _make_plugin_with_qzone(tmp_path)
    calls = {"n": 0, "seed": 0}

    async def trigger(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": False, "error": "未找到已存在的聊天流: xxx"}
        return {"success": True}

    async def route(name, msg):
        calls["seed"] += 1
        return {"success": True}

    ctx.maisaka.proactive.trigger = trigger
    ctx.gateway.route_message = route
    await p._qzone_send_trigger({"kind": "daily", "activity": "忙"}, browsed=False)
    assert calls["n"] == 2 and calls["seed"] == 1


@pytest.mark.asyncio
async def test_send_trigger_once_per_window(tmp_path):
    """同窗口不重复触发。"""

    p, ctx = _make_plugin_with_qzone(
        tmp_path, window={"kind": "daily", "read_qzone": False, "send_qzone": True})
    n = {"i": 0}

    async def trigger(**kwargs):
        n["i"] += 1
        return {"success": True}

    ctx.maisaka.proactive.trigger = trigger
    await p._qzone_poll_feeds()
    await p._qzone_poll_feeds()  # 同窗口第二轮:armed 键不变防重(仅 send 窗口 window_active 恒 False,判重不依赖泵状态)
    await asyncio.sleep(0)
    assert n["i"] == 1


@pytest.mark.asyncio
async def test_send_trigger_browsed_waits_pump_idle_then_fires(tmp_path):
    """同窗形态(browsed=True):等注入泵空闲即触发,intent 是「刚刷完」语境
    (分享有上下文),reason 同款;泵空闲判定即时通过(初始队列空)。"""

    p, ctx = _make_plugin_with_qzone(tmp_path)
    fired = []

    async def trigger(**kwargs):
        fired.append(kwargs)
        return {"success": True}

    ctx.maisaka.proactive.trigger = trigger
    await p._qzone_send_trigger({"kind": "daily", "activity": "逛空间"}, browsed=True)
    assert len(fired) == 1
    assert "刚刷完QQ空间" in fired[0]["intent"] and "qzone_post" in fired[0]["intent"]


@pytest.mark.asyncio
async def test_send_trigger_browsed_fires_on_zero_new_feeds(tmp_path):
    """审查修正:read+send 窗口首轮零新动态(空发现)同样算完成浏览——空发现
    早退分支收尾也派发 browsed 触发,整窗不再静默;第二轮零新动态不重复。"""

    p, ctx = _make_plugin_with_qzone(
        tmp_path, window={"kind": "daily", "read_qzone": True, "send_qzone": True})
    fired = []

    async def trigger(**kwargs):
        fired.append(kwargs)
        return {"success": True}

    ctx.maisaka.proactive.trigger = trigger
    await p._qzone_poll_feeds()  # 发现层空时间线(默认桩):走空发现早退分支
    await asyncio.sleep(0)  # 收尾派发的后台触发任务在下一调度槽执行
    assert len(fired) == 1 and "刚刷完QQ空间" in fired[0]["intent"]
    await p._qzone_poll_feeds()  # 第二轮零新动态:first_poll_done 已置位,不重复
    await asyncio.sleep(0)
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_send_trigger_rearms_on_next_window(tmp_path):
    """窗口切换后重新武装:同一天另一个 send 窗口(不同 start)再次触发——
    邻接 qzone 窗口间 window_active 不复位,武装按窗口标识判重。"""

    now = datetime.now()
    first = {"kind": "daily", "read_qzone": False, "send_qzone": True,
             "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
             "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), "activity": "忙"}
    second = dict(first, start=(now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"))
    p, ctx = _make_plugin_with_qzone(tmp_path, window=first)
    n = {"i": 0}

    async def trigger(**kwargs):
        n["i"] += 1
        return {"success": True}

    ctx.maisaka.proactive.trigger = trigger
    await p._qzone_poll_feeds()
    await asyncio.sleep(0)
    assert n["i"] == 1  # 第一个 send 窗口已触发
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [second]}
    await p._qzone_poll_feeds()  # 切到另一个 send 窗口(标识变化→重新武装)
    await asyncio.sleep(0)
    assert n["i"] == 2
