"""虚拟流注入消息构造测试(spec §2.2/§3.2)。"""
import base64

from catsitate_core.qzone import QZONE_PLATFORM
from catsitate_core.qzone.messages import build_feed_message
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


def test_gateway_declared_platform_constant():
    """网关平台必须是常量 qzone-qq(连字符别名折叠进 qq 人物命名空间,spec §2.17)。"""
    import inspect

    import plugin as _plugin  # tests/conftest 已把插件目录加入 sys.path

    src = inspect.getsource(_plugin)
    assert 'MessageGateway(' in src and 'qzone-qq' in src
    # 网关回调显式拒发(M2 起为按意图路由,无意图仍拒;断言同步为拒发分支告警文案)
    assert "QQ空间出站拒绝" in src


def test_m2_wiring_source_assertions():
    """M2 接线源码级断言:驱动路由/点赞工具/评论轮询/意图消费。"""
    import inspect

    import plugin as _plugin

    src = inspect.getsource(_plugin)
    assert "route_outbound(" in src and "do_comment(fid=" in src and "do_reply(fid=" in src
    assert '"qzone_like"' in src and "do_like(fid=" in src
    assert "_qzone_comment_poll_tick" in src and "comment_reply" in src
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
