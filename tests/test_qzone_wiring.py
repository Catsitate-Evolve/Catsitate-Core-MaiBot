"""QQ空间组合层行为测试(工具驱动架构):plugin 接线的行为断言,非源码字符串。

_StubCtx 模式参照 test_integration.py:离线装配插件实例,依赖全部注入桩,
只验证「组合层」行为——工具目标解析(registry/seen_store/awaiting)/评论频控/
@ 前缀/AuthError 自愈/统一通知轮询守卫(T11)与源B楼中楼上下文登记。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
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
from catsitate_core.qzone.wire import CommentItem, LikeEvent, ReplyItem
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


class _StubConfig:
    """主程序配置面最小桩:bot.nickname(#33 裁定后回注昵称必读)。"""

    def __init__(self, nickname="Catsitate-dev"):
        self.nickname = nickname

    async def get(self, key, default=""):
        if key == "bot.nickname":
            return self.nickname
        return default


class _StubCtx:
    """组合层测试的最小 ctx 面:logger + gateway + config。"""

    def __init__(self, logs):
        self.logger = _CollectLogger(logs)
        self.gateway = _StubGateway()
        self.config = _StubConfig()


class _StubCommentClient:
    """评论轮询输入桩:记录取数次数,get_own_feed_comments 返回固定三视图
    (评论映射/正文上下文/楼中楼回复,reply 视图缺省空)。"""

    def __init__(self, comments, ctx_map, replies=()):
        self._comments = comments
        self._ctx_map = ctx_map
        self._replies = list(replies)
        self.fetches = 0

    async def get_own_feed_comments(self, *, bot_uin, num=10):
        del bot_uin, num
        self.fetches += 1
        return self._comments, self._ctx_map, self._replies

    async def get_unified_timeline(self, *, count=20, begintime=None):
        del count, begintime
        return [], ""  # 发现层空:源B零拉取,聚焦源A行为

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
    p._qzone_seq = 0
    p._qzone_pump_lock = asyncio.Lock()  # 泵互斥锁(on_load 装配,离线测试手工补)
    # 发现层单飞锁(on_load 装配,离线测试手工补):浏览层与通知源B 共用的
    # 统一入口单飞+共享缓存+限流退避依赖此锁
    p._qzone_discovery_fetch_lock = asyncio.Lock()
    p._background_tasks = set()  # 后台任务引用集(tick 派发 feeds/scan 用,on_load 装配)
    # 与生产同构:seen/comment_seen 共用一个 SQLiteStore(_qzone_data_prune 走 self.store)
    store = SQLiteStore(tmp_path / "catsitate.db")
    p.store = store
    p.qzone_seen = SeenStore(store)
    p.qzone_seen.ensure_schema()
    p.qzone_comment_seen = CommentSeenStore(store)
    p.qzone_comment_seen.ensure_schema()
    # 源C 赞事件去重(on_load 装配,离线测试手工补)
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

    翻页语义:固定列表只在首页(begintime=None)生效,第 2 页起返回空(模拟
    「更早页无积压」的稳态,翻页止步)——否则同一列表被每页重复消费。
    """

    def __init__(self, discoveries):
        self._discoveries = discoveries
        self.discovery_calls = 0

    async def get_unified_timeline(self, *, count=20, begintime=None):
        del count
        self.discovery_calls += 1
        return (list(self._discoveries), "cur1") if begintime is None else ([], "")

    async def get_like_events(self, *, count=30):
        del count
        return []  # 源C 空:各测试按需子类覆盖(通知扫描三源都要经本接口取数)


def _png_bytes(color, size=(48, 32)) -> bytes:
    """纯色小 PNG(真实可解码字节,多图合成链需要合法图片输入)。"""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _patch_sleep(monkeypatch, record: list) -> None:
    """把 asyncio.sleep 换成记录桩(源B请求间隔断言用;不让测试真等 2 秒)。"""

    async def _fake_sleep(delay, *a, **k):
        record.append(float(delay))

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)


def test_poll_tick_window_start_opens_injector(tmp_path):
    """窗口开启冒烟(频控删除后,2026-09-02 用户裁定:同说说评论硬上限删除,
    防护交 QQ 侧频控+-10049 限制回执):read_qzone 窗口开始,注入泵正常激活。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "read_qzone": True,
    }]}

    # 发现层空时间线(默认桩):窗口开始分支后本轮提前返回,足以断言窗口状态
    asyncio.run(p._qzone_poll_feeds())
    assert p.qzone_injector.window_active is True  # 窗口正常开启(不跳过注入)


def test_qzone_inject_multi_image_composes_numbered_grid(tmp_path):
    """注入链多图合成(C 方案,2026-09-03 用户裁定):[:3] 截断删除,
    5 图说说全量下载后拼成一张带序号角标的合成图——注入消息恒单图片段
    (hash=合成图 sha256),省 VLM token 与注入上下文。"""

    from catsitate_core.qzone.imaging import compose_numbered_grid

    p = _make_plugin(tmp_path)
    pngs = {f"u{i}": _png_bytes((40 * i % 250, 30, 220)) for i in range(1, 6)}

    class _GridClient(_StubUnifiedClient):
        """按 url 返回不同 PNG 的下载桩(合成需要合法且互异的图字节)。"""

        def __init__(self):
            super().__init__([])
            self.downloads: list[str] = []

        async def download_image(self, url):
            self.downloads.append(url)
            return pngs[url]

    client = _GridClient()
    p.qzone_client = client
    p.qzone_injector.window_started()  # P2 浏览动态仅窗口内可弹
    p.qzone_injector.enqueue([FeedItem(
        tid="grid00000001", abstime="1750000000", uin="10001", nickname="小明",
        content="多图说说", image_urls=list(pngs),
    )])
    asyncio.run(p._qzone_pump())
    # 截断删除:5 张全量下载(合成后恒单图,无 media 爆炸面)
    assert client.downloads == ["u1", "u2", "u3", "u4", "u5"]
    # 副作用链完整走通:注入成功进入 awaiting,网关收到该条消息
    assert p.qzone_injector.awaiting_tid == "grid00000001"
    assert len(p._ctx.gateway.calls) == 1
    msg = p._ctx.gateway.calls[0][1]
    image_segs = [s for s in msg["raw_message"] if s.get("type") == "image"]
    assert len(image_segs) == 1  # 多图恒单图(合成)
    composite = compose_numbered_grid([(i, pngs[f"u{i}"]) for i in range(1, 6)])
    assert image_segs[0]["hash"] == hashlib.sha256(composite).hexdigest()
    assert base64.b64decode(image_segs[0]["binary_data_base64"]) == composite


# ---- 工具驱动:qzone_comment/qzone_reply/qzone_like 工具行为测试 ----


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
                   content_summary="今天去了海边", comment_map={"ct0": ("20000", "小红")})
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


