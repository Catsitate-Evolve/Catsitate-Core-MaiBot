"""虚拟流注入消息构造测试(spec §2.2/§3.2)。"""
import base64

from catsitate_core.qzone import QZONE_PLATFORM
from catsitate_core.qzone.messages import build_feed_message, build_notify_message
from catsitate_core.qzone.protocol import FeedItem


def _feed(**kw):
    # kw 允许覆盖默认字段(如 content="");brief 原样代码在 _feed(content="") 时与
    # 显式关键字冲突抛 TypeError,此为辅助函数机械缺陷的等价修正,断言语义不变。
    args = dict(tid="t1", abstime="1750000000", uin="10001", nickname="小明", content="今天天气好")
    args.update(kw)
    return FeedItem(**args)


def test_build_message_core_fields():
    msg = build_feed_message(_feed(), seq=3, group_id="qzone_feed", group_name="QQ空间",
                             images=[], now_epoch=1750000100.0)
    assert msg["platform"] == QZONE_PLATFORM
    assert msg["message_id"] == "qzone_t1_3"  # 全局唯一去重键(tid+序号)
    assert msg["timestamp"] == "1750000100"  # 方案 B:timestamp=阅读时刻(注入时刻)
    info = msg["message_info"]
    assert info["user_info"] == {"user_id": "10001", "user_nickname": "小明"}
    assert info["group_info"] == {"group_id": "qzone_feed", "group_name": "QQ空间"}
    # 强制触发标记必须在 message_info.additional_config 内(联调缺陷#3:
    # 主程序 is_mentioned_bot_in_message 只读 message_info.additional_config,
    # 顶层键会被丢弃,注入消息将卡在必要性评分 50<80 不触发 planner 轮)
    assert info["additional_config"]["is_mentioned"] == 1.0
    text = msg["raw_message"][0]["data"]
    assert text.startswith("(今天") and text.endswith("今天天气好")  # 同日阅读→今天前缀


def test_build_message_image_segments_and_placeholder():
    """图片段形态对齐 napcat-adapter(data 留空=描述槽,hash 显式 sha256,联调缺陷#15);
    下载失败(None)的图以 [图片] 占位;体积不限(交主程序入站链路)。"""
    import hashlib

    small = b"imagedata"
    msg = build_feed_message(_feed(abstime="", image_urls=["u1", "u2"]), seq=1, group_id="g", group_name="n",
                             images=[("u1", small), ("u2", None)], now_epoch=1.0)
    text = msg["raw_message"][0]
    assert text == {"type": "text", "data": "今天天气好 [图片]"}  # 下载失败的图占位(abstime 空→无前缀)
    img = msg["raw_message"][1]
    assert img["type"] == "image"
    assert img["data"] == ""  # 描述槽必须留空:填占位文本会令主程序跳过 VLM 描述
    assert img["hash"] == hashlib.sha256(small).hexdigest()
    assert base64.b64decode(img["binary_data_base64"]) == small  # 成功图带 base64(主流水线落盘/描述前提)


def test_build_message_empty_content_uses_placeholder():
    msg = build_feed_message(_feed(abstime="", content=""), seq=1, group_id="g", group_name="n",
                             images=[], now_epoch=1.0)
    assert msg["raw_message"][0]["data"] == "(无文字内容)"


def test_build_message_reading_timestamp_and_publish_prefix():
    """时间语义(方案 B,2026-08-31):timestamp=阅读时刻(注入时刻,消息流时钟单调);
    发布时间由正文相对时间前缀承载(时区无关断言)。"""
    import datetime as _dt

    post = 1750000000
    post_dt = _dt.datetime.fromtimestamp(post)
    clock = f"{post_dt:%H:%M}"
    same_day_evening = _dt.datetime(post_dt.year, post_dt.month, post_dt.day, 22, 0).timestamp()
    msg = build_feed_message(_feed(abstime=str(post)), seq=1, group_id="g", group_name="n",
                             images=[], now_epoch=same_day_evening)
    assert msg["timestamp"] == str(int(same_day_evening))  # 阅读时刻(与发布日无关)
    text = msg["raw_message"][0]["data"]
    assert text.startswith("(今天") and clock in text and text.endswith("今天天气好")
    # 老动态(约 2 个月后阅读)→ 日期前缀;timestamp 仍是阅读时刻
    later = _dt.datetime(post_dt.year, post_dt.month + 2, 1, 9, 0).timestamp()
    msg2 = build_feed_message(_feed(abstime=str(post)), seq=2, group_id="g", group_name="n",
                              images=[], now_epoch=later)
    assert msg2["timestamp"] == str(int(later))
    t2 = msg2["raw_message"][0]["data"]
    assert t2.startswith(f"({post_dt:%m月%d日}") and clock in t2
    # abstime 缺失:无前缀
    msg3 = build_feed_message(_feed(abstime=""), seq=3, group_id="g", group_name="n",
                             images=[], now_epoch=123456.0)
    assert msg3["timestamp"] == "123456"
    assert msg3["raw_message"][0]["data"] == "今天天气好"


