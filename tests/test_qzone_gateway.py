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
    assert msg["timestamp"] == "1750000100"  # 新鲜时间戳(非动态发布时间)
    info = msg["message_info"]
    assert info["user_info"] == {"user_id": "10001", "user_nickname": "小明"}
    assert info["group_info"] == {"group_id": "qzone_feed", "group_name": "QQ空间"}
    assert msg["additional_config"]["is_mentioned"] == 1.0  # 强制触发(spec §2.18)
    assert msg["raw_message"] == [{"type": "text", "data": "今天天气好"}]


def test_build_message_image_segments_and_placeholder():
    small = b"imagedata"
    oversized = b"x" * (1024 * 1024 + 1)  # 1MB+1 超 1KB 上限
    msg = build_feed_message(_feed(image_urls=["u1", "u2"]), seq=1, group_id="g", group_name="n",
                             images=[("u1", small), ("u2", oversized)], max_kb=1, now_epoch=1.0)
    text = msg["raw_message"][0]
    assert text == {"type": "text", "data": "今天天气好 [图片]"}  # 超限图占位
    img = msg["raw_message"][1]
    assert img["type"] == "image"
    assert base64.b64decode(img["binary_data_base64"]) == small  # 未超限图带 base64(主流水线落盘/描述前提)


def test_build_message_empty_content_uses_placeholder():
    msg = build_feed_message(_feed(content=""), seq=1, group_id="g", group_name="n",
                             images=[], max_kb=1, now_epoch=1.0)
    assert msg["raw_message"][0]["data"] == "(无文字内容)"