def test_qzone_comment_reply_anchor_miss_skips_polish_llm(tmp_path):
    """目标解析前置(2026-09-04):锚失效(说说/楼中楼二元组解析不到)是纯本地
    判定,须在表达润色之前失败返回——旁路 LLM 一次都不调(旧序会先烧一次润色
    再失败),零写调用;锚有效时行为不变(既有用例覆盖)。"""

    p = _make_tool_plugin(tmp_path)
    # qzone_comment:说说锚失效
    res = asyncio.run(p.qzone_comment(feed_id="nosuchanchor", content="你好", stream_id="s1"))
    assert "未找到说说" in res
    assert p.expr_llm_calls == []  # 润色 LLM 零调用
    assert p.qzone_client.comment_calls == []

    # qzone_reply:说说锚失效
    res2 = asyncio.run(p.qzone_reply(feed_id="nosuchanchor", comment_id="c1", content="你好", stream_id="s1"))
    assert "未找到说说" in res2
    assert p.qzone_client.reply_calls == []

    # qzone_reply:说说锚有效但评论锚失效(二元组解析同样前置)
    _register_feed(p, tid="feednoanchor", owner="10001")
    res3 = asyncio.run(p.qzone_reply(feed_id="feednoanchor", comment_id="gone", content="你好", stream_id="s1"))
    assert "未找到这条评论" in res3
    assert p.expr_llm_calls == []  # 三种锚失效形态全零润色调用
    assert p.qzone_client.reply_calls == []


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
    # 同轮自愈(2026-09-02 #7):作废→重取(桩默认重取失败)→显式回执,不再「下轮再试」
    assert "登录态失效且 cookie 重取失败" in res
    assert p.qzone_cookie.invalidate_calls == 1  # cookie 已作废并尝试重取
    assert p.qzone_client.comment_calls == []  # 零写调用

    # 重取成功→原地重试一次(动作不放弃);仍失效→显式回执
    p2 = _make_tool_plugin(tmp_path)
    _register_feed(p2, tid="feedX", owner="10001")

    class _AuthOnceClient(_StubWriteClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def do_comment(self, *, fid, target_qq, content):
            self.calls += 1
            if self.calls == 1:
                raise QzoneAuthError("登录态失效(code=-3000)")
            return True

    p2.qzone_client = _AuthOnceClient()
    p2.qzone_cookie.get_result = {"p_skey": "fresh"}
    res = asyncio.run(p2.qzone_comment(feed_id="feedX", content="你好", stream_id="s1"))
    assert res.startswith("评论成功,已发出:")  # 同轮重试成功,动作未放弃
    assert p2.qzone_cookie.invalidate_calls == 1 and p2.qzone_client.calls == 2


def test_qzone_biz_error_too_frequent_receipts(tmp_path):
    """业务错误回执带限制语义(2026-09-02 联调实证 code=-10049,用户裁定:
    不做硬频控,把限制写进工具返回让模型自行收敛):四动作工具命中操作频繁
    时,回执明说「别再重试这条」;其它业务码回执带 code 且劝阻立即重试。"""

    from catsitate_core.qzone.client import BIZ_CODE_TOO_FREQUENT, QzoneBizError

    class _BizFailClient(_StubWriteClient):
        code = BIZ_CODE_TOO_FREQUENT

        async def do_comment(self, *, fid, target_qq, content):
            raise QzoneBizError(self.code, "too frequent")

        async def do_reply(self, *, fid, target_qq, comment_tid, comment_uin,
                           comment_nick, content, at_uin="", at_nick=""):
            raise QzoneBizError(self.code, "too frequent")

        async def do_publish(self, *, content):
            raise QzoneBizError(self.code, "too frequent")

        async def do_like(self, *, fid, target_qq):
            raise QzoneBizError(self.code, "too frequent")

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedF", owner="10001")
    p.qzone_client = _BizFailClient()

    res = asyncio.run(p.qzone_comment(feed_id="feedF", content="你好", stream_id="s1"))
    assert "操作太频繁" in res and "别重试" in res
    _register_feed(p, tid="feedR", owner=BOT_UIN,
                   commenter_uin="20000", commenter_nickname="小红",
                   comment_tid="ct1", comment_uin="20000")
    res = asyncio.run(p.qzone_reply(feed_id="feedR", comment_id="ct1", content="好", stream_id="s1"))
    assert "操作太频繁" in res and "别再重试这条" in res
    res = asyncio.run(p.qzone_post(content="发一条", stream_id="s1"))
    assert "操作太频繁" in res and "先歇一歇" in res
    res = asyncio.run(p.qzone_like(feed_id="feedF", stream_id="s1"))
    assert "操作太频繁" in res and "先歇一歇" in res

    # 其它业务码:回执带 code,劝阻立即重试(不误报「太频繁」)
    class _BizOtherClient(_BizFailClient):
        code = -9999

    p.qzone_client = _BizOtherClient()
    res = asyncio.run(p.qzone_comment(feed_id="feedF", content="你好", stream_id="s1"))
    assert "code=-9999" in res and "先不要立刻重试" in res and "操作太频繁" not in res


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
    """入参校验:缺说说ID/评论ID/内容 → 显式提示;全域化(2026-09-02 Q3)后任意
    流可用,但无评论级上下文(未通知过/未浏览/未查详情)→ 显式拒绝+指引(Q11),
    零写调用。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedR3", owner="10001")
    res = asyncio.run(p.qzone_reply(feed_id="", comment_id="c1", content="x", stream_id="s1"))
    assert "都不能为空" in res
    # 真实流调用不再被流门控拒绝,而是评论级解析失败显式指引
    res2 = asyncio.run(p.qzone_reply(feed_id="feedR3", comment_id="c1", content="x", stream_id="other"))
    assert "未找到这条评论" in res2 and "view_friend_feed_detail" in res2
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
    assert msg["message_info"]["group_info"]["group_id"] == "qzone_feed"  # 虚拟伪群号常量(固化,不可配置)
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
    )  # summary=正文前 50 字(own-post 摘要依赖)
    assert p.qzone_seen.get_message_id("fullpubtid000123456") == msg["message_id"]
    ctx = p._qzone_registry.resolve("fullpubtid00")  # 前缀锚解析(registry 两级口径)
    assert ctx is not None and ctx.tid == "fullpubtid000123456"
    assert ctx.owner_uin == BOT_UIN and ctx.owner_nickname == "我" and ctx.kind == "self"
    assert ctx.content_summary == "今天的心情很不错,写点什么好呢"  # 表达生成场景素材


def test_qzone_post_route_failure_skips_local_anchor(tmp_path):
    """缓议修正:回注 route 失败(异常)时本地锚定三连跳过——注入没成功
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


def test_qzone_post_echo_carries_full_content(tmp_path):
    """回注正文带全文(2026-09-02 用户裁定:截断全删):bot 对自己发过什么
    持有完整上下文锚,不再 60 字预览;锚行独立成行。"""

    long_content = "字" * 80
    p = _make_tool_plugin(tmp_path)
    asyncio.run(p.qzone_post(content=long_content, stream_id="s1"))
    assert p.qzone_client.publish_calls == [long_content]  # 全文发布(表达方向即正文的桩形态)
    _, msg = p._ctx.gateway.calls[0]
    # 回注全文;锚行独立成行
    assert msg["raw_message"][0]["data"] == f"我发布了一条说说:{long_content}\n〔说说ID=newtid0001〕"


def test_qzone_post_nickname_read_precedes_polish(tmp_path):
    """昵称读取前移(终审 b-1,2026-09-03):bot.nickname 读取失败(空值抛
    RuntimeError,#33 不兜底)时,异常在**润色之前**上抛——不得先烧一次润色
    LLM 再失败;零润色调用+零发布调用。"""

    p = _make_tool_plugin(tmp_path)
    p._ctx.config.nickname = ""  # #33 裁定:昵称空=主程序配置异常,直接抛错
    with pytest.raises(RuntimeError, match="bot 昵称"):
        asyncio.run(p.qzone_post(content="想发点什么都发不出去", stream_id="s1"))
    assert p.expr_llm_calls == []  # 润色 LLM 一次都不该调(旧序会先润色后读昵称)
    assert p.qzone_client.publish_calls == []  # 零发布调用
    assert p._ctx.gateway.calls == []  # 零回注


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
    # 同轮自愈(2026-09-02 #7):作废→重取(桩默认失败)→显式回执;零回注
    assert "登录态失效且 cookie 重取失败" in res
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


def test_qzone_post_nickname_read_failure_zero_publish(tmp_path):
    """昵称读取抛错前置(2026-09-03 复审修复):bot.nickname 为空触发
    _bot_echo_nickname 直接抛错(#33 裁定不兜底)——读取须发生在发布之前:
    零发布调用、零回注,异常原样上抛工具层;杜绝「发布成功却报失败」
    诱导模型重复发布的事故面。"""

    p = _make_tool_plugin(tmp_path)
    p._ctx.config.nickname = ""  # 主程序 bot.nickname 读取失败/为空形态
    with pytest.raises(RuntimeError):
        asyncio.run(p.qzone_post(content="这条不该发出去", stream_id="s1"))
    assert p.qzone_client.publish_calls == []  # 发布前失败:零发布调用
    assert p._ctx.gateway.calls == []  # 零回注


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

    async def _fake_side_llm(messages, model, module, timeout_ms=None, temperature=None):
        llm_calls.append(
            {"messages": messages, "model": model, "module": module, "temperature": temperature}
        )
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
            return list(msgs)  # 实机形态(2026-09-02 联调):SDK 已解包,直接返回 list
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


def test_diary_chat_timeline_accepts_dict_shape(tmp_path):
    """dict+success 形态兼容(能力未经 SDK 解包的返回):messages 键取列表,
    素材装配同样可用——两种返回形态都能吃(2026-09-02 联调后加固)。"""

    import time as _time

    now = datetime.now()
    p = _make_diary_plugin(tmp_path)
    p._stream_cache = {}
    p._stream_cache_at = _time.time()
    t1 = now - timedelta(seconds=40)
    msgs = [_diary_msg("20000", "小红", "dict 形态的消息", t1.timestamp())]

    async def _capability(name, **kw):
        if name == "message.get_by_time":
            return {"success": True, "messages": msgs}
        raise AssertionError(f"不应走回退路径:{name}")

    p._ctx.call_capability = _capability
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "dict 形态的消息" in stable


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


def test_diary_weather_line_and_word_count_guidance(tmp_path):
    """M3 修正:日记素材补真实天气(time_aware 快照,无数据
    省略该行)+篇幅区间指导(2026-09-04:去目标字数随机化——
    diary_word_count_min/max 配置区间进素材行,模板口径引用素材)。"""

    import re as _re

    p = _make_diary_plugin(tmp_path)
    # 快照表缺位(离线未建表):素材行省略并告警,不臆造天气
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "当前天气" not in stable
    assert any(
        level == "exception" and "日记天气素材" in str(a[0]) for level, a in p.logs
    )
    # 默认配置区间进素材行(确定性,无随机)
    assert "(目标篇幅80~200字)" in stable
    assert "长度按素材里给的篇幅区间" in p.llm_calls[0]["messages"][0]["content"]

    # 有快照:素材行出现(温度/天气码来自快照);fetched_at 须新鲜(1 小时前,
    # 过期快照按无数据处理——见 test_weather_text_staleness);自定义区间原样进素材
    p.store.execute(
        "CREATE TABLE IF NOT EXISTS weather_snapshot ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), city TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL, data TEXT NOT NULL)"
    )
    fresh_at = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.store.execute(
        "INSERT INTO weather_snapshot (id, city, fetched_at, data) VALUES (1, '珠海', "
        f"'{fresh_at}', '{{\"temperature_2m\": 26.5, \"weather_code\": 1}}')"
    )
    p.config.qzone.diary_word_count_min = 120
    p.config.qzone.diary_word_count_max = 350
    p.llm_calls.clear()
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "当前天气:温度 26.5°C(天气码 1)" in stable
    assert "(目标篇幅120~350字)" in stable
    assert not _re.search(r"目标 \d+ 字左右", stable)  # 旧随机目标行不复存在


def test_diary_generation_temperature_passthrough(tmp_path):
    """日记生成温度可配置:diary_llm_temperature ≥0 时传给 _side_llm_call;
    默认 -1 传 None(走主程序任务默认)。RPC 层 kwargs 形态见单独断言。"""

    p = _make_diary_plugin(tmp_path)
    asyncio.run(p._generate_and_publish_diary())
    assert p.llm_calls[0]["temperature"] is None  # 默认 -1 → 不携带

    p.config.qzone.diary_llm_temperature = 0.3
    p.llm_calls.clear()
    asyncio.run(p._generate_and_publish_diary())
    assert p.llm_calls[0]["temperature"] == 0.3


def test_side_llm_call_temperature_rpc_kwargs(tmp_path):
    """_side_llm_call 的 RPC kwargs 形态:temperature 仅在显式传入时携带
    (主机 core.py args.get("temperature"),None 走任务默认);timeout_ms=0
    归一 None 的既有行为不受影响。用基础装配(日记装配会把
    _side_llm_call 整个换成记录桩,测不到真方法)。"""

    p = _make_plugin(tmp_path)
    # 记账表在 on_load 建(基础装配不跑 on_load),按同款 DDL 手工补
    p.store.execute(
        "CREATE TABLE IF NOT EXISTS llm_usage ("
        "day TEXT NOT NULL, module TEXT NOT NULL, "
        "calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (day, module))"
    )
    captured: list[dict] = []

    async def _cap(capability, **kwargs):
        captured.append({"capability": capability, **kwargs})
        return {"success": True, "response": "ok"}

    p._ctx.call_capability = _cap
    asyncio.run(p._side_llm_call([{"role": "user", "content": "x"}], "memory", "test"))
    assert captured[0] == {
        "capability": "llm.generate", "timeout_ms": None,
        "prompt": [{"role": "user", "content": "x"}], "model": "memory",
    }
    asyncio.run(
        p._side_llm_call([{"role": "user", "content": "x"}], "memory", "test",
                         temperature=0.7, timeout_ms=120000)
    )
    assert captured[1] == {
        "capability": "llm.generate", "timeout_ms": 120000,
        "prompt": [{"role": "user", "content": "x"}], "model": "memory",
        "temperature": 0.7,
    }


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

    async def _fail(messages, model, module, timeout_ms=None, temperature=None):
        return {"success": False, "response": "boom"}

    p._side_llm_call = _fail
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == []
    assert p._pending_diary_snapshot.load() == {}
    assert any(
        level == "warning" and "QQ空间日记 LLM 失败" in str(a[0]) for level, a in p.logs
    )


def test_diary_generation_empty_skips_and_long_publishes(tmp_path):
    """内容护栏(对齐 diary_plugin):空文本跳过发布;**不再设超长硬上限**
    ——301 字照常发布(长度完全由素材行的篇幅区间软约束)。"""

    p = _make_diary_plugin(tmp_path)

    async def _empty(messages, model, module, timeout_ms=None, temperature=None):
        return {"success": True, "response": "   "}

    p._side_llm_call = _empty
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == []
    assert any(
        level == "warning" and "内容为空" in str(a[0]) for level, a in p.logs
    )

    async def _long(messages, model, module, timeout_ms=None, temperature=None):
        return {"success": True, "response": "字" * 301}

    p._side_llm_call = _long
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == ["字" * 301]  # 超长照发(无硬上限)


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


def test_diary_generation_auth_error_retries_in_round(tmp_path):
    """日记发布接同轮自愈(2026-09-03 复审,用户裁定 #7 语义,与 qzone_post 同款):
    AuthError → cookie 作废重取成功 → 原地重试一次发布成功;快照照常落(醒来回注)。
    未接入前登录态失效当晚日记会静默丢失。"""

    class _AuthOncePublish(_StubWriteClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def do_publish(self, *, content):
            self.calls += 1
            if self.calls == 1:
                raise QzoneAuthError("登录态失效(code=-3000)")
            return await super().do_publish(content=content)

    p = _make_diary_plugin(tmp_path)
    p.qzone_client = _AuthOncePublish()
    p.qzone_cookie.get_result = {"p_skey": "fresh"}  # 重取成功
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.calls == 2  # 失效一次,同轮原地重试
    assert p.qzone_cookie.invalidate_calls == 1  # cookie 已作废并重取
    # 仅重试成功那次计入发布(AuthError 次远端已拒发)
    assert p.qzone_client.publish_calls == ["今天窝着刷手机,看到小明去公园散步,有点懒洋洋的。"]
    data = p._pending_diary_snapshot.load()
    assert data.get("text") == "今天窝着刷手机,看到小明去公园散步,有点懒洋洋的。"
    assert data.get("tid") == "newtid0001"


def test_diary_generation_auth_refetch_fail_skips_publish(tmp_path):
    """日记发布登录态失效且 cookie 重取失败:告警跳过,零发布零快照——
    失败显式暴露,不静默也不伪装成发布。"""

    class _AuthFailPublish(_StubWriteClient):
        async def do_publish(self, *, content):
            raise QzoneAuthError("登录态失效(code=-3000)")

    p = _make_diary_plugin(tmp_path)
    p.qzone_client = _AuthFailPublish()
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == []  # 零发布(桩失败路径不记录)
    assert p.qzone_cookie.invalidate_calls == 1  # cookie 已作废并尝试重取(默认重取失败)
    assert p._pending_diary_snapshot.load() == {}  # 未发布的日记不落快照
    assert any(
        level == "warning" and "QQ空间日记发布失败" in str(a[0]) for level, a in p.logs
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
    assert msg["message_info"]["group_info"]["group_id"] == "qzone_feed"  # 虚拟伪群号常量(固化,不可配置)
    assert "is_mentioned" not in (msg["message_info"].get("additional_config") or {})
    assert msg["raw_message"] == [{"type": "text", "data": "我昨晚发布的日记:昨晚的日记正文"}]
    assert p._pending_diary_snapshot.load() == {}
    assert any(
        level == "info" and "日记醒来补注完成" in str(a[0]) for level, a in p.logs
    )


def test_echo_pending_diary_carries_full_text(tmp_path):
    """补注正文带全文(2026-09-02 用户裁定:截断全删,与 qzone_post 回注同纪律):
    bot 对昨晚日记持有完整上下文锚,不再 60 字预览。"""

    p = _make_diary_plugin(tmp_path)
    p._pending_diary_snapshot.save({"text": "字" * 80})
    asyncio.run(p._echo_pending_diary())
    _, msg = p._ctx.gateway.calls[0]
    assert msg["raw_message"][0]["data"] == f"我昨晚发布的日记:{'字' * 80}"  # 旧快照无 tid:无锚行


def test_echo_pending_diary_with_tid_anchors_seen_and_registry(tmp_path):
    """快照带 tid 的补注(本任务起的新形态):回注文本尾部带〔说说ID=前12位〕
    锚;route 成功后 seen(queued→seen,summary=日记正文前 50 字)+
    registry kind="self" 同款锚定(2026-09-02 起 summary/摘要存全文);锚定失败不拦
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
    assert ctx.content_summary == "昨晚的日记正文"  # 表达生成场景素材(全文)
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


def test_echo_pending_diary_nickname_failure_keeps_pending(tmp_path):
    """昵称读取抛错纳入 route try(2026-09-03 复审修复):补注构造取昵称失败时
    异常被既有 try 捕获告警、不外泄——快照保留,醒态 sleep_tick 下轮重试,
    入睡链不被瘫痪(取昵称在 try 外时每 60s 刷异常且快照永不清空)。"""

    p = _make_diary_plugin(tmp_path)
    p._pending_diary_snapshot.save({"text": "待补注日记"})
    p._ctx.config.nickname = ""  # bot.nickname 读取失败/为空形态
    asyncio.run(p._echo_pending_diary())  # 不外泄:被内层 try 捕获告警
    assert p._pending_diary_snapshot.load().get("text") == "待补注日记"
    assert p._ctx.gateway.calls == []  # 未走到注入
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


# ---- v1.0.0 内容护栏:三动作工具 + 日记拦截----


def _enable_guard(p, patterns):
    """护栏启用态模拟(绕过 on_load):enabled=True + 手工编译正则入实例属性,
    等价 on_load 装配产物(装配路径本身见下方实例级行为测试)。"""

    p.config.guard.enabled = True
    p._guard_compiled = [re.compile(pat) for pat in patterns]


def test_qzone_comment_guard_hit_blocks_api_and_accounting(tmp_path):
    """护栏命中(评论):润色后最终文本命中 → 零 API 调用零记账零 seen,回执
    明示规则编号;告警含工具名/规则号/文本前 60 字。"""

    p = _make_tool_plugin(tmp_path)
    _enable_guard(p, ["敏感词"])
    _register_feed(p, tid="guardhit01", owner="10001")
    # seen 预登记(mark_interacted 的落库对象,拦截时不得被置位)
    p.qzone_seen.mark_queued("guardhit01", abstime="1", author_uin="10001", summary="动态")
    res = asyncio.run(p.qzone_comment(feed_id="guardhit01", content="这条评论带敏感词", stream_id="s1"))
    assert res == "内容被拦截(命中规则1),未发布。"
    assert p.qzone_client.comment_calls == []  # 零 API 调用
    rows = p.qzone_seen.store.query("SELECT interacted FROM qzone_feeds WHERE tid = 'guardhit01'")
    assert rows and rows[0][0] == 0  # 零 seen 写(mark_interacted 未执行)
    assert p.qzone_comment_seen.store.query("SELECT 1 FROM qzone_fav_events") == []  # 零记账
    assert any(
        level == "warning"
        and a[0] == "内容护栏拦截:qzone_comment 命中规则%d,未发布(文本:%s...)"
        and a[1] == 1 and a[2] == "这条评论带敏感词"
        for level, a in p.logs
    )


def test_qzone_reply_guard_hit_blocks_api(tmp_path):
    """护栏命中(楼中楼回复):润色后最终文本命中 → 零 API 调用零记账,
    回执明示规则编号;告警含工具名/规则号/文本前 60 字。"""

    p = _make_tool_plugin(tmp_path)
    _enable_guard(p, ["敏感词"])
    _register_feed(p, tid="guardhit02", owner=BOT_UIN,
                   commenter_uin="20000", commenter_nickname="小红",
                   comment_tid="gc1", comment_uin="20000")
    res = asyncio.run(p.qzone_reply(feed_id="guardhit02", comment_id="gc1",
                                    content="回复里也有敏感词", stream_id="s1"))
    assert res == "内容被拦截(命中规则1),未发布。"
    assert p.qzone_client.reply_calls == []  # 零 API 调用
    assert p.qzone_comment_seen.store.query("SELECT 1 FROM qzone_fav_events") == []  # 零记账
    assert any(
        level == "warning"
        and a[0] == "内容护栏拦截:qzone_reply 命中规则%d,未发布(文本:%s...)"
        and a[1] == 1 and a[2] == "回复里也有敏感词"
        for level, a in p.logs
    )


def test_qzone_post_guard_hit_blocks_api(tmp_path):
    """护栏命中(发说说):润色后最终文本命中 → 零 API 调用零回注,回执明示
    规则编号(非首条规则=编号如实回显);告警含工具名/规则号/文本前 60 字。"""

    p = _make_tool_plugin(tmp_path)
    _enable_guard(p, ["绝不会命中的词", "敏感词"])
    res = asyncio.run(p.qzone_post(content="想发的说说里带敏感词", stream_id="s1"))
    assert res == "内容被拦截(命中规则2),未发布。"
    assert p.qzone_client.publish_calls == []  # 零 API 调用
    assert p._ctx.gateway.calls == []  # 零回注(拦截即未发布)
    assert any(
        level == "warning"
        and a[0] == "内容护栏拦截:qzone_post 命中规则%d,未发布(文本:%s...)"
        and a[1] == 2 and a[2] == "想发的说说里带敏感词"
        for level, a in p.logs
    )


def test_guard_enabled_miss_publishes_normally(tmp_path):
    """护栏启用未命中:三工具照常发布(拦截只对命中生效),零拦截告警。"""

    p = _make_tool_plugin(tmp_path)
    _enable_guard(p, ["敏感词"])
    _register_feed(p, tid="cleanfeed01", owner="10001",
                   commenter_uin="20000", commenter_nickname="小红",
                   comment_tid="cc1", comment_uin="20000")
    res_c = asyncio.run(p.qzone_comment(feed_id="cleanfeed01", content="很不错的照片", stream_id="s1"))
    assert res_c.startswith("评论成功,已发出:")
    res_r = asyncio.run(p.qzone_reply(feed_id="cleanfeed01", comment_id="cc1",
                                      content="谢谢你", stream_id="s1"))
    assert res_r.startswith("回复成功")
    res_p = asyncio.run(p.qzone_post(content="今天心情很好", stream_id="s1"))
    assert res_p.startswith("发布成功,已发出说说:")
    assert p.qzone_client.comment_calls == [("cleanfeed01", "10001", "很不错的照片")]
    assert len(p.qzone_client.reply_calls) == 1 and p.qzone_client.reply_calls[0][5] == "谢谢你"
    assert p.qzone_client.publish_calls == ["今天心情很好"]
    assert not any(level == "warning" and "内容护栏拦截" in str(a[0]) for level, a in p.logs)


def test_guard_disabled_matching_text_publishes(tmp_path):
    """护栏未启用零行为变化:_guard_compiled 空列表匹配恒 0(天然短路),
    含命中模式的文本三工具照常发布。"""

    p = _make_tool_plugin(tmp_path)
    assert p.config.guard.enabled is False and p._guard_compiled == []
    _register_feed(p, tid="offfeed001", owner="10001",
                   commenter_uin="20000", commenter_nickname="小红",
                   comment_tid="oc1", comment_uin="20000")
    res_c = asyncio.run(p.qzone_comment(feed_id="offfeed001", content="评论带敏感词也照发", stream_id="s1"))
    assert res_c.startswith("评论成功,已发出:")
    res_r = asyncio.run(p.qzone_reply(feed_id="offfeed001", comment_id="oc1",
                                      content="回复带敏感词", stream_id="s1"))
    assert res_r.startswith("回复成功")
    res_p = asyncio.run(p.qzone_post(content="说说带敏感词", stream_id="s1"))
    assert res_p.startswith("发布成功,已发出说说:")
    assert p.qzone_client.comment_calls == [("offfeed001", "10001", "评论带敏感词也照发")]
    assert len(p.qzone_client.reply_calls) == 1 and p.qzone_client.reply_calls[0][5] == "回复带敏感词"
    assert p.qzone_client.publish_calls == ["说说带敏感词"]


def test_diary_guard_hit_skips_publish_and_snapshot(tmp_path):
    """护栏命中(日记):LLM 产出文本命中 → 不发布不落快照(醒来无可回注),
    告警含「日记」/规则号/文本前 60 字。"""

    p = _make_diary_plugin(tmp_path)
    _enable_guard(p, ["敏感词"])

    async def _llm(messages, model, module, timeout_ms=None, temperature=None):
        return {"success": True, "response": "今天过得平静,日记里出现了敏感词,就到这里。"}

    p._side_llm_call = _llm
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == []  # 零发布
    assert p._pending_diary_snapshot.load() == {}  # 零快照(不发布不落待回注素材)
    assert any(
        level == "warning"
        and a[0] == "内容护栏拦截:日记 命中规则%d,未发布(文本:%s...)"
        and a[1] == 1 and a[2] == "今天过得平静,日记里出现了敏感词,就到这里。"
        for level, a in p.logs
    )


def test_diary_guard_enabled_miss_publishes(tmp_path):
    """护栏启用未命中(日记):发布与快照照常(拦截只对命中生效)。"""

    p = _make_diary_plugin(tmp_path)
    _enable_guard(p, ["敏感词"])
    asyncio.run(p._generate_and_publish_diary())
    assert p.qzone_client.publish_calls == ["今天窝着刷手机,看到小明去公园散步,有点懒洋洋的。"]
    assert p._pending_diary_snapshot.load().get("text")


def test_guard_assembly_compiles_on_load_path(tmp_path, monkeypatch):
    """on_load 装配路径实例级行为(T1 审查遗留补测,装配块抽为 _assemble_guard
    供离线直调):enabled=False 零编译保持空列表;enabled=True 按 guard.patterns
    编译入 _guard_compiled(可命中);非法正则整组置空+告警(错误显式暴露)。"""

    import plugin as plugin_mod
    from catsitate_core.guard import match_guard

    compile_calls: list = []
    real_compile = plugin_mod.compile_guard

    def _record_compile(patterns):
        compile_calls.append(list(patterns))
        return real_compile(patterns)

    monkeypatch.setattr(plugin_mod, "compile_guard", _record_compile)

    p = _make_tool_plugin(tmp_path)
    # 未启用(默认):零编译调用,实例属性保持空列表
    assert p.config.guard.enabled is False
    p._assemble_guard()
    assert compile_calls == []
    assert p._guard_compiled == []

    # 启用:编译入参=guard.patterns,产出可命中的编译列表
    p.config.guard.enabled = True
    p.config.guard.patterns = ["敏感词", "风控\\d+"]
    p._assemble_guard()
    assert compile_calls == [["敏感词", "风控\\d+"]]
    assert len(p._guard_compiled) == 2
    assert match_guard(p._guard_compiled, "正文含敏感词") == 1
    assert match_guard(p._guard_compiled, "触发风控100") == 2
    assert not any(level == "warning" for level, a in p.logs)

    # 非法正则:整组置空+告警(护栏失效不阻断插件加载,错误显式暴露)
    p.config.guard.patterns = ["ok", "[unclosed"]
    p._assemble_guard()
    assert p._guard_compiled == []
    assert any(level == "warning" and "整组护栏失效" in str(a[0]) for level, a in p.logs)


def test_qzone_like_accounting_failure_still_reports_success(tmp_path):
    """记账异常保护(2026-09-04):远端点赞成功后本地记账(fav_event 等 SQLite
    写入)抛异常——只告警不误报失败;回执仍成功(谎报失败会诱导模型对同一条
    说说重复点赞),与 qzone_comment/qzone_reply 的记账 try 保护同款。"""

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="likeacct01", owner="10001", nickname="小明")

    def _boom(*a, **k):
        raise RuntimeError("sqlite database is locked")

    p.qzone_comment_seen.fav_event = _boom
    res = asyncio.run(p.qzone_like(feed_id="likeacct01", stream_id="s1"))
    assert res.startswith("点赞成功:小明 的说说")  # 回执不误报失败
    assert p.qzone_client.like_calls == [("likeacct01", "10001")]  # 远端动作已成功
    assert any(
        level == "exception" and "点赞记账失败" in str(a[0]) and "远端已成功" in str(a[0])
        for level, a in p.logs
    )  # 错误显式暴露(告警留痕)


def test_qzone_like_via_feed_id_and_notify_origin(tmp_path):
    """qzone_like:feed_id 参数(锚前缀)经 registry 解析为全量 tid;
    通知 awaiting 不再拒赞——缺省目标取 origin_tid(真实说说),owner=源A=bot。"""

    import time as _time

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="likefeed0001", owner="10001", nickname="小明")
    res = asyncio.run(p.qzone_like(feed_id="likefeed0001", stream_id="s1"))
    assert res.startswith("点赞成功:小明 的说说")
    assert p.qzone_client.like_calls == [("likefeed0001", "10001")]  # 全量 tid+主人

    # 通知项 awaiting(合成 tid):缺省目标=origin_tid 真实说说,可点其原说说;
    # 泵注入时登记 FeedContext(owner=源A主人=bot;2026-09-03 复审小修后通知分支
    # owner_nickname 置空——评论者昵称与主人昵称语义错位)——此处按同款登记模拟
    p2 = _make_tool_plugin(tmp_path)
    _register_feed(p2, tid="realtid", owner=BOT_UIN, nickname="")
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
    assert res2.startswith("点赞成功:10000 的说说")  # 昵称缺省回退 owner_uin(bot 自己)
    assert p2.qzone_client.like_calls == [("realtid", BOT_UIN)]  # 真实 tid+源A主人=bot


def test_qzone_like_on_notify_registration_falls_back_to_owner_uin(tmp_path):
    """2026-09-03 复审小修:泵对通知项登记的 owner_nickname 置空——notify 分支
    feed.nickname 实为评论者/点赞者昵称,与 owner_uin(说说主人)语义错位,原样
    登记会让 qzone_like 回执张冠李戴(「点赞成功:小红 的说说」实为 bot 自己的
    说说)。置空后回执昵称回退 owner_uin;评论者昵称仍留在 commenter_nickname;
    registry 字段级合并保留浏览/detail 登记过的正确主人昵称(置空不清旧值)。"""

    p = _make_tool_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_injector.enqueue_priority([FeedItem(
        tid="notify_comment_selff_c1", abstime="1750000000", uin="20000",
        nickname="小红", content="(通知) 小红 评论了你的说说", source="notify",
        origin_tid="selffeed001", friend_uin="",
    )])
    asyncio.run(p._qzone_pump())
    ctx = p._qzone_registry.resolve("selffeed001")
    assert ctx is not None
    assert ctx.owner_uin == BOT_UIN  # 源A:说说主人=bot 自己
    assert ctx.owner_nickname == ""  # 通知分支不再把评论者昵称当主人昵称
    assert ctx.commenter_nickname == "小红"  # 评论者昵称留在正确字段
    res = asyncio.run(p.qzone_like(feed_id="selffeed001", stream_id="s1"))
    assert res.startswith("点赞成功:10000 的说说")  # 昵称回退 owner_uin
    assert "小红" not in res  # 回执不显示评论者昵称
    assert p.qzone_client.like_calls == [("selffeed001", BOT_UIN)]


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
            return {}, {}, []

    p.qzone_client = _ProbeClient()
    asyncio.run(p._qzone_notify_scan())
    assert fetches == []  # awaiting 未释放:不取数(不叠加)
    p.qzone_injector.on_turn_complete(_time.monotonic())
    asyncio.run(p._qzone_notify_scan())
    assert fetches == [1]  # awaiting 释放后恢复取数


def test_notify_scan_source_a_reply_under_bot_comment_notifies(tmp_path):
    """源A 断链修复(2026-09-04):好友在自己说说下回复 bot 的评论(list_3,同载荷
    第三视图)→ 通知注入(形态与源B 一致:楼中楼上下文+参数行);归属区别断言——
    origin_sender/comment_uin=bot(自己说说、父评论作者是 bot);去重键登记
    二次扫描不重发;bot 自己的楼中楼回复跳过(不通知自己)。"""

    import time as _time

    now_s = str(int(_time.time()))
    replies = [
        ReplyItem(reply_tid="rr1", parent_comment_tid="bc1", feed_tid="ownfeed1",
                  friend_uin=BOT_UIN, uin="20000", nickname="小红",
                  content="再来一句", create_time=now_s,
                  feed_content="我的说说正文", parent_comment_content="我的评论"),
        # bot 自己的楼中楼回复:不通知自己(插件层防御跳过,与顶层自评同款)
        ReplyItem(reply_tid="rr_self", parent_comment_tid="bc1", feed_tid="ownfeed1",
                  friend_uin=BOT_UIN, uin=BOT_UIN, nickname="我",
                  content="bot 自己的楼中楼", create_time=now_s,
                  feed_content="我的说说正文", parent_comment_content="我的评论"),
    ]
    # bot 自评(bci 的顶层评论):幂等登记不注入,不影响楼中楼段
    comments = {"ownfeed1": [CommentItem(
        comment_tid="bc1", uin=BOT_UIN, nickname="我", content="我的评论", create_time=now_s,
    )]}
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_client = _StubCommentClient(comments, {"ownfeed1": "我的说说正文"}, replies)
    # 原说说曾注入过(seen 记录 message_id)→ 通知注入消息带 reply 段引用它
    p.qzone_seen.mark_queued("ownfeed1", abstime=now_s, author_uin=BOT_UIN, summary="我的说说正文")
    p.qzone_seen.mark_seen("ownfeed1", "2026-09-01T10:00:00", "qzone_ownfeed1_3")

    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    msg = p._ctx.gateway.calls[0][1]
    assert msg["message_info"]["user_info"]["user_id"] == "20000"
    assert "notify_reply_ownfeed1_rr1" in msg["message_id"]
    # reply 段引用原说说注入消息(自己说说:sender=bot 自己)
    reply = msg["raw_message"][0]
    assert reply["type"] == "reply"
    assert reply["data"]["target_message_id"] == "qzone_ownfeed1_3"
    assert reply["data"]["target_message_sender_id"] == BOT_UIN
    # 正文=楼中楼上下文(bot 原评论前 20 字)+参数行(评论ID=主评论 bc1,
    # 评论者QQ=回复者;动作时间随运行时刻→前缀+后缀断言)
    text = msg["raw_message"][1]["data"]
    assert text.startswith("回复了你的评论「我的评论」:再来一句\n〔说说ID=ownfeed1 评论ID=bc1 评论者QQ=20000 回复于(今天")
    assert text.endswith(")〕")
    # registry 登记对位:owner=bot(自己说说);主评论二元组=bot 的评论(作者=bot);
    # 评论者=回复者(qzone_reply 的 @ 目标)
    ctx = p._qzone_registry.resolve("ownfeed1")
    assert ctx is not None
    assert (ctx.owner_uin, ctx.comment_tid, ctx.comment_uin) == (BOT_UIN, "bc1", BOT_UIN)
    assert (ctx.commenter_uin, ctx.commenter_nickname) == ("20000", "小红")
    # 楼中楼去重键已登记(下轮判重);bot 自己的回复键零登记
    assert p.qzone_comment_seen.is_new("ownfeed1:bc1:reply:rr1") is False
    assert p.store.query(
        "SELECT comment_key FROM qzone_comments WHERE comment_key LIKE '%rr_self%'"
    ) == []
    # fav_event 记账(COMMENT,指向回复者)
    since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    evs = p.qzone_comment_seen.fav_events_since("20000", since)
    assert any(e["kind"] == "COMMENT" and "回复了你的评论" in e["text"] for e in evs)

    # 二次扫描:释放 awaiting 后键判重不重发(取数发生,不是早退);bot 自评仍不注入
    p.qzone_injector.on_turn_complete(_time.monotonic())
    asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    assert p.qzone_client.fetches == 2


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
            return {}, {}, []

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
            return {}, {}, []

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
    """源C:「与我相关」流赞事件 → 去重(qzone_likes 发现即登记)/
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
            return {}, {}, []

        async def get_like_events(self, *, count=30):
            del count
            self.like_fetches += 1
            return list(events)

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # 自己的说说已发布回注:qzone_feeds 有 summary → 通知带「摘要」标题
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
    # fav_event 记账(kind=LIKE):源C 好感度显式事件(滚动窗取数,昨日下界必取到当下登记)
    since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    evs = p.qzone_comment_seen.fav_events_since("20000", since)
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
            return comments, {"feeda": "说说正文"}, []

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
    # 同轮自愈(2026-09-02 #7):作废→重取(桩默认失败)→源C 按空处理不中断
    assert p.qzone_cookie.invalidate_calls == 1
    assert any("cookie 重取失败" in " ".join(str(x) for x in a) for level, a in p.logs if level == "warning")
    assert any(level == "warning" and "通知源C" in " ".join(str(x) for x in a)
               for level, a in p.logs)


def test_notify_scan_source_c_runtime_error_keeps_source_a_notifications(tmp_path):
    """审查修复(2026-09-03):源C 取数抛非 Auth 异常(如相对时间折算遇非闰年
    2月29日的 ValueError)——调用点独立隔离(源B 同款纪律):告警后按空处理
    继续不上抛;上抛会触发扫描级原子性兜底,回退本轮源A/B 已登记的全部去重键,
    通知未入队即中止且每 120 秒重复崩溃。源A 通知照常入队注入、键不回退。"""

    import time as _time

    from catsitate_core.qzone.wire import CommentItem

    comments = {"feeda": [CommentItem(
        comment_tid="ca1", uin="20000", nickname="小红", content="好友评论",
        create_time=str(int(_time.time())),
    )]}

    class _BoomLikeClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return comments, {"feeda": "说说正文"}, []

        async def get_like_events(self, *, count=30):
            del count
            raise RuntimeError("源C 解析崩溃(相对时间折算 2月29日)")

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_client = _BoomLikeClient()
    asyncio.run(p._qzone_notify_scan())  # 不抛:异常被源C 调用点隔离
    # 源A 通知未丢:正常注入 1 条
    assert len(p._ctx.gateway.calls) == 1
    assert p._ctx.gateway.calls[0][1]["message_info"]["user_info"]["user_id"] == "20000"
    # 隔离显式告警(exception 级,含源C 上下文),非 Auth 不作废 cookie
    assert any(level == "exception" and "源C拉取失败" in " ".join(str(x) for x in a)
               for level, a in p.logs)
    assert p.qzone_cookie.invalidate_calls == 0
    # 源A 去重键不回退(已消费,注入完成——与上抛回退路径的区别)
    assert p.qzone_comment_seen.is_new("feeda:ca1:20000") is False


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


def test_notify_scan_reverts_registered_keys_on_failure(tmp_path):
    """原子性兜底(终审 H-1 修复,2026-09-02):源A 登记后入队侧异常
    (enqueue_priority 抛非 Auth 异常)——已登记未入队的去重键回退
    (pending_retry=1),下轮重新发现,通知不永久静默丢失。
    (审查修复 2026-09-03:源C 取数异常已改为调用点独立隔离不再上抛,本用例
    失败注入点从源C 移至入队侧,外层原子性回退语义保持覆盖。)"""

    import time as _time

    from catsitate_core.qzone.wire import CommentItem

    comments = {"feedh1": [CommentItem(
        comment_tid="ch1", uin="20000", nickname="小红", content="会被回退的评论",
        create_time=str(int(_time.time())),
    )]}

    class _EnqueueBoomClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return comments, {"feedh1": "正文"}, []

        async def get_like_events(self, *, count=30):
            del count
            return []

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_client = _EnqueueBoomClient()

    def _boom_enqueue(items):
        del items
        raise RuntimeError("入队炸了")

    p.qzone_injector.enqueue_priority = _boom_enqueue
    with pytest.raises(RuntimeError):
        asyncio.run(p._qzone_notify_scan())
    rows = p.qzone_comment_seen.store.query(
        "SELECT pending_retry FROM qzone_comments WHERE comment_key LIKE 'feedh1:%'"
    )
    assert rows and int(rows[0][0]) == 1  # 键已软回退:下轮 is_new 重新激活
    assert any("回退本轮已登记去重键" in " ".join(str(x) for x in a) for level, a in p.logs if level == "exception")
    assert p._ctx.gateway.calls == []  # 未注入(下轮重发现后注入)


def test_validate_schedule_threshold_falls_back_with_warning(tmp_path):
    """speak_threshold_level 非法值(终审 M-4 修复):显式告警+回退「熟悉」,
    不再静默停用日程主动发言;合法值零告警零改动。"""

    p = _make_plugin(tmp_path)
    p.config.schedule.speak_threshold_level = "好友"  # 非法(合法:陌生/熟悉/亲近/挚友/特别)
    p._validate_schedule_threshold()
    assert p.config.schedule.speak_threshold_level == "熟悉"
    assert any(level == "warning" and "speak_threshold_level" in " ".join(str(x) for x in a)
               for level, a in p.logs)
    p.logs.clear()
    p.config.schedule.speak_threshold_level = "亲近"
    p._validate_schedule_threshold()
    assert p.config.schedule.speak_threshold_level == "亲近"  # 合法值不动
    assert not any(level == "warning" for level, a in p.logs)


def test_config_update_validates_schedule_threshold(tmp_path):
    """M-1(v1 清理,2026-09-03):on_config_update(scope=self) 热重载须补跑
    _validate_schedule_threshold——热改注入非法等级同样显式告警+回退;旧实现
    只在 on_load 校验一次,热改坏值会静默停用日程主动发言直到下次重启。"""
    from types import SimpleNamespace

    from catsitate_core.decay import DecayExecutor
    from catsitate_core.favorability import BatchEngine
    from catsitate_core.msg_react import MsgReactEngine
    from catsitate_core.poke import PokeEngine
    from catsitate_core.schedule import ScheduleGenerator
    from catsitate_core.services.scheduler import Scheduler
    from catsitate_core.sleep import SleepManager

    p = _make_plugin(tmp_path)
    p.assembler = SimpleNamespace(reset=lambda: None)
    p._env_cache = {}
    p._snapshot_cache = {}
    p._scheduler = Scheduler(tick_seconds=60)
    # 热重载分支会重指各引擎配置引用,离线装配须带上(on_load 同款最小集)
    async def _llm(messages, model=""):
        return {"success": True, "response": "{}", "model": model}

    p.sleep = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), p.config.sleep)
    p.schedule_gen = ScheduleGenerator(_llm, p.config.schedule, p.config.sleep)
    p.fav_engine = BatchEngine(p.store, p.config.favorability)
    p.decay = DecayExecutor(p.store, p.config.favorability, _llm)
    p.react = MsgReactEngine(JsonSnapshot(tmp_path / "msg_react_cooldown.json"), p.config.msg_react)
    p.poke = PokeEngine(JsonSnapshot(tmp_path / "poke_cooldown.json"), p.config.poke)

    async def _no_selfcheck():
        return False

    p._qzone_selfcheck = _no_selfcheck  # 隔离:自检不在本测试范围
    p.config.schedule.speak_threshold_level = "好友"  # 非法值经热重载注入
    asyncio.run(p.on_config_update("self", {}, ""))
    assert p.config.schedule.speak_threshold_level == "熟悉"
    assert any(level == "warning" and "speak_threshold_level" in " ".join(str(x) for x in a)
               for level, a in p.logs)


def test_notify_scan_drives_pump_on_stale_awaiting(tmp_path):
    """awaiting 死锁解锁(2026-09-02 联调缺陷):窗口结束后 planner 长期不跑轮
    (自然概率「等待更多消息」),awaiting 超时兜底本只在泵里做而泵的常规入口
    是浏览窗口 tick 与轮完成信号——通知扫描被「awaiting 占用」门挡住,好友
    新评论全部未被扫描(实机卡 18 分钟)。修复后:扫描遇 awaiting 先驱动泵
    (超时则强制推进并注入下一条),未超时才维持不叠加。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.qzone_injector.decision_window_s = 1  # 注入后 1s 即超时(hard_cap 不涉:无 wait 态)

    # 通知项 A 注入,占用 awaiting(窗口外 P1 推送语义)
    feed_a = FeedItem(tid="notifyA", abstime=str(int(_time.time())), uin="20000",
                      nickname="小红", content="(通知) A", source="notify", origin_tid="realtidA")
    p.qzone_injector.enqueue_priority([feed_a])
    asyncio.run(p._qzone_pump())
    assert len(p._ctx.gateway.calls) == 1 and p.qzone_injector.awaiting_feed is not None

    # 源A 桩:好友在自己说说下的新评论(create_time=当前,新鲜度窗内)
    class _Client(_StubCommentClient):
        def __init__(self):
            import time as _t
            super().__init__(
                {"feedown1": [CommentItem(comment_tid="c9", uin="20000", nickname="小红",
                                          content="新的评论", create_time=str(int(_t.time())))]},
                {"feedown1": "自己说说的正文"},
            )

        async def get_unified_timeline(self, *, count=20, begintime=None):
            return []

        async def get_like_events(self, *, count=30):
            return []

    client = _Client()
    p.qzone_client = client

    # 未超时时扫描维持不叠加(不取数不注入)
    asyncio.run(p._qzone_notify_scan())
    assert client.fetches == 0 and len(p._ctx.gateway.calls) == 1

    # 过决策窗后扫描:驱动泵→超时强制推进→发现新评论→注入(死锁解除)
    import asyncio as _asyncio
    awaitable = p._qzone_notify_scan()

    async def _run_after_expiry():
        await _asyncio.sleep(1.2)
        await awaitable

    asyncio.run(_run_after_expiry())
    assert client.fetches == 1  # 扫描真正执行(不再被卡死的 awaiting 挡住)
    assert len(p._ctx.gateway.calls) == 2  # 新评论通知已注入
    assert any(level == "warning" and "强制推进" in str(a[0]) for level, a in p.logs)
    assert p.qzone_injector.awaiting_feed is not None  # 新通知占用 awaiting(串行语义)


def test_notify_scan_source_c_drift_warn_once(tmp_path):
    """源C 漂移告警(收敛:常规轮次不打「解析 N 条」观测日志,仅保留
    异常信号):连续 3 轮取数成功但零事件打一次锚点漂移 warning(去重标记,
    后续空轮不重复告警);恢复有事件后计数与标记复位,再次连续 3 轮空才会
    告警第二次(新漂移段落)。"""

    import time as _time

    class _MutableLikeClient(_StubUnifiedClient):
        """源C 输入桩:赞事件列表可变(观测线的空/非空轮切换)。"""

        def __init__(self):
            super().__init__([])
            self.events: list = []

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}, []

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

    for _ in range(4):  # 连续 4 轮空:第 3 轮触发一次,第 4 轮不重复
        asyncio.run(p._qzone_notify_scan())
    assert _drift_warnings() == 1  # warn-once

    client.events = [LikeEvent(
        like_key="20000_10000_aaaa1111", liker_uin="20000", liker_nickname="小红",
        owner_uin=BOT_UIN, target_tid="ownfeed1", create_time=str(int(_time.time())),
    )]
    asyncio.run(p._qzone_notify_scan())
    p.qzone_injector.on_turn_complete(_time.monotonic())  # 释放 awaiting,后续轮可继续
    assert _drift_warnings() == 1  # 恢复非空不告警

    client.events = []
    for _ in range(3):
        asyncio.run(p._qzone_notify_scan())
    assert _drift_warnings() == 2  # 复位后的新漂移段落:第二次告警


def test_qzone_reply_mixed_notify_and_comment_map_uses_matching_anchor(tmp_path):
    """终审 H-1 修复(2026-09-03):通知登记过的说说(registry 字段级合并保留
    comment_uin/commenter),回复评论区里**另一条**评论(comment_id≠通知主评论)
    时——二元组与 @ 目标都取 comment_map 命中,不再被旧通知上下文错挂线程;
    回复通知的那条主评论(锚匹配)时才用通知二元组与评论者。"""

    p = _make_tool_plugin(tmp_path)
    # 通知登记:主评论 ct_notify(作者=bot,源B 形态),评论者=小红
    _register_feed(p, tid="feedMix", owner="10001", commenter_uin="20000",
                   commenter_nickname="小红", comment_tid="ct_notify", comment_uin=BOT_UIN,
                   comment_map={"ct_other": ("30000", "小蓝")})  # 浏览/detail 补充的评论锚
    # ① 回复另一条评论:comment_map 命中(通知锚不匹配)→ 二元组=小蓝、@小蓝
    res = asyncio.run(p.qzone_reply(feed_id="feedMix", comment_id="ct_other",
                                    content="看到这条了", stream_id="s1"))
    assert res.startswith("回复成功,已回复 小蓝")
    call = p.qzone_client.reply_calls[-1]
    assert call[3] == "30000" and call[6] == "30000"  # comment_uin/at_uin=评论作者
    # ② 回复通知的主评论:锚匹配 → 通知二元组(bot 自己的主评论)+@评论者小红
    res2 = asyncio.run(p.qzone_reply(feed_id="feedMix", comment_id="ct_notify",
                                     content="回你", stream_id="s1"))
    assert res2.startswith("回复成功,已回复 小红")
    call2 = p.qzone_client.reply_calls[-1]
    assert call2[3] == BOT_UIN and call2[6] == "20000"  # 二元组=bot、@=评论者


def test_view_friend_feeds_pagination_empty_page_hint(tmp_path):
    """翻页(Q8=A 设计共识 2026-09-02):page 透传 client(pos=(page-1)*num);
    第 2 页为空 → 「没有更多了」诚实提示,不编造。"""

    p = _make_plugin(tmp_path)
    pages: list[int] = []

    async def get_user_feeds(*, target_uin, nickname, num=3, page=1):
        pages.append(page)
        return []  # 模拟翻到底

    p.qzone_client.get_user_feeds = get_user_feeds
    res = asyncio.run(p.view_friend_feeds(qq="100", count=3, page=2))
    assert pages == [2]  # 页码透传
    assert "没有更多了" in res and "第 2 页为空" in res
    res1 = asyncio.run(p.view_friend_feeds(qq="100", count=3))  # 默认第 1 页
    assert pages == [2, 1] and "最近没有可见的说说" in res1  # 首页空=没有说说(措辞分言)


def test_view_friend_feed_detail_returns_comments_and_marks_seen(tmp_path):
    """详情工具(设计共识 Q1A/Q2B/Q4/Q10B/Q12A):feed_id 锚解析主人(qq 可省)、
    返回正文+评论区(含评论ID 锚)+图片 content_items;查看即 mark_seen;
    registry 登记 comment_map 供 qzone_reply 评论级解析。"""

    from catsitate_core.qzone.wire import FeedComment

    detail_feed = FeedItem(
        tid="detailfeed0001", abstime="1750000000", uin="100", nickname="小明",
        content="这条说说的正文", image_urls=["http://img.qpic.cn/d.jpg"],
        comments=[FeedComment(comment_tid="dc1", uin="20000", nickname="小红",
                              content="第一条评论", create_time="1750000500")],
        comment_total=1,
    )

    async def get_user_feeds(*, target_uin, nickname, num=20, page=1):
        assert target_uin == "100"  # qq 省略时经 registry 锚解析出主人
        return [detail_feed]

    async def download(url):
        return b"fakejpeg"

    p = _make_plugin(tmp_path)
    p.qzone_client.get_user_feeds = get_user_feeds
    p.qzone_client.download_image = download
    _register_feed(p, tid="detailfeed0001", owner="100")  # 预置锚:主人可解析
    # 2026-09-03 复审修复:预置浏览注入落的 reply 段锚(detail 查看只置 seen 不得抹掉)
    p.qzone_seen.mark_queued("detailfeed0001", abstime="1750000000", author_uin="100", summary="这条说说的正文")
    p.qzone_seen.mark_seen("detailfeed0001", "2026-09-01T10:00:00", "qzone_detailfeed_9")

    result = asyncio.run(p.view_friend_feed_detail(feed_id="detailfeed0001"))
    assert isinstance(result, dict)
    content = result["content"]
    assert "小明(QQ:100)的说说详情" in content
    assert "这条说说的正文" in content
    assert "评论区(1条):" in content and "小红(20000):第一条评论" in content
    assert "〔评论ID=dc1〕" in content  # reply 锚
    assert content.endswith("〔说说ID=detailfeed00〕")  # tid 前 12 位锚
    assert result["content_items"] and result["content_items"][0]["mime_type"] == "image/jpeg"
    # Q10=B:查看即已见(浏览轮询 is_new_candidate 转 False)
    assert p.qzone_seen.is_new_candidate("detailfeed0001") is False
    # 复审修复:detail 查看传 None 只置 seen,reply 段锚(注入消息 id)保留
    assert p.qzone_seen.get_message_id("detailfeed0001") == "qzone_detailfeed_9"
    # Q6:评论级锚登记
    ctx = p._qzone_registry.resolve("detailfeed0001")
    assert ctx.comment_map == {"dc1": ("20000", "小红")}


def test_view_friend_feed_detail_multi_image_composes_single_item(tmp_path):
    """详情工具多图接入(C 方案,2026-09-03):与列表工具同款——双图拼成
    一张角标合成图,恒单 content_item(mime 恒 jpeg);锚单条「图1-图2(拼接,…)」。"""

    from catsitate_core.qzone.imaging import compose_numbered_grid

    p = _make_plugin(tmp_path)
    p1, p2 = _png_bytes((200, 30, 30)), _png_bytes((30, 30, 200))
    detail_feed = FeedItem(
        tid="mdetail00001", abstime="1750000000", uin="100", nickname="小明",
        content="详情双图", image_urls=["u1", "u2"],
    )

    async def get_user_feeds(*, target_uin, nickname, num=20, page=1):
        return [detail_feed]

    async def download(url):
        return {"u1": p1, "u2": p2}[url]

    p.qzone_client.get_user_feeds = get_user_feeds
    p.qzone_client.download_image = download

    result = asyncio.run(p.view_friend_feed_detail(feed_id="mdetail00001", qq="100"))
    assert isinstance(result, dict)
    composite = compose_numbered_grid([(1, p1), (2, p2)])
    assert result["content_items"] == [{
        "content_type": "image",
        "data": base64.b64encode(composite).decode("ascii"),
        "mime_type": "image/jpeg",
    }]
    tag = f"图1-图2(拼接,hash={hashlib.sha256(composite).hexdigest()[:8]})"
    assert tag in result["content"]
    assert "图1(" not in result["content"]
    # 评论区/锚行结构不回退:既有锚与评论提示原样
    assert "评论区:还没有评论。" in result["content"]
    assert result["content"].endswith("〔说说ID=mdetail00001〕")


def test_view_friend_feed_detail_requires_owner_when_unresolved(tmp_path):
    """Q12=A 锚过期形态:registry/seen 都解析不到主人且未传 qq → 显式索要,
    不猜测。"""

    p = _make_plugin(tmp_path)
    res = asyncio.run(p.view_friend_feed_detail(feed_id="unknownfeed99"))
    assert "请带上好友的QQ号" in res


def test_qzone_reply_via_comment_map_targets_comment_author(tmp_path):
    """Q6/Q11:真实流(无通知上下文)经 comment_map 命中——主评论作者=二元组
    comment_uin、@ 目标=该评论作者;map 无命中则显式拒绝+指引(零写调用)。"""

    from catsitate_core.qzone.wire import FeedComment

    p = _make_tool_plugin(tmp_path)
    _register_feed(p, tid="feedM", owner="10001",
                   comment_map={"cm1": ("20000", "小红")})
    res = asyncio.run(p.qzone_reply(feed_id="feedM", comment_id="cm1",
                                    content="回你!", stream_id="real-chat"))
    assert res.startswith("回复成功,已回复 小红")  # @ 目标=评论作者(昵称)
    assert p.qzone_client.reply_calls[0][3] == "20000"  # comment_uin=主评论作者
    assert p.qzone_client.reply_calls[0][6] == "20000"  # at_uin

    _register_feed(p, tid="feedN", owner="10001")  # 无评论锚
    res2 = asyncio.run(p.qzone_reply(feed_id="feedN", comment_id="gone1",
                                     content="x", stream_id="real-chat"))
    assert "未找到这条评论" in res2 and "view_friend_feed_detail" in res2


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
    自然文本一致(比「昵称:摘要」键值对更像转述见闻);摘要截 100 字(2026-09-02 用户裁定,原 20),纯图说说
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
    assert text == "[空间] 近期刷到: 小明发了「今天去公园散步拍了好多照片晚霞很好看心情也很好」;10002发了「图片」"


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
    assert "fav_events_day" not in src  # 死代码删除防回归(2026-09-04,素材改 fav_events_window)


def test_digest_fav_events_24h_rolling_window_catches_last_night(tmp_path):
    """见闻互动素材近 24h 滚动窗(2026-09-04 翻案 H-2 自然日旧裁定):旧代码按
    生成时刻自然日查(day=今天),零点前登记的事件 day=昨日取不到——跨零点会话
    互动素材全空;新代码 fav_events_window(now-24h) 滚动窗取到。压缩模拟
    「零点前登记、零点后生成」(day=昨日+created_at=now-1h),任何时刻跑都确定。
    另插 now-25h 陈年事件断言不进素材——锁 24h 上界,防 since 放宽回归。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    at = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.store.execute(
        "INSERT INTO qzone_fav_events (day, user_id, kind, text, created_at) "
        "VALUES (?, '10001', 'COMMENT', '昨夜评论了你的说说:好看', ?)", (yday, at))
    # 超窗陈年事件(day/created_at 同取 25h 前,行内自洽):不得混进素材
    old_day = (now - timedelta(hours=25)).strftime("%Y-%m-%d")
    old_at = (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.store.execute(
        "INSERT INTO qzone_fav_events (day, user_id, kind, text, created_at) "
        "VALUES (?, '10001', 'COMMENT', '前夜陈年评论:不该进素材', ?)", (old_day, old_at))

    captured: list = []

    async def llm(messages, model, module, timeout_ms=None):
        captured.append("\n".join(str(m.get("content", "")) for m in messages))
        return {"success": True, "response": "昨晚收到评论,今天又刷到新动态。"}

    p._side_llm_call = llm
    # 人设缓存预置(离线桩无 ctx.config,不走异常兜底路径;实例级覆盖防测试间泄漏)
    p._persona_cache = "猫耳少女"
    p._style_cache = ""
    asyncio.run(p._qzone_generate_digest())
    # day=昨日的事件仍在 24h 滚动窗内,进素材段(旧自然日口径查不到);
    # now-25h 的陈年事件超窗,不进素材(锁 24h 上界)
    assert "昨夜评论了你的说说:好看" in "\n".join(captured)
    assert "前夜陈年评论:不该进素材" not in "\n".join(captured)
    assert p._qzone_digest_snapshot.load().get("date") == datetime.now().strftime("%Y-%m-%d")


def test_digest_fav_events_truncated_to_latest_10(tmp_path):
    """见闻互动素材截断统一保留最新(2026-09-04):升序取尾 events[-10:](与结算
    路径 events[-5:] 同款)——12 条窗内事件只保留最新 10 条,最早的 2 条截掉,
    不再「丢掉下午互动的」。created_at 自 now-30min 起每条 +1 分钟递增(全部
    落在 24h 窗内,升序可分辨)。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    for i in range(1, 13):
        at = (now - timedelta(minutes=30) + timedelta(minutes=i - 1)).strftime("%Y-%m-%dT%H:%M:%S")
        p.qzone_comment_seen.store.execute(
            "INSERT INTO qzone_fav_events (day, user_id, kind, text, created_at) "
            "VALUES (?, '10001', 'LIKE', ?, ?)",
            (now.strftime("%Y-%m-%d"), f"事件{i:02d}", at),
        )

    captured: list = []

    async def llm(messages, model, module, timeout_ms=None):
        captured.append("\n".join(str(m.get("content", "")) for m in messages))
        return {"success": True, "response": "今天和好多人互动了。"}

    p._side_llm_call = llm
    p._persona_cache = "猫耳少女"
    p._style_cache = ""
    asyncio.run(p._qzone_generate_digest())
    material = "\n".join(captured)
    assert all(f"事件{i:02d}" in material for i in range(3, 13))  # 最新 10 条全保留
    assert "事件01" not in material and "事件02" not in material  # 最早的 2 条截掉


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
            return {}, {}, []

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
            return {}, {}, []

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
    assert "登录态失效且 cookie 重取失败" in res  # 同轮自愈(2026-09-02 #7)
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


def test_send_trigger_intent_text_no_stray_paren():
    """2026-09-03 复审小修:发布触发 intent 文案两处删多余右括号
    (「…直接写你想发的内容);」→「…直接写你想发的内容;」,browsed/非 browsed
    各一处,轻量源码断言)。"""
    import inspect

    import plugin as plugin_mod

    src = inspect.getsource(plugin_mod)
    assert "内容);" not in src  # 残留右括号已删
    assert src.count("直接写你想发的内容;") == 2  # 两处文案齐全


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
        async def get_unified_timeline(self, *, count=20, begintime=None):
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
        async def get_unified_timeline(self, *, count=20, begintime=None):
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


def test_update_schedule_coerces_string_params(tmp_path):
    """入参形态矫正:模型经宿主传参不做类型矫正,字符串 "false" 不得被裸
    bool() 误判为 True;字符串序号转整数;真值字符串("true"/"1")按真处理。"""

    p = _make_plugin(tmp_path)
    p.config.plugin.enabled = True
    p._schedule_edit_history = []
    p._ctx.paths = type("_Paths", (), {"data_dir": tmp_path})()
    p._schedule_generated = False
    now = datetime.now()
    day, nxt = now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")
    p._schedule_data = {"date": day, "windows": [
        {"kind": "daily", "start": f"{day}T08:00", "end": f"{day}T09:00",
         "activity": "闲逛", "plan_speak": False, "topic": ""},
        {"kind": "sleep", "start": f"{day}T23:00", "end": f"{nxt}T07:30", "activity": ""},
    ]}
    # 字符串 "false":不落标记(裸 bool() 会误判 True)
    res = asyncio.run(p.update_schedule(action="add", start="10:00", end="11:00",
                                         activity="看看书", read_qzone="false", send_qzone="false",
                                         stream_id="s1", user_id="10001"))
    assert "日程已更新" in res
    w = next(w for w in p._schedule_data["windows"] if w.get("activity") == "看看书")
    assert "read_qzone" not in w and "send_qzone" not in w
    # 字符串 "true"/"1":按真落标记
    res = asyncio.run(p.update_schedule(action="add", start="20:00", end="21:00",
                                         activity="刷刷空间", read_qzone="true", send_qzone="1",
                                         stream_id="s1", user_id="10001"))
    assert "日程已更新" in res
    w = next(w for w in p._schedule_data["windows"] if w.get("activity") == "刷刷空间")
    assert w.get("read_qzone") is True and w.get("send_qzone") is True
    # 字符串序号:delete 走 int 矫正
    res = asyncio.run(p.update_schedule(action="delete", window_index="1",
                                         stream_id="s1", user_id="10001"))
    assert "日程已更新" in res
    assert all(w.get("activity") != "看看书" for w in p._schedule_data["windows"])


def test_update_schedule_add_qzone_window(tmp_path):
    """update_schedule 工具适配 QQ空间窗口字段:add 传 read_qzone/send_qzone
    落进新窗口,view 文本带「(刷空间)/(发说说)」标注。"""

    p = _make_plugin(tmp_path)
    p.config.plugin.enabled = True  # 工具首行门控(离线装配默认关)
    p._schedule_edit_history = []  # on_load 装配的编辑历史(离线补)
    p._ctx.paths = type("_Paths", (), {"data_dir": tmp_path})()  # 成功路径落盘 schedule.json 用
    p._schedule_generated = False  # on_load 装配的生成标记(落盘字段,离线补)
    now = datetime.now()
    day, nxt = now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")
    p._schedule_data = {"date": day, "windows": [
        {"kind": "daily", "start": f"{day}T08:00", "end": f"{day}T09:00",
         "activity": "闲逛", "plan_speak": False, "topic": ""},
        {"kind": "sleep", "start": f"{day}T23:00", "end": f"{nxt}T07:30", "activity": ""},
    ]}
    res = asyncio.run(p.update_schedule(action="add", start="20:00", end="21:00",
                                         activity="刷刷空间", read_qzone=True, send_qzone=True,
                                         stream_id="s1", user_id="10001"))
    assert "日程已更新" in res
    w = next(w for w in p._schedule_data["windows"] if w.get("activity") == "刷刷空间")
    assert w.get("read_qzone") is True and w.get("send_qzone") is True
    view = asyncio.run(p.update_schedule(action="view"))
    assert "(刷空间)" in view and "(发说说)" in view


def test_poll_feeds_spacing_governs_fetch_rhythm(tmp_path):
    """拉取间距语义(窗口开始即首拉,间隔=两次拉取的间距而非与窗口无关的
    固定节奏):距上次拉取不足 poll_interval_minutes 的轮次跳过发现/充实
    (窗口激活与收窗判定照常);足距后恢复拉取。缓存新鲜时足距轮同样零
    端点调用(发现层统一入口共享缓存语义),缓存失效后足距轮真实重拉。"""

    from time import monotonic

    p = _make_plugin(tmp_path)
    now = datetime.now()
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "read_qzone": True,
    }]}
    asyncio.run(p._qzone_poll_feeds())  # 首轮(时间戳默认 0=无距可判):实际拉取
    assert p.qzone_client.discovery_calls == 1
    asyncio.run(p._qzone_poll_feeds())  # 距上次不足间隔:跳过拉取段
    assert p.qzone_client.discovery_calls == 1
    assert p.qzone_injector.window_active is True  # 窗口激活不受间距影响
    p._qzone_last_fetch_at = monotonic() - 16 * 60  # 足距(默认间隔 15 分钟)
    p._qzone_discovery_cache = None  # 共享缓存失效:足距轮必须真实重拉(命中会免请求)
    asyncio.run(p._qzone_poll_feeds())
    assert p.qzone_client.discovery_calls == 2
    # 缓存新鲜(600 秒内,上一轮重拉已写入):足距轮吃共享缓存,零端点调用
    p._qzone_last_fetch_at = monotonic() - 16 * 60
    asyncio.run(p._qzone_poll_feeds())
    assert p.qzone_client.discovery_calls == 2


def test_schedule_tick_dispatches_poll_on_qzone_window_entry(tmp_path):
    """qzone 窗口开始即首拉:_schedule_tick 检出进入 read/send 窗口时立即
    派发一轮拉取(经 _qzone_poll_tick),同窗口只派发一次;非 qzone 窗口
    不派发。"""

    p = _make_plugin(tmp_path)
    p.config.plugin.enabled = True  # 离线装配默认关:schedule_tick 首行门控
    p.sleep = _SleepStub(False)  # schedule_tick 无条件查睡眠状态(on_load 装配,离线补)
    p._schedule_tick_fired = {}  # on_load 装配的窗口触发/计数标记(离线补)
    p._speak_counts = {}
    p._remind_fired = {}
    now = datetime.now()
    qzone_win = {"kind": "daily",
                 "start": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                 "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                 "activity": "闲逛", "plan_speak": False, "topic": "", "read_qzone": True}
    plain_win = {**qzone_win, "read_qzone": False}
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [qzone_win]}

    calls: list = []

    async def _tick():
        calls.append(1)

    async def _noop_trigger(*a, **k):
        return None

    p._qzone_poll_tick = _tick
    p._window_trigger = _noop_trigger  # 隔离:窗口主动发言路径与拉取派发无关
    asyncio.run(p._schedule_tick())
    assert calls == [1]
    asyncio.run(p._schedule_tick())  # 同窗口已触发过:不重复派发
    assert calls == [1]
    # 非 qzone 窗口:不派发
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [plain_win]}
    p._schedule_tick_fired = {}  # 换窗场景:重置触发标记让 tick 真正评估新窗口
    asyncio.run(p._schedule_tick())
    assert calls == [1]


def test_poll_tick_closes_ended_window_while_previous_poll_running(tmp_path):
    """窗口收尾不被上一轮拉取拖住(2026-09-04):窗口已结束且上一轮后台拉取
    仍在跑(_qzone_poll_running=True)时,tick 防重入分支先行收窗——
    window_ended 已执行、浏览队列清空、queued 行回退未读,无需等 poll_feeds
    完成;tick 早退不清 running 标记(清位仍在 feeds 的 finally)。"""

    p = _make_plugin(tmp_path)
    now = datetime.now()
    # 日程窗口已结束(1 小时前收尾):窗口判定=非浏览窗口
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "daily", "start": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "逛空间", "plan_speak": False, "topic": "", "read_qzone": True,
    }]}
    p.qzone_injector.window_started()  # 上一轮窗口遗留的浏览态(未收窗)
    p.qzone_seen.mark_queued("tailtid0001", abstime="1", author_uin="10001", summary="窗口尾未读动态")
    p.qzone_injector.enqueue([FeedItem(
        tid="tailtid0001", abstime="1750000000", uin="10001", nickname="小明", content="窗口尾动态",
    )])
    assert p.qzone_injector.stats()["p2_queued"] == 1

    p._qzone_poll_running = True  # 上一轮后台拉取还在跑(tick 走防重入分支)
    asyncio.run(p._qzone_poll_tick())

    # 收窗不等上一轮拉取完成:窗口已关、浏览队列清空、queued 行回退未读(删除)
    assert p.qzone_injector.window_active is False
    assert p.qzone_injector.stats()["p2_queued"] == 0
    assert p.store.query("SELECT COUNT(*) FROM qzone_feeds WHERE tid = 'tailtid0001'")[0][0] == 0
    assert any(
        level == "info" and "QQ空间浏览窗口结束,浏览队列回退未读" in str(a[0]) for level, a in p.logs
    )
    assert p._qzone_poll_running is True  # tick 早退不清标记(防重入语义不变)


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


def test_pump_registers_content_summary_and_comment_map(tmp_path):
    """M3-r2 表达生成层素材:泵登记 FeedContext 带 content_summary(说说
    全文)与 comment_map(结构化评论的评论级锚,Q6)
    ——qzone_comment 生成正文时的场景素材;纯图说说以「(无文字)」占位。"""

    import time as _time

    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    p.qzone_seen.mark_queued("t_cs", abstime="1750000000", author_uin="10001", summary="正文")
    from catsitate_core.qzone.wire import FeedComment

    p.qzone_injector.enqueue([FeedItem(
        tid="t_cs", abstime="1750000000", uin="10001", nickname="小明", content="今天去了海边玩,晒黑了",
        comments=[FeedComment(comment_tid="ct1", uin="20000", nickname="小红", content="羡慕", create_time=""),
                  FeedComment(comment_tid="ct2", uin="30000", nickname="小刚", content="哈哈", create_time="")],
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
    assert ctx.comment_map == {"ct1": ("20000", "小红"), "ct2": ("30000", "小刚")}  # 评论级锚
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
        async def get_unified_timeline(self, *, count=20, begintime=None):
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


def test_poll_feeds_rate_limit_skips_round_without_legacy_fallback(tmp_path):
    """发现层 -10001(服务端限流)→ 告警跳过本轮,不回退 legacy 逐好友路径
    (限流期间 1→N+1 请求放大等于火上浇油);窗口状态保持,下轮再试。"""

    from catsitate_core.qzone.client import QzoneRateLimitError

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()

    class _RateLimitedDiscoveryClient:
        def __init__(self):
            self.feeds_calls = 0

        async def get_unified_timeline(self, *, count=20, begintime=None):
            raise QzoneRateLimitError("空间服务限流(统一时间线): code=-10001")

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            self.feeds_calls += 1
            return []

    client = _RateLimitedDiscoveryClient()
    p.qzone_client = client
    asyncio.run(p._qzone_poll_feeds())
    assert client.feeds_calls == 0  # 未回退 legacy(零放大)
    assert any(
        level == "warning" and "服务限流" in str(a[0]) and "本轮浏览跳过" in str(a[0])
        for level, a in p.logs
    )
    assert p.qzone_injector.window_active is True  # 窗口保持(非收窗)


def test_shared_discovery_rate_limit_backoff(tmp_path):
    """发现层限流退避(浏览与通知源B 共享):首页撞 -10001 → 浏览层原文案告警
    跳过本轮、源B 扫描零异常零调用;退避期内(30 分钟)再跑浏览与源B 零端点
    请求且退避告警只一条;退避过期后恢复真实拉取,info「退避结束」且退避态
    复位(再遇限流会重新告警)。"""

    from time import monotonic

    from catsitate_core.qzone.client import QzoneRateLimitError

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    # 源B 名单非空(名单先行判定通过,会走到发现层统一入口)
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "我的评论", fresh)

    class _RecoverableRateLimitClient(_StubUnifiedClient):
        """首页可切换限流/恢复的桩:limited=True 恒抛限流,False 返回正常空列表。"""

        def __init__(self):
            super().__init__([])
            self.limited = True

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}, []

        async def get_unified_timeline(self, *, count=20, begintime=None):
            del count, begintime
            self.discovery_calls += 1
            if self.limited:
                raise QzoneRateLimitError("空间服务限流(统一时间线): code=-10001")
            return [], ""

    client = _RecoverableRateLimitClient()
    p.qzone_client = client

    from time import monotonic as _mono

    # 第 1 轮浏览:撞限流 → 浏览层原文案告警(不回退 legacy),退避进入告警 1 条
    asyncio.run(p._qzone_poll_feeds())
    assert any(
        level == "warning" and "服务限流" in str(a[0]) and "本轮浏览跳过" in str(a[0])
        for level, a in p.logs
    )
    assert any(
        level == "warning" and "30 分钟退避" in str(a[0]) and "期间零请求" in str(a[0])
        for level, a in p.logs
    )
    assert client.discovery_calls == 1

    # 第 1 轮源B:退避期内静默跳过,零异常零端点调用,源C 照常
    asyncio.run(p._qzone_notify_scan())
    assert client.discovery_calls == 1
    assert not any(level == "exception" for level, a in p.logs)
    assert any(
        level == "debug" and "通知源B发现层限流退避中" in str(a[0]) for level, a in p.logs
    )

    # 退避期内再跑一轮浏览+源B:零端点请求,退避告警仍只 1 条(warn-once)
    p._qzone_last_fetch_at = _mono() - 16 * 60
    asyncio.run(p._qzone_poll_feeds())
    asyncio.run(p._qzone_notify_scan())
    assert client.discovery_calls == 1  # 退避期内零请求
    assert sum(
        1 for level, a in p.logs
        if level == "warning" and "30 分钟退避" in str(a[0])
    ) == 1

    # 退避过期+桩恢复:恢复真实拉取,info「退避结束」且退避态复位
    p._qzone_last_fetch_at = _mono() - 16 * 60
    p._qzone_discovery_backoff_until = monotonic() - 1
    client.limited = False
    asyncio.run(p._qzone_poll_feeds())
    assert client.discovery_calls == 2
    assert any(
        level == "info" and "限流退避结束,恢复拉取" in str(a[0]) for level, a in p.logs
    )
    assert p._qzone_discovery_backoff_until == 0.0
    assert p._qzone_discovery_backoff_warned is False

    # 恢复后源B:吃共享缓存,零端点调用
    asyncio.run(p._qzone_notify_scan())
    assert client.discovery_calls == 2


def test_notify_scan_source_b_uses_shared_discovery_cache(tmp_path, monkeypatch):
    """通知源B 经发现层统一入口:连续两轮通知扫描共用一次首页请求——第二轮
    吃共享缓存(600 秒内)零端点调用,逐好友拉取两轮照常(缓存共享不改变
    源B 的交叉与拉取语义)。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p.qzone_injector.window_started()
    # bot 曾在好友 30000 说说下评论:名单非空,源B 走发现层交叉
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", fresh)

    pulls: list = []
    now_s = str(int(_time.time()))

    class _CacheShareClient(_StubUnifiedClient):
        """源A 空;发现层含被评论好友 30000(有新活动);源B 逐好友拉取记录。"""

        def __init__(self):
            super().__init__([
                FeedDiscovery(tid="fa1", uin="30000", nickname="好友甲", abstime=now_s, appid=311),
            ])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}, []

        async def get_user_feeds_raw(self, *, target_uin, num=5):
            del num
            pulls.append(target_uin)
            return {"usrinfo": {"uin": target_uin}, "msglist": []}

    p.qzone_client = _CacheShareClient()
    asyncio.run(p._qzone_notify_scan())
    asyncio.run(p._qzone_notify_scan())  # 第二轮:缓存新鲜,首页零端点调用
    assert p.qzone_client.discovery_calls == 1
    assert pulls == ["30000", "30000"]  # 逐好友拉取两轮照常
    assert sleeps == [2.0, 2.0]  # 每轮活跃好友前各 2 秒


