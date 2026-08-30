"""QQ 空间网页 cgi 协议纯函数(蓝本 Maizone 3.0.2,自研实现;端点与鉴权见 spec §2.14/§11)。

仅放纯函数:解析与签名。IO(QzoneClient)在 client.py,便于离线单测。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FEED_APPID_SHUOSHUO = 311  # M1 只处理说说类动态,其余 appid 跳过并计数(spec §2.14)


@dataclass
class FeedItem:
    """一条好友动态(说说)。"""

    tid: str
    abstime: str
    uin: str
    nickname: str
    content: str
    image_urls: list[str] = field(default_factory=list)
    appid: int = FEED_APPID_SHUOSHUO


def generate_gtk(p_skey: str) -> int:
    """g_tk = hash33(p_skey)(经典 QQ 网页鉴权参数)。"""

    h = 5381
    for ch in str(p_skey or ""):
        h += (h << 5) + ord(ch)
    return 2147483647 & h


def extract_callback_json(text: str) -> dict:
    """从 `frameElement.callback( {...} );` 包裹的响应中截取 JSON。

    比 Maizone 的固定偏移截取更稳:取首个 "(" 与末个 ")" 之间的片段再 strip。
    解析失败抛出(调用方告警,不静默)。
    """

    left = text.index("(")
    right = text.rindex(")")
    payload = text[left + 1 : right].strip()
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(f"callback 载荷不是 JSON 对象: {type(data).__name__}")
    return data


def _feed_images(feed: dict) -> list[str]:
    pic = feed.get("pic") or {}
    urls: list[str] = []
    for item in pic.get("picList") or []:
        url = str((item or {}).get("url1") or "")
        if url:
            urls.append(url)
    return urls


def parse_msglist(payload: dict) -> tuple[list[FeedItem], int]:
    """解析 emotion_cgi_msglist_v6 载荷为 FeedItem 列表。

    Returns:
        (items, skipped_non_311): 非 311 动态跳过并计数(告警统计由调用方记日志)。
    """

    data = (payload or {}).get("data") or {}
    items: list[FeedItem] = []
    skipped = 0
    for feed in data.get("vFeeds") or []:
        if not isinstance(feed, dict):
            continue
        appid = int(feed.get("appid") or 0)
        if appid != FEED_APPID_SHUOSHUO:
            skipped += 1
            continue
        userinfo = feed.get("userinfo") or {}
        uin = str(userinfo.get("uin") or "")
        nickname = str((userinfo.get("user") or {}).get("nick") or "") or uin
        summary = ((feed.get("summary") or {}).get("summary") or "").strip()
        items.append(
            FeedItem(
                tid=str(feed.get("tid") or ""),
                abstime=str(feed.get("abstime") or ""),
                uin=uin,
                nickname=nickname,
                content=summary,
                image_urls=_feed_images(feed),
                appid=appid,
            )
        )
    return items, skipped
