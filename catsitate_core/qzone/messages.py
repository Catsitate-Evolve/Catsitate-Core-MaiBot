"""虚拟流注入消息构造(message_dict 对齐主程序 plugin_runtime/host/message_utils.py 格式)。

纪律(联调修正 2026-08-31):timestamp=**发布时间**(原时间,防 bot 把老说说当成刚发生——
联调缺陷#5);正文带相对时间前缀(今天 HH:MM / M月d日 HH:MM)使模型可感知动态新旧;
message_id 全局唯一(tid+序号);is_mentioned 嵌在 message_info.additional_config
(主程序只读该位置);图片段带 binary_data_base64,下载失败的图以 [图片] 占位;
纯图说说省略空文本段(图段承载内容,联调缺陷#4)。图片体积不加插件侧上限
(用户裁定 2026-08-31:主程序入站链路对过大图片自有压缩/丢弃处理)。
"""

from __future__ import annotations

import base64
from datetime import datetime

from catsitate_core.qzone import QZONE_PLATFORM
from catsitate_core.qzone.protocol import FeedItem


def _time_prefix(post_dt: datetime, now_dt: datetime) -> str:
    """相对时间前缀:同日=今天HH:MM,不同日=M月d日HH:MM,跨年补年份。"""

    if (post_dt.year, post_dt.month, post_dt.day) == (now_dt.year, now_dt.month, now_dt.day):
        return f"(今天{post_dt:%H:%M})"
    if post_dt.year != now_dt.year:
        return f"({post_dt:%Y年%m月%d日 %H:%M})"
    return f"({post_dt:%m月%d日 %H:%M})"


def build_feed_message(
    feed: FeedItem,
    *,
    seq: int,
    group_id: str,
    group_name: str,
    images: list[tuple[str, bytes]],
    now_epoch: float,
) -> dict:
    """构造一条说说注入消息。images 为 (url, bytes) 列表,下载失败(None)的图以占位呈现。

    timestamp 取 feed.abstime(发布时间);abstime 非法/缺失时回退注入时刻且不加前缀。
    """

    text = feed.content.strip()
    post_epoch: float | None = None
    try:
        candidate = float(str(feed.abstime or "").strip())
        if candidate > 0:
            post_epoch = candidate
    except ValueError:
        post_epoch = None

    raw: list[dict] = []
    for url, data in images:
        if data is None:
            text += " [图片]"
            continue
        raw.append({
            "type": "image",
            "data": "QQ空间动态图片",
            "binary_data_base64": base64.b64encode(data).decode("ascii"),
        })
    if not raw and feed.image_urls and not images:
        text += " [图片]"  # 有图但全未下载成功的占位

    timestamp = post_epoch if post_epoch is not None else now_epoch
    if post_epoch is not None:
        prefix = _time_prefix(datetime.fromtimestamp(post_epoch), datetime.fromtimestamp(now_epoch))
    else:
        prefix = ""
    # 文本段:正文→前缀+正文;纯图→仅时间前缀(无时间则整段省略,图段承载内容);
    # 无正文无图→前缀+占位
    if text:
        body = f"{prefix}{text}".strip()
    elif raw:
        body = prefix
    else:
        body = f"{prefix}(无文字内容)".strip()
    if body:
        raw.insert(0, {"type": "text", "data": body})
    return {
        "message_id": f"qzone_{feed.tid}_{seq}",
        "platform": QZONE_PLATFORM,
        "timestamp": str(int(timestamp)),
        "message_info": {
            "user_info": {"user_id": str(feed.uin), "user_nickname": feed.nickname},
            "group_info": {"group_id": group_id, "group_name": group_name},
            # is_mentioned 必须嵌在 message_info.additional_config 内:主程序
            # is_mentioned_bot_in_message 只读该位置(联调缺陷#3,顶层键会被丢弃)
            "additional_config": {"is_mentioned": 1.0},
        },
        "raw_message": raw,
    }