def test_poll_feeds_uses_fresh_cache_without_refetch(tmp_path):
    """浏览层吃共享缓存:通知源B 扫描先经统一入口拉首页写缓存(600 秒内),
    随后浏览轮(足距)零端点调用——首页列表来自缓存,旧动态与非说说条目
    过滤照常(不误注入),窗口与泵链路照常。"""

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    # 源B 名单(30000)与发现层作者(10001)无交集:源B 零逐好友拉取
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", fresh)
    # 旧说说预登记为已见(queued 会被窗口开始的回收影响,seen 才是判重基准)
    p.qzone_seen.mark_queued("oldtid", abstime="1750000000", author_uin="10001", summary="旧动态")
    p.qzone_seen.mark_seen("oldtid", "2026-08-31T10:00:00")

    class _CacheFirstClient(_StubUnifiedClient):
        """源A 空;首页含旧说说+非说说(浏览流只认 appid=311 的新 tid)。"""

        def __init__(self):
            super().__init__([
                FeedDiscovery(tid="oldtid", uin="10001", nickname="小明", abstime="1750000000", appid=311),
                FeedDiscovery(tid="shareid", uin="10001", nickname="小明", abstime="1750000100", appid=2023106),
            ])

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}, []

    p.qzone_client = _CacheFirstClient()
    asyncio.run(p._qzone_notify_scan())  # 源B 扫描:经统一入口拉首页写缓存
    assert p.qzone_client.discovery_calls == 1
    asyncio.run(p._qzone_poll_feeds())  # 浏览轮:缓存新鲜,首页零端点调用
    assert p.qzone_client.discovery_calls == 1  # 未重拉(吃共享缓存)
    assert p._ctx.gateway.calls == []  # 旧动态/非说说过滤照常,零注入
    assert p.qzone_injector.window_active is True  # 窗口照常激活


