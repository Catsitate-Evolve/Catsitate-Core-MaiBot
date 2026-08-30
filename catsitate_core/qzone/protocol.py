"""QQ 空间网页 cgi 协议纯函数(蓝本 Maizone 3.0.2 + 联调实证 2026-08-30 修正)。

关键事实(联调裁定):emotion_cgi_msglist_v6 是「指定用户说说列表」(uin=目标,
响应顶层 msglist,条目含 tid/created_time/content/pic[].url1/commentlist),
不是好友聚合接口(vFeeds 形态不存在);好友列表经 adapter 的 OneBot API 获取。
仅放纯函数:解析与签名。IO(QzoneClient)在 client.py,便于离线单测。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FEED_APPID_SHUOSHUO = 311  # 说说类动态(msglist 条目即说说,M2 互动路径沿用此常量)


@dataclass
class FeedItem:
    """一条好友说说(来自指定用户的 msglist)。"""

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
    """从 `_Callback( {...} );` / `_preloadCallback( {...} );` 包裹的响应中截取 JSON。

    取首个 "(" 与末个 ")" 之间的片段再 strip(对任意回调名通用)。
    解析失败抛出(调用方告警,不静默)。
    """

    left = text.index("(")
    right = text.rindex(")")
    payload = text[left + 1 : right].strip()
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(f"callback 载荷不是 JSON 对象: {type(data).__name__}")
    return data


def parse_msglist(payload: dict, *, target_uin: str, nickname: str) -> list[FeedItem]:
    """解析指定用户 msglist 载荷为 FeedItem 列表(空/缺 msglist 返回空列表)。

    uin/nickname 由调用方传入:响应的 logininfo 是访客(bot)信息,好友昵称
    以 adapter 好友列表为准(remark 优先)。
    """

    entries = (payload or {}).get("msglist") or []
    items: list[FeedItem] = []
    for feed in entries:
        if not isinstance(feed, dict):
            continue
        urls: list[str] = []
        for pic in feed.get("pic") or []:
            url = str((pic or {}).get("url1") or "")
            if url:
                urls.append(url)
        items.append(
            FeedItem(
                tid=str(feed.get("tid") or ""),
                abstime=str(feed.get("created_time") or ""),
                uin=str(target_uin),
                nickname=str(nickname or target_uin),
                content=str(feed.get("content") or "").strip(),
                image_urls=urls,
            )
        )
    return items


def parse_friend_list(result: object) -> list[dict]:
    """解析 adapter OneBot get_friend_list 的返回(信封容忍)。

    Returns:
        [{"user_id": str, "nickname": str}]——remark 优先于 nickname(好友备注
        是用户对该好友的称呼,注入时更拟人);解析失败/空返回 []。
    """

    if isinstance(result, dict):
        result = result.get("data") if isinstance(result.get("data"), list) else result.get("friends")
    if not isinstance(result, list):
        return []
    out: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("user_id") or item.get("uin") or "").strip()
        if not uid:
            continue
        name = str(item.get("remark") or item.get("nickname") or "").strip() or uid
        out.append({"user_id": uid, "nickname": name})
    return out