def test_build_message_pure_image_text_policy():
    """纯图说说:带时间→文本段仅含时间前缀;无时间→省略文本段(图段承载内容,联调缺陷#4)。"""
    msg = build_feed_message(_feed(abstime="1750000000", content="", image_urls=["u1"]),
                             seq=1, group_id="g", group_name="n",
                             images=[("u1", b"imgdata")], now_epoch=1750000100.0)
    assert msg["raw_message"][0]["type"] == "text"
    assert msg["raw_message"][0]["data"].startswith("(今天")  # 仅时间前缀
    assert msg["raw_message"][1]["type"] == "image"
    # 无时间纯图:首段即图片
    msg2 = build_feed_message(_feed(abstime="", content="", image_urls=["u1"]),
                              seq=2, group_id="g", group_name="n",
                              images=[("u1", b"imgdata")], now_epoch=1.0)
    assert msg2["raw_message"][0]["type"] == "image"


def test_build_notify_message_with_reply_segment():
    """通知专用构造(联调修正):reply 段置首引用原说说的注入消息(napcat quote 式
    上下文关联),target_message_content 截 60 字;消息 id 形如 qzone_notify_{tid}_{epoch}。"""
    feed = _feed(tid="notify_comment_f1_c1", uin="20000", nickname="小红",
                 content="(通知) 小红 评论了你的说说: 好棒")
    msg = build_notify_message(feed, group_id="qzone_feed", group_name="QQ空间",
                               now_epoch=1750000100.0, reply_target_id="qzone_f1_3",
                               reply_target_content="原说说正文" * 20, reply_target_sender="10000")
    assert msg["message_id"] == "qzone_notify_notify_comment_f1_c1_1750000100"
    assert msg["platform"] == QZONE_PLATFORM and msg["timestamp"] == "1750000100"
    info = msg["message_info"]
    assert info["user_info"] == {"user_id": "20000", "user_nickname": "小红"}
    assert info["group_info"] == {"group_id": "qzone_feed", "group_name": "QQ空间"}
    assert info["additional_config"]["is_mentioned"] == 1.0  # 同 build_feed_message(联调缺陷#3)
    reply = msg["raw_message"][0]
    assert reply["type"] == "reply"
    assert reply["data"]["target_message_id"] == "qzone_f1_3"
    assert reply["data"]["target_message_content"] == ("原说说正文" * 20)[:60]  # 截 60 字
    assert reply["data"]["target_message_sender_id"] == "10000"
    assert msg["raw_message"][1] == {"type": "text", "data": feed.content}


def test_build_notify_message_without_reply_segment():
    """reply 目标缺省(原说说未注入过,seen 无 message_id 记录)→ 无 reply 段,
    raw_message 仅文本段,正文原样(通知格式精简后不再重复引用原文)。"""
    feed = _feed(tid="notify_reply_f2_r1", uin="30000", nickname="阿好",
                 content="(通知) 阿好 回复了你: 说得对")
    msg = build_notify_message(feed, group_id="g", group_name="n", now_epoch=1750000200.5)
    assert msg["message_id"] == "qzone_notify_notify_reply_f2_r1_1750000200"
    assert msg["raw_message"] == [{"type": "text", "data": feed.content}]


def test_gateway_declared_platform_constant():
    """网关平台必须是常量 qzone-qq(连字符别名折叠进 qq 人物命名空间,spec §2.17)。"""
    import inspect

    import plugin as _plugin  # tests/conftest 已把插件目录加入 sys.path

    src = inspect.getsource(_plugin)
    assert 'MessageGateway(' in src and 'qzone-qq' in src
    # 网关回调显式拒发(M2 起为按意图路由,无意图仍拒;断言同步为拒发分支告警文案)
    assert "QQ空间出站拒绝" in src