def test_poll_feeds_pagination_rate_limit_enters_backoff_without_legacy(tmp_path, monkeypatch):
    """回归(v1.0.3 引入):翻页穿透路径撞限流必须进入共享退避并终止本轮,
    绝不能落进 legacy 回退(1→N+1 请求放大);首页正常、第二页限流——
    发现层终止早于充实阶段(零登记),退避期内后续发现层消费零请求。"""

    from catsitate_core.qzone.client import QzoneRateLimitError
    from time import monotonic as _monotonic

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    now_s = str(int(_monotonic()))
    prev_s = str(int(_monotonic()) - 10)

    class _SecondPageRateLimitedClient(_StubUnifiedClient):
        def __init__(self):
            super().__init__([
                FeedDiscovery(tid="p1", uin="10001", nickname="小明", abstime=now_s, appid=311),
                FeedDiscovery(tid="p2", uin="10002", nickname="小红", abstime=prev_s, appid=311),
            ])
            self.calls: list = []

        async def get_unified_timeline(self, *, count=20, begintime=None):
            self.calls.append(begintime)
            if begintime is None:
                return list(self._discoveries), "cur1"
            raise QzoneRateLimitError("空间服务限流(统一时间线): code=-10001")

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            if target_uin == "10001":
                return [FeedItem(tid="p1", abstime=now_s, uin="10001", nickname=nickname, content="新动态A")]
            return []

    client = _SecondPageRateLimitedClient()
    p.qzone_client = client
    asyncio.run(p._qzone_poll_feeds())

    assert any(
        level == "warning" and "服务限流" in str(a[0]) and "翻页" in str(a[0])
        for level, a in p.logs
    )
    assert not any(
        level == "exception" and "回退逐好友旧路径" in str(a[0]) for level, a in p.logs
    )
    # 终止发生在发现层阶段(充实层未运行):两页动态均未登记,下轮缓存过期后重拉
    assert p.qzone_seen.is_new_candidate("p1") is True
    assert p.qzone_seen.is_new_candidate("p2") is True
    assert p._qzone_discovery_backoff_until > _monotonic()  # 已进入共享退避
    assert any(
        level == "warning" and "进入 30 分钟退避" in str(a[0]) for level, a in p.logs
    )
    # 退避期内源B 与浏览的发现层消费零请求
    calls_after = len(client.calls)
    asyncio.run(p._qzone_poll_feeds())
    assert len(client.calls) == calls_after  # 浏览层零新调用(退避静默)


