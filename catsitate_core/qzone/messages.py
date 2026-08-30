"""虚拟流注入消息构造(message_dict 对齐主程序 plugin_runtime/host/message_utils.py 格式)。

纪律(spec §2.2):timestamp=当前时刻(新鲜,get_recent 24h 窗);message_id 全局唯一
(tid+序号);additional_config.is_mentioned=1.0 强制触发;图片段带 binary_data_base64
(主流水线描述/落 Images 表的前提),超限图以 [图片] 占位。
"""

from __future__ import annotations

import base64

from catsitate_core.qzone import QZONE_PLATFORM
from catsitate_core.qzone.protocol import FeedItem


def build_feed_message(
    feed: FeedItem,
    *,
    seq: int,
    group_id: str,
    group_name: str,
    images: list[tuple[str, bytes]],
    max_kb: int,
    now_epoch: float,
) -> dict:
    """构造一条说说注入消息。images 为 (url, bytes) 列表,超限/缺失的图以占位呈现。"""

    text = feed.content.strip() or "(无文字内容)"
    raw: list[dict] = []
    for url, data in images:
        if data is None:
            text += " [图片]"
            continue
        if len(data) > max_kb * 1024:
            text += " [图片]"
            continue
        raw.append({
            "type": "image",
            "data": "QQ空间动态图片",
            "binary_data_base64": base64.b64encode(data).decode("ascii"),
        })
    if not raw and feed.image_urls and not images:
        text += " [图片]"  # 有图但全未下载成功的占位
    raw.insert(0, {"type": "text", "data": text})
    return {
        "message_id": f"qzone_{feed.tid}_{seq}",
        "platform": QZONE_PLATFORM,
        "timestamp": str(int(now_epoch)),
        "message_info": {
            "user_info": {"user_id": str(feed.uin), "user_nickname": feed.nickname},
            "group_info": {"group_id": group_id, "group_name": group_name},
            # is_mentioned 必须嵌在 message_info.additional_config 内:主程序
            # is_mentioned_bot_in_message 只读该位置(联调缺陷#3,顶层键会被丢弃)
            "additional_config": {"is_mentioned": 1.0},
        },
        "raw_message": raw,
    }
