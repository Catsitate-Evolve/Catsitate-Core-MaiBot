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
                             images=[], max_kb=3072, now_epoch=1750000100.0)
    assert msg["platform"] == QZONE_PLATFORM
    assert msg["message_id"] == "qzone_t1_3"  # 全局唯一去重键(tid+序号)
    assert msg["timestamp"] == "1750000000"  # 发布时间(原时间,联调缺陷#5)
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
    small = b"imagedata"
    oversized = b"x" * (1024 * 1024 + 1)  # 1MB+1 超 1KB 上限
    msg = build_feed_message(_feed(abstime="", image_urls=["u1", "u2"]), seq=1, group_id="g", group_name="n",
                             images=[("u1", small), ("u2", oversized)], max_kb=1, now_epoch=1.0)
    text = msg["raw_message"][0]
    assert text == {"type": "text", "data": "今天天气好 [图片]"}  # 超限图占位(abstime 空→无前缀)
    img = msg["raw_message"][1]
    assert img["type"] == "image"
    assert base64.b64decode(img["binary_data_base64"]) == small  # 未超限图带 base64(主流水线落盘/描述前提)


def test_build_message_empty_content_uses_placeholder():
    msg = build_feed_message(_feed(abstime="", content=""), seq=1, group_id="g", group_name="n",
                             images=[], max_kb=1, now_epoch=1.0)
    assert msg["raw_message"][0]["data"] == "(无文字内容)"


def test_build_message_uses_post_time_and_prefix():
    """联调缺陷#5(时间错乱):时间戳=发布时间(原时间);正文带相对时间前缀(时区无关断言)。"""
    import datetime as _dt

    post = 1750000000
    post_dt = _dt.datetime.fromtimestamp(post)
    clock = f"{post_dt:%H:%M}"
    same_day_evening = _dt.datetime(post_dt.year, post_dt.month, post_dt.day, 22, 0).timestamp()
    msg = build_feed_message(_feed(abstime=str(post)), seq=1, group_id="g", group_name="n",
                             images=[], max_kb=1, now_epoch=same_day_evening)
    assert msg["timestamp"] == str(post)  # 原时间(非注入时刻)
    text = msg["raw_message"][0]["data"]
    assert text.startswith("(今天") and clock in text and text.endswith("今天天气好")
    # 老动态(约 2 个月后阅读)→ 日期前缀
    later = _dt.datetime(post_dt.year, post_dt.month + 2, 1, 9, 0).timestamp()
    msg2 = build_feed_message(_feed(abstime=str(post)), seq=2, group_id="g", group_name="n",
                              images=[], max_kb=1, now_epoch=later)
    t2 = msg2["raw_message"][0]["data"]
    assert t2.startswith(f"({post_dt:%m月%d日}") and clock in t2
    # abstime 缺失:回退注入时刻,无前缀
    msg3 = build_feed_message(_feed(abstime=""), seq=3, group_id="g", group_name="n",
                              images=[], max_kb=1, now_epoch=123456.0)
    assert msg3["timestamp"] == "123456"
    assert msg3["raw_message"][0]["data"] == "今天天气好"


def test_build_message_pure_image_text_policy():
    """纯图说说:带时间→文本段仅含时间前缀;无时间→省略文本段(图段承载内容,联调缺陷#4)。"""
    msg = build_feed_message(_feed(abstime="1750000000", content="", image_urls=["u1"]),
                             seq=1, group_id="g", group_name="n",
                             images=[("u1", b"imgdata")], max_kb=1, now_epoch=1750000100.0)
    assert msg["raw_message"][0]["type"] == "text"
    assert msg["raw_message"][0]["data"].startswith("(今天")  # 仅时间前缀
    assert msg["raw_message"][1]["type"] == "image"
    # 无时间纯图:首段即图片
    msg2 = build_feed_message(_feed(abstime="", content="", image_urls=["u1"]),
                              seq=2, group_id="g", group_name="n",
                              images=[("u1", b"imgdata")], max_kb=1, now_epoch=1.0)
    assert msg2["raw_message"][0]["type"] == "image"


def test_gateway_declared_platform_constant():
    """网关平台必须是常量 qzone-qq(连字符别名折叠进 qq 人物命名空间,spec §2.17)。"""
    import inspect

    import plugin as _plugin  # tests/conftest 已把插件目录加入 sys.path

    src = inspect.getsource(_plugin)
    assert 'MessageGateway(' in src and 'qzone-qq' in src
    # 网关回调显式拒发
    assert "M1_OUTBOUND_ERROR" in src