def test_shared_discovery_pagination_passes_through(tmp_path, monkeypatch):
    """缓存新鲜时浏览层积压补全仍穿透直发:通知源B 扫描先写共享缓存(首页含
    新说说),浏览轮吃缓存发现新动态 → 带游标的第二页调用真实发生(直调
    client,不经共享层),新动态充实注入照常。"""

    import time as _time

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    p.qzone_injector.window_started()
    fresh = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    p.qzone_comment_seen.note_bot_comment("ffeed1", "30000", "评论一", fresh)  # 与作者 10001 无交集

    enrich_calls: list = []
    now_s = str(int(_time.time()))

    class _PassThroughClient(_StubUnifiedClient):
        """首页(begintime=None)含新说说并记录每次调用的游标;第二页起空页。"""

        def __init__(self):
            super().__init__([
                FeedDiscovery(tid="newtid", uin="10001", nickname="小明", abstime=now_s, appid=311),
            ])
            self.timeline_begintimes: list = []

        async def get_own_feed_comments(self, *, bot_uin, num=10):
            del bot_uin, num
            return {}, {}, []

        async def get_unified_timeline(self, *, count=20, begintime=None):
            del count
            self.discovery_calls += 1
            self.timeline_begintimes.append(begintime)
            return (list(self._discoveries), "cur1") if begintime is None else ([], "")

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            enrich_calls.append(target_uin)
            return [FeedItem(tid="newtid", abstime=now_s, uin=target_uin, nickname=nickname, content="新动态正文")]

    p.qzone_client = _PassThroughClient()
    asyncio.run(p._qzone_notify_scan())  # 源B 扫描:首页调用(无游标)写缓存
    assert p.qzone_client.discovery_calls == 1
    asyncio.run(p._qzone_poll_feeds())  # 浏览轮:吃缓存发现新动态 → 穿透翻页
    assert p.qzone_client.discovery_calls == 2
    assert p.qzone_client.timeline_begintimes == [None, "cur1"]  # 第二页带游标直发(穿透)
    assert sleeps == [2.0]  # 页间防风控间隔(首页吃缓存前无间隔)
    assert enrich_calls == ["10001"]  # 充实层照常
    assert [c[1]["message_id"] for c in p._ctx.gateway.calls] == ["qzone_newtid_1"]  # 注入照常