def test_m2_wiring_source_assertions():
    """M2 接线源码级断言:驱动路由/点赞工具/统一通知轮询/意图消费。"""
    import inspect

    import plugin as _plugin

    src = inspect.getsource(_plugin)
    assert "route_outbound(" in src and "do_comment(fid=" in src
    # 楼中楼降级(联调实证 -10049):reply 分支改发头评+@前缀,do_reply 不再接线
    assert "do_reply(" not in src and "auto:1}}" in src
    assert '"qzone_like"' in src and "do_like(fid=" in src
    assert "comment_reply" in src
    # T11 统一通知通道:双源检测(自己说说评论+他人说说楼中楼回复)+P1 插队
    assert "_qzone_notify_poll_tick" in src and "_qzone_comment_poll_tick" not in src
    assert "enqueue_priority(" in src and "parse_feed_replies(" in src
    assert "get_user_feeds_raw(" in src and "notification_interval_seconds" in src
    assert 'source="notify"' in src  # 通知 FeedItem 标记(泵按 source 区分意图)
    # T11 工具双向隔离:非 qzone 流隐藏 qzone_ 前缀专属工具(防模型误调);
    # 终审 I4 抽纯函数 filter_qzone_tools_for_stream(scene.py),plugin 侧接线断言随之更新
    assert "filter_qzone_tools_for_stream(" in src
    assert 'self._qzone_outbound_intent = None' in src  # 意图一次性消费
    # 审查必修:远端成功即刻消费意图(记账失败不得把意图留到下一条出站→重复评论)
    assert 'self._qzone_outbound_intent = None  # 远端成功即刻消费' in src
    # T7 接线:好感度显式事件消费(结算素材并入 + 衰减计时基准)
    assert "fav_events_on(" in src and "last_fav_interaction(" in src
    # T7 审查必修:事件合成消息 ts 用原始时刻(created_at)防同日 early→daily 重判;
    # 事件标签按 kind 三分(审查顺手)
    assert '"ts": e["created_at"] or' in src
    assert "你评论了TA" in src and "你点赞了TA" in src
    # T7 M-1:快照缓存 LRU 上限
    assert "SNAPSHOT_CACHE_MAX" in src and "popitem(last=False)" in src
    # T7 M-2:见闻摘要带作者昵称
    assert 'author_nickname=friend["nickname"]' in src and "author_nickname" in src
    # T11 场景文案:通知回应→楼中楼回复
    from catsitate_core.qzone.scene import QZONE_SCENE_TEXT as _scene_text

    assert "通知" in _scene_text and "楼中楼" in _scene_text


def test_selfcheck_blocks_talk_value_zero():
    """spec §2.18 硬性要求:talk_value=0 前置检测(注入消息会被主程序静默消费)。"""
    import inspect

    import plugin as _plugin

    assert "talk_value" in inspect.getsource(_plugin)
    selfcheck_src = inspect.getsource(_plugin.CatsitatePlugin._qzone_selfcheck)
    # 键路径核对(official_configs.py):talk_value 归属 ChatConfig.reply_timing
    assert "chat.reply_timing.talk_value" in selfcheck_src
    # 停用文案关键词(talk_value=0 → 告警并 return False)
    assert "talk_value=0" in selfcheck_src and "静默消费" in selfcheck_src
    assert "return False" in selfcheck_src


# ---- RPC 帧预算压缩(用户裁定 2026-08-31:体积治理=压缩到帧限内,非拒收) ----

from catsitate_core.qzone.messages import RPC_IMAGE_BUDGET_BYTES, fit_images_to_rpc_budget


def test_fit_images_under_budget_unchanged():
    imgs = [("u1", b"a" * 100), ("u2", b"b" * 50)]
    out = fit_images_to_rpc_budget(imgs, budget_bytes=RPC_IMAGE_BUDGET_BYTES)
    assert out == imgs  # 预算内原样返回(不白压缩)