def test_poll_feeds_enrichment_rate_limit_stops_round(tmp_path):
    """充实层撞限流 → 终止本轮(不再逐作者重试加重风控);先前已入队作者保留,
    后续作者零调用(区分「终止本轮」与「单好友跳过 continue」)。"""

    from catsitate_core.qzone.client import QzoneRateLimitError

    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()

    class _EnrichRateLimitedClient:
        def __init__(self):
            self.enrich_calls: list[str] = []

        async def get_unified_timeline(self, *, count=20, begintime=None):
            return (
                [FeedDiscovery(tid="d1", uin="10001", nickname="小明", abstime="300", appid=311),
                 FeedDiscovery(tid="d2", uin="10002", nickname="小红", abstime="200", appid=311),
                 FeedDiscovery(tid="d3", uin="10003", nickname="小蓝", abstime="100", appid=311)],
                "",
            )

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            self.enrich_calls.append(target_uin)
            if target_uin == "10001":
                return [FeedItem(tid="d1", abstime="300", uin="10001", nickname=nickname, content="新动态A")]
            raise QzoneRateLimitError("空间服务限流(uin=10002): code=-10001")

    client = _EnrichRateLimitedClient()
    p.qzone_client = client
    asyncio.run(p._qzone_poll_feeds())
    assert client.enrich_calls == ["10001", "10002"]  # 第二作者撞限流,第三作者零调用(终止而非跳过)
    assert any(
        level == "warning" and "服务限流" in str(a[0]) and "充实终止" in str(a[0])
        for level, a in p.logs
    )
    assert p.qzone_seen.is_new_candidate("d2") is True  # 第二作者未被充实(本轮终止)
    assert p.qzone_seen.is_new_candidate("d3") is True
    assert p.qzone_seen.is_new_candidate("d1") is False  # 已入队作者保留


def test_notify_scan_source_a_rate_limit_keeps_source_b_c(tmp_path):
    """通知源A限流 → 源A本轮空,但源B/C 照常执行(限流不得阻断后续源)。"""

    from catsitate_core.qzone.client import QzoneRateLimitError

    p = _make_plugin(tmp_path)

    class _RateLimitedSourceAClient(_StubUnifiedClient):
        async def get_own_feed_comments(self, *, bot_uin, num=10):
            raise QzoneRateLimitError("空间服务限流(uin=x): code=-10001")

    p.qzone_client = _RateLimitedSourceAClient([])
    like_events: list = []

    async def _likes(*, count=30):
        like_events.append(count)
        return []

    p.qzone_client.get_like_events = _likes
    asyncio.run(p._qzone_notify_scan())
    assert like_events  # 源C 照常执行(未被源A阻断)
    assert any(
        level == "warning" and "源A限流" in str(a[0]) and "源B/C 照常" in str(a[0])
        for level, a in p.logs
    )


def test_qzone_like_rate_limit_receipt(tmp_path):
    """写路径 -10001:回执明示「稍后再试」(对齐 -10049 的限制语义风格)。"""

    from catsitate_core.qzone.client import BIZ_CODE_SERVER_BUSY, QzoneBizError

    p = _make_plugin(tmp_path)
    _register_feed(p, tid="busytid01", owner="10001")

    class _BusyBizError(QzoneBizError):
        def __init__(self):
            super().__init__(-10001, "network busy")

    async def _busy(*, fid, target_qq):
        raise _BusyBizError()

    p.qzone_client.do_like = _busy
    res = asyncio.run(p.qzone_like(feed_id="busytid01", stream_id="s1", user_id="10001"))
    assert "有点忙" in res and "稍后再试" in res
    assert BIZ_CODE_SERVER_BUSY == -10001


# ---- M3 终审修复波:I1(浏览流bot自我排除)+ I2(发现层登录态失效行为) ----


class _StubCookie:
    """cookie 管理桩:记录 invalidate;get 模拟 adapter 重取(默认 None=重取失败,
    同轮自愈链断言用,2026-09-02 #7)。"""

    def __init__(self):
        self.invalidate_calls = 0
        self.get_result = None

    def invalidate(self):
        self.invalidate_calls += 1

    async def get(self):
        return self.get_result


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
        async def get_unified_timeline(self, *, count=20, begintime=None):
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
            ]}, {"feed1": "今天的心情"}, []

        async def get_unified_timeline(self, *, count=20, begintime=None):
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
            ]}, {"feed1": "今天的心情"}, []

        async def get_unified_timeline(self, *, count=20, begintime=None):
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
            return {}, {}, []

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

    async def page(*, count, begintime=None):
        calls.append((count, begintime))
        if begintime is None:
            return [_disc("t1", "100", "300"), _disc("t2", "101", "200")], "cur1"  # t1/t2 已登记
        return [_disc("t3", "102", "100")], "cur2"

    p.qzone_client.get_unified_timeline = page
    p.qzone_seen.mark_queued("t1", abstime="300", author_uin="100", summary="a")
    p.qzone_seen.mark_queued("t2", abstime="200", author_uin="101", summary="b")
    asyncio.run(p._qzone_poll_feeds())
    # 第 1 页全部已见(更早页只会更旧),翻页止步:恰 1 次调用,页大小=配置值
    assert calls == [(50, None)]