def test_fit_images_compresses_via_ladder_until_fit():
    """超预算→压缩阶梯逐级收紧至达标(注入 fake 压缩器,离线可测)。"""
    big = b"x" * 3000
    calls = []

    def fake_compress(data, max_dim, quality):
        calls.append((max_dim, quality))
        return data[: len(data) // 4]  # 每级缩到 1/4

    out = fit_images_to_rpc_budget([("u", big)], budget_bytes=1000, compress=fake_compress)
    assert len(out[0][1]) == 750  # 3000→750(base64≈1000)一级达标
    assert calls and calls[0] == (4096, 85)  # 从最轻档开始


def test_fit_images_extreme_case_drops_largest():
    """压缩永不达标(极端)→丢弃最大图保帧限,并逐次回调告警。"""
    dropped = []

    def no_shrink(data, max_dim, quality):
        return data  # 压缩无效(模拟已是极限)

    out = fit_images_to_rpc_budget(
        [("small", b"a" * 100), ("huge", b"b" * 5000)],
        budget_bytes=200, compress=no_shrink, on_drop=lambda u: dropped.append(u),
    )
    assert ("huge", None) in out and ("small", b"a" * 100) in out
    assert dropped == ["huge"]


def test_module_log_forwarder_routes_levels():
    """联调缺陷#10:模块级日志转发到插件 ctx logger(告警可见性)。"""
    import logging as _logging

    from plugin import _ModuleLogForwarder

    captured = []

    class _Sink:
        def warning(self, fmt, *args):
            captured.append(("warning", fmt % args if args else fmt))

        def error(self, fmt, *args):
            captured.append(("error", fmt % args if args else fmt))

        def info(self, fmt, *args):
            captured.append(("info", fmt % args if args else fmt))

    fwd = _ModuleLogForwarder(_Sink())
    rec = _logging.LogRecord("catsitate_core.qzone.client", _logging.WARNING, __file__, 1, "下载失败: %s", ("u1",), None)
    fwd.emit(rec)
    rec2 = _logging.LogRecord("catsitate_core.x", _logging.ERROR, __file__, 1, "boom", (), None)
    fwd.emit(rec2)
    assert captured[0] == ("warning", "catsitate_core.qzone.client: 下载失败: u1")
    assert captured[1] == ("error", "catsitate_core.x: boom")


# ---- 统一通知轮询行为测试(T11:_StubCtx 模式,P1 入队→泵注入→意图楼中楼) ----

import asyncio as _asyncio
import time as _time

from catsitate_core.config import CatsitateConfig as _CatsitateConfig
from catsitate_core.qzone.comment_seen import CommentSeenStore as _CommentSeenStore
from catsitate_core.qzone.injector import FeedInjector as _FeedInjector
from catsitate_core.qzone.seen_store import SeenStore as _SeenStore
from catsitate_core.qzone.wire import CommentItem as _CommentItem
from catsitate_core.storage import SQLiteStore as _SQLiteStore


class _CollectLogger2:
    """收集日志的 stub logger(断言跳过日志)。"""

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


class _StubGateway2:
    """记录 route_message 注入调用的网关桩。"""

    def __init__(self):
        self.calls = []

    async def route_message(self, name, msg):
        self.calls.append((name, msg))
        return True


class _StubCtx2:
    """统一通知轮询测试的最小 ctx 面:logger + gateway。"""

    def __init__(self, logs):
        self.logger = _CollectLogger2(logs)
        self.gateway = _StubGateway2()


class _StubCommentClient:
    """通知轮询输入桩:get_own_feed_comments 返回固定 (comments, feed 上下文)。"""

    def __init__(self, comments, ctx_map):
        self._comments = comments
        self._ctx_map = ctx_map
        self.calls = 0

    async def get_own_feed_comments(self, *, bot_uin, num=10):
        del bot_uin, num
        self.calls += 1
        return self._comments, self._ctx_map

    async def get_unified_timeline(self, *, count=20):
        del count
        return []


def _make_notify_poll_plugin(tmp_path, comments, ctx_map):
    """离线装配 _qzone_notify_poll_tick 所需最小插件实例(注入窗口开启,泵可推进)。"""

    import plugin as plugin_mod

    logs: list = []
    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx2(logs)
    p._plugin_config_instance = _CatsitateConfig()
    p._qzone_available = True
    p.config.sleep.enabled = False  # 不依赖 self.sleep
    p.config.favorability.bot_user_id = "10000"
    p._qzone_outbound_intent = None
    p._qzone_seq = 0
    p._qzone_pump_lock = _asyncio.Lock()
    p.qzone_seen = _SeenStore(_SQLiteStore(tmp_path / "seen.db"))
    p.qzone_seen.ensure_schema()
    p.qzone_comment_seen = _CommentSeenStore(_SQLiteStore(tmp_path / "notify.db"))
    p.qzone_comment_seen.ensure_schema()
    p.qzone_injector = _FeedInjector(decision_window_s=75)
    p.qzone_injector.window_started()  # 通知经泵注入需窗口开启(窗口外静置 P1)
    p.qzone_client = _StubCommentClient(comments, ctx_map)
    return p


def test_notify_poll_stale_comment_skipped_and_registered(tmp_path):
    """T11 新鲜度截断(承终审 I2):早于 summary_days 的过旧评论不入队不注入,
    且 is_new 已登记——下轮判重不再重扫(发现即登记的 store 契约)。"""

    stale = str(int(_time.time()) - 10 * 86400)  # 默认 summary_days=3,10 天前必过旧
    comments = {"feed1": [_CommentItem(
        comment_tid="ct1", uin="20000", nickname="小红", content="好棒", create_time=stale,
    )]}
    p = _make_notify_poll_plugin(tmp_path, comments, {"feed1": "今天的心情"})
    _asyncio.run(p._qzone_notify_scan())
    assert p._ctx.gateway.calls == []  # 不注入
    assert p._qzone_outbound_intent is None  # 不占意图
    assert p.qzone_injector.queue_size() == 0  # 未入队(不是入队后没泵出)
    # 已登记:is_new 对该键返回 False(下轮判重跳过,不重扫)
    assert p.qzone_comment_seen.is_new("feed1:ct1:20000") is False
    assert any(level == "info" and "评论过旧跳过" in str(a[0]) for level, a in p._ctx.logger._logs)


def test_notify_poll_injects_notify_message_with_and_without_reply_segment(tmp_path):
    """通知注入(联调修正):泵对 source=notify 走 build_notify_message 专用构造——
    正文精简「(通知) XX 评论了你的说说: 评论内容」(无发布时间前缀/reply 段已带
    上下文);原说说已在 seen 登记 message_id → 注入消息带 reply 段引用原说说注入
    消息(napcat quote 式);未登记 → 无 reply 段回退纯文本。"""

    fresh = {"feed2": [_CommentItem(
        comment_tid="ct2", uin="20001", nickname="小明", content="写得好",
        create_time=str(int(_time.time())),
    )]}
    p = _make_notify_poll_plugin(tmp_path, fresh, {"feed2": "今天的心情"})
    # 原说说 feed2 已注入过(seen 记录 message_id)→ 通知应带 reply 段引用它
    p.qzone_seen.mark_queued("feed2", abstime="1750000000", author_uin="10000", summary="今天的心情")
    p.qzone_seen.mark_seen("feed2", "2026-09-01T10:00:00", "qzone_feed2_5")
    _asyncio.run(p._qzone_notify_scan())
    assert len(p._ctx.gateway.calls) == 1
    msg = p._ctx.gateway.calls[0][1]
    assert msg["message_id"].startswith("qzone_notify_notify_comment_feed2_ct2_")  # 通知专用 id 形态
    reply = msg["raw_message"][0]
    assert reply["type"] == "reply"  # 引用段置首(napcat quote 式上下文关联)
    assert reply["data"]["target_message_id"] == "qzone_feed2_5"
    assert reply["data"]["target_message_sender_id"] == "10000"  # 源A:原说说作者=bot
    assert msg["raw_message"][1] == {"type": "text", "data": "(通知) 小明 评论了你的说说: 写得好"}
    intent = p._qzone_outbound_intent
    assert intent is not None and intent.kind == "comment_reply"
    assert (intent.tid, intent.target_qq, intent.comment_tid, intent.comment_uin) == \
        ("feed2", "10000", "ct2", "20001")  # 源A:说说主人=bot,commentId=好友评论
    assert intent.message_id == msg["message_id"]  # 意图绑定通知注入消息(quote 校验对位)

    # 回退形态:原说说未注入过(无 message_id 记录)→ 无 reply 段,首段即精简正文
    no_origin = {"feed3": [_CommentItem(
        comment_tid="ct3", uin="20002", nickname="小刚", content="加油", create_time="",
    )]}
    p2 = _make_notify_poll_plugin(tmp_path / "b", no_origin, {"feed3": "今天的心情"})
    _asyncio.run(p2._qzone_notify_scan())
    assert len(p2._ctx.gateway.calls) == 1
    raw2 = p2._ctx.gateway.calls[0][1]["raw_message"]
    assert raw2 == [{"type": "text", "data": "(通知) 小刚 评论了你的说说: 加油"}]
    assert p2._qzone_outbound_intent is not None and p2._qzone_outbound_intent.comment_tid == "ct3"