def test_discovery_pagination_stops_when_second_page_all_seen(tmp_path, monkeypatch):
    """Task5:第 1 页含新动态 → 翻第 2 页;第 2 页全旧 → 止步(不再第 3 页)。
    页间有 2 秒防风控间隔(与充实层/通知源B 好友间隔同款),首页前无间隔。"""

    sleeps: list = []
    _patch_sleep(monkeypatch, sleeps)
    p = _make_plugin(tmp_path)
    p._schedule_data = _active_qzone_schedule()
    p.qzone_injector.window_started()
    p.qzone_seen.mark_queued("t2", abstime="200", author_uin="101", summary="旧")
    p.qzone_seen.mark_queued("t3", abstime="100", author_uin="102", summary="旧")
    calls = []

    class _PagedClient(_StubUnifiedClient):
        async def get_unified_timeline(self, *, count=20, begintime=None):
            calls.append(begintime)
            if begintime is None:
                return [_disc("t1", "100", "300"), _disc("t2", "101", "200")], "cur1"
            return [_disc("t3", "102", "100")], "cur2"  # 第 2 页全旧

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            return []  # 充实页空:聚焦翻页调用序列

    p.qzone_client = _PagedClient([])
    asyncio.run(p._qzone_poll_feeds())
    assert calls == [None, "cur1"]  # 第 2 页无新说说即止步
    # 单一新动态作者:仅页间 1 次 2.0(翻第 2 页的页间隔,首页前无);充实层
    # 好友间隔在循环开头(首作者前不多睡),单作者场景不产生
    assert sleeps == [2.0]


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
        async def get_unified_timeline(self, *, count=20, begintime=None):
            calls.append((count, begintime))
            # 每页一条不同作者的新动态:has_new 恒真,翻页直到配置上限
            page_no = len(calls) - 1
            return [_disc(f"t{page_no}", f"1{page_no:03d}", str(300 - page_no))], f"cur{page_no + 1}"

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            return []  # 充实页空:不引入注入链

    p.qzone_client = _BacklogClient([])
    asyncio.run(p._qzone_poll_feeds())
    # 默认上限 3 页止步(即便第 3 页仍有新动态);两页积压场景由上一用例覆盖
    assert calls == [(50, None), (50, "cur1"), (50, "cur2")]


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
        async def get_unified_timeline(self, *, count=20, begintime=None):
            calls.append((count, begintime))
            page_no = len(calls) - 1
            return [_disc(f"t{page_no}", f"1{page_no:03d}", "300")], f"cur{page_no + 1}"  # 恒有新动态

        async def get_user_feeds(self, *, target_uin, nickname, num=5):
            return []

    p.qzone_client = _PagedClient([])
    asyncio.run(p._qzone_poll_feeds())
    assert calls == [(7, None), (7, "cur1")]  # 页大小 7、上限 2 页


# ---- M3-r2 view_friend_feeds 全域查看工具 + inspect_image hash 路径 ----


def test_view_friend_feeds_returns_media_dict(tmp_path):
    """view_friend_feeds 成功路径(全域查看工具,任何聊天流可用):拉好友说说→
    dict 回执(content 文本摘要+content_items 图片媒体项,宿主按 tool result
    media 入 Images 表供 inspect_image hash 路径反查);图标注为 sha256 前 8 位
    (inspect_image 的 image_hash 前缀即来源于此);成功即 registry 登记
    (content_summary=正文;comment_map=评论级锚)。"""

    p = _make_plugin(tmp_path)
    feed = FeedItem(tid="tid1", abstime="1750000000", uin="100", nickname="小明",
                    content="今天天气好", image_urls=["http://img.qpic.cn/a.jpg"],
                    comments=[])  # 结构化评论空:列表工具不消费评论(Q7 后仅 comment_map 用)
    feed_calls: list = []

    async def get_user_feeds(*, target_uin, nickname, num=3, page=1):
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
    assert resolved.content_summary == "今天天气好"
    # count 上限 10(防单次拉爆);非法 QQ 号显式拒绝(零拉取)
    asyncio.run(p.view_friend_feeds(qq="100", count=99))
    assert feed_calls[-1][2] == 10
    assert asyncio.run(p.view_friend_feeds(qq="notnum")) == "请提供好友的QQ号(纯数字)。"
    assert len(feed_calls) == 2


def test_view_friend_feeds_anchor_hash_matches_sent_bytes_after_refit(tmp_path, monkeypatch):
    """I1 修复回归(工具出口端到端,真实压缩阶梯):大图超预算被阶梯重编码后,
    锚 hash == sha256(实际送出的 content_items 字节)——inspect_image 的
    image_hash 前缀反查契约;压缩前 hash 形态必须不再出现。"""

    import io
    import os

    from PIL import Image

    from catsitate_core.qzone import imaging as imaging_mod
    from catsitate_core.qzone.messages import fit_images_to_rpc_budget as real_fit

    def small_budget_fit(images, *, on_drop=None):
        # 小预算注入:512² 噪声 PNG(~770KB)必超,首档(4096,85)即重编码达标
        return real_fit(images, budget_bytes=300 * 1024, on_drop=on_drop)

    monkeypatch.setattr(imaging_mod, "fit_images_to_rpc_budget", small_budget_fit)

    buf = io.BytesIO()
    Image.frombytes("RGB", (512, 512), os.urandom(512 * 512 * 3)).save(buf, format="PNG")
    noise = buf.getvalue()
    assert len(noise) > 300 * 1024  # 前置:确超预算,阶梯必然介入

    p = _make_plugin(tmp_path)

    async def get_user_feeds(*, target_uin, nickname, num=3, page=1):
        return [FeedItem(tid="bigtid00001", abstime="1750000000", uin="100", nickname="小明",
                         content="大图说说", image_urls=["big"])]

    async def download(url):
        return noise

    p.qzone_client.get_user_feeds = get_user_feeds
    p.qzone_client.download_image = download

    result = asyncio.run(p.view_friend_feeds(qq="100"))
    assert isinstance(result, dict)
    sent = base64.b64decode(result["content_items"][0]["data"])
    assert sent != noise  # 阶梯真实重编码(PNG→JPEG),送出字节已变
    assert f"图1({hashlib.sha256(sent).hexdigest()[:8]})" in result["content"]
    # 旧缺陷形态(压缩前字节的 hash)不再出现
    assert f"图1({hashlib.sha256(noise).hexdigest()[:8]})" not in result["content"]


def test_view_friend_feeds_multi_image_composes_single_item(tmp_path):
    """列表工具多图接入(C 方案,2026-09-03):≥2 图拼成一张角标合成图
    → 恒单 content_item(mime 恒 image/jpeg,合成图不再需要魔数探测);锚文案
    单条「图1-图N(拼接,hash=合成图sha256前8)」,不再逐图列 hash。"""

    from catsitate_core.qzone.imaging import compose_numbered_grid

    p = _make_plugin(tmp_path)
    p1, p2 = _png_bytes((200, 30, 30)), _png_bytes((30, 30, 200))

    async def get_user_feeds(*, target_uin, nickname, num=3, page=1):
        return [FeedItem(tid="mtid1", abstime="1750000000", uin="100", nickname="小明",
                         content="双图说说", image_urls=["u1", "u2"])]

    async def download(url):
        return {"u1": p1, "u2": p2}[url]

    p.qzone_client.get_user_feeds = get_user_feeds
    p.qzone_client.download_image = download

    result = asyncio.run(p.view_friend_feeds(qq="100"))
    assert isinstance(result, dict)
    composite = compose_numbered_grid([(1, p1), (2, p2)])
    # 恒单 content_item:合成 JPEG + mime 恒 jpeg(宿主 media 入库形态)
    assert result["content_items"] == [{
        "content_type": "image",
        "data": base64.b64encode(composite).decode("ascii"),
        "mime_type": "image/jpeg",
    }]
    tag = f"图1-图2(拼接,hash={hashlib.sha256(composite).hexdigest()[:8]})"
    assert tag in result["content"]
    assert "图1(" not in result["content"]  # 不再逐图列 hash


def test_inspect_image_by_hash_prefix_skips_message_search(tmp_path, monkeypatch):
    """inspect_image hash 路径(M3-r2):image_hash 给定时跳过消息搜索
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


# ---- M3-r2 说说发布主动触发(分窗口形态)与冷启动种子自举 ----


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
async def test_seed_virtual_session_nickname_failure_returns_false(tmp_path):
    """种子自举同款(2026-09-03 复审修复):构造取昵称抛错纳入注入 try——
    异常被捕获告警并返回 False(日程窗口按「自举失败」跳过重试等下个窗口),
    不沿触发链上抛。"""

    p = _make_tool_plugin(tmp_path)
    p._ctx.config.nickname = ""  # bot.nickname 读取失败/为空形态
    assert await p._qzone_seed_virtual_session() is False
    assert p._ctx.gateway.calls == []  # 未走到注入
    assert any(
        level == "exception" and "种子消息注入失败" in str(a[0]) for level, a in p.logs
    )


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


def test_guard_assembly_rebuilds_on_config_update(tmp_path):
    """终审 H-1 回归:on_config_update(scope=self) 须重建 _guard_compiled——
    开→编译生效/关→置空/改 patterns→新规则替换旧规则,热重载不静默失效。"""
    from catsitate_core.guard import match_guard

    p = _make_plugin(tmp_path)

    # 关→开:装配后列表非空
    p.config.guard.enabled = True
    p.config.guard.patterns = ["敏感"]
    p._assemble_guard()
    assert len(p._guard_compiled) == 1 and match_guard(p._guard_compiled, "有敏感词") == 1

    # 改 patterns→新规则替换
    p.config.guard.patterns = ["别的"]
    p._assemble_guard()
    assert len(p._guard_compiled) == 1 and match_guard(p._guard_compiled, "有敏感词") == 0
    assert match_guard(p._guard_compiled, "有别的词") == 1

    # 开→关:置空
    p.config.guard.enabled = False
    p._assemble_guard()
    assert p._guard_compiled == []


def test_diary_after_midnight_uses_sleep_day_materials(tmp_path):
    """午夜后入睡的日记素材日(入睡日):now 已是次日 00:05,日期行/备忘
    due_on/聊天时间线起点全部取 sleep_day(入睡日)——日记与日程同源修正,
    不再用 now 取日(否则素材日写成次日)。"""

    import plugin as plugin_mod

    class _FakeDateTime(datetime):
        _current = datetime(2026, 9, 5, 0, 5, 0)

        @classmethod
        def now(cls, tz=None):
            del tz
            return cls._current

    p = _make_diary_plugin(tmp_path)
    p._stream_cache = {}
    p._stream_cache_at = 0.0
    due_days: list[str] = []
    real_due_on = p.memo.due_on

    def _capture_due_on(day):
        due_days.append(day)
        return real_due_on(day)

    p.memo.due_on = _capture_due_on
    cap_calls: list[dict] = []

    async def _capability(name, **kw):
        cap_calls.append({"name": name, **kw})
        if name == "message.get_by_time":
            return []
        return {"success": True, "response": "{}"}

    p._ctx.call_capability = _capability
    old = plugin_mod.datetime
    plugin_mod.datetime = _FakeDateTime
    try:
        asyncio.run(p._generate_and_publish_diary(sleep_day="2026-09-04"))
    finally:
        plugin_mod.datetime = old

    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "今天是2026年9月4日" in stable  # 素材日期行=入睡日(now 已是 9 月 5 日)
    assert due_days == ["2026-09-04"]  # 备忘 due_on 用素材日
    assert any(
        c["name"] == "message.get_by_time"
        and c["start_time"] == datetime(2026, 9, 4, 0, 0, 0).timestamp()
        for c in cap_calls
    )  # 聊天时间线起点=入睡日 00:00
    assert p.qzone_client.publish_calls  # 全链路照常发布


def test_weather_text_staleness_ceiling(tmp_path):
    """天气快照新鲜度上限:只按 fetched_at 倒序取最新不查时效,持续拉取失败时
    旧天气会被无限期当「当前天气」注入环境块/日程/日记素材——超 6 小时按
    「无数据」处理(debug 记录不刷屏);1 小时内正常返回;fetched_at 解析失败
    同样按过期(解析异常不得抛出)。"""

    p = _make_diary_plugin(tmp_path)
    p.store.execute(
        "CREATE TABLE IF NOT EXISTS weather_snapshot ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), city TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL, data TEXT NOT NULL)"
    )

    def _insert(fetched_at: str) -> None:
        p.store.execute("DELETE FROM weather_snapshot")
        p.store.execute(
            "INSERT INTO weather_snapshot (id, city, fetched_at, data) VALUES (1, '珠海', "
            f"'{fetched_at}', '{{\"temperature_2m\": 26.5, \"weather_code\": 1}}')"
        )

    _insert((datetime.now() - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%S"))
    assert p._weather_text() == "无数据"  # 7 小时前:过期
    assert any(level == "debug" and "天气快照过期" in str(a[0]) for level, a in p.logs)

    _insert((datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"))
    assert p._weather_text() == "温度 26.5°C(天气码 1)"  # 1 小时前:正常

    _insert("not-a-timestamp")
    assert p._weather_text() == "无数据"  # 时刻不可解析:按过期,不抛出


def test_diary_chat_timeline_success_without_messages_key_warns_and_falls_back(tmp_path):
    """形态容错显式化:get_by_time 返回 dict+success 但缺 messages 键(或非 list)
    时,旧逻辑静默按 0 条处理,日记会误写「今天没和人聊天」——现显式 warning
    并回退逐流取数,时间线素材仍可用。"""

    import time as _time

    today = datetime.now()
    p = _make_diary_plugin(tmp_path)
    p._stream_cache = {
        "g1": {"session_id": "g1", "is_group_session": True, "user_id": ""},
    }
    p._stream_cache_at = _time.time()
    ten_five = today.replace(hour=10, minute=5, second=0, microsecond=0)
    msgs = [_diary_msg("40000", "群友", "逐流回退的消息", ten_five.timestamp())]
    requested: list[str] = []

    async def _capability(name, **kw):
        if name == "message.get_by_time":
            return {"success": True}  # success 但缺 messages 键的畸形形态
        if name == "message.get_recent":
            requested.append(str(kw.get("chat_id")))
            return list(msgs)
        return {"success": True, "response": "{}"}

    p._ctx.call_capability = _capability
    asyncio.run(p._generate_and_publish_diary())
    stable = p.llm_calls[0]["messages"][1]["content"]
    assert "[10:05] 群友:逐流回退的消息" in stable  # 逐流回退路径被走到
    assert requested == ["g1"]
    assert any(
        level == "warning" and "get_by_time 返回形态异常" in str(a[0]) for level, a in p.logs
    )
