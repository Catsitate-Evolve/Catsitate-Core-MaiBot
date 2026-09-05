"""QQ 空间网页 cgi 协议纯函数(蓝本 Maizone 3.0.2 + 联调实证 2026-08-30 修正)。

关键事实(联调实证):emotion_cgi_msglist_v6 是「指定用户说说列表」(uin=目标,
响应顶层 msglist,条目含 tid/created_time/content/pic[].url1/commentlist),
不是好友聚合接口(vFeeds 形态不存在);好友列表经 adapter 的 OneBot API 获取。
仅放纯函数:解析与签名。IO(QzoneClient)在 client.py,便于离线单测。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注用,避免 protocol↔wire 循环导入
    from catsitate_core.qzone.wire import FeedComment

logger = logging.getLogger(__name__)

FEED_APPID_SHUOSHUO = 311  # 说说类动态(msglist 条目即说说,互动路径沿用此常量)


@dataclass
class FeedItem:
    """一条好友说说(来自指定用户的 msglist)。

    统一通知通道扩展:source 区分注入条目的队列来源("feed"=浏览动态
    P2 / "notify"=通知 P1;registry 登记与通知分支判定依据此值);friend_uin
    为通知源B(他人说说楼中楼回复)的说说主人 uin——楼中楼回复 API 的 target_qq,
    源A(自己说说评论)留空,泵侧回退 bot 自己。origin_* 为通知 reply 段
    关联信息(联调修正):通知注入消息带 reply 段引用**原说说**的注入消息
    (napcat quote 式上下文关联),origin_tid 查 seen_store.get_message_id
    得引用目标;浏览动态不填(默认空=无 reply 语义)。
    """

    tid: str
    abstime: str
    uin: str
    nickname: str
    content: str
    image_urls: list[str] = field(default_factory=list)
    appid: int = FEED_APPID_SHUOSHUO
    source: str = "feed"  # "feed"=浏览动态 / "notify"=通知(统一通知通道)
    friend_uin: str = ""  # 通知源B:说说主人(楼中楼 target_qq);源A/浏览动态为空
    # 通知项的去重键:is_new 发现即登记,注入被宿主拒绝/异常时泵侧
    # 据此回退登记(revert),下轮通知轮询重新发现——通知不因一次拒绝永久丢失。
    # notify_reply 键含 parent_comment tid(tid 后缀不可还原),故构造时直接传入。
    dedup_key: str = ""
    origin_tid: str = ""       # 通知关联的原说说 tid(reply 段用)
    origin_content: str = ""   # 原说说正文(reply 段用,注入构造层截前 60 字)
    origin_sender: str = ""    # 原说说作者 uin(reply 段用)
    comment_tid: str = ""      # 通知场景:主评论 tid(qzone_reply 用,工具驱动 2026-09-01)
    comment_uin: str = ""      # 通知场景:主评论作者 uin(楼中楼二元组;源A=评论好友,源B=bot 自己)
    # 该说说近期评论摘要(「昵称:内容」前 3 条,注入历史与上下文素材;
    # 结构化评论区块(get_user_feeds 经 parse_feed_comments_full 填充:
    # 顶层评论+楼中楼;通知项无此数据保持空)
    comments: list["FeedComment"] = field(default_factory=list)
    # 评论总数(响应 cmtnum;QQ 截断 commentlist 时 len(comments)<total,
    # 展示层据此标「前N/共M」;响应未标注时等于列表长度)
    comment_total: int = 0


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
                content=_feed_display_text(feed),
                image_urls=urls,
            )
        )
    return items


def _feed_display_text(feed: dict) -> str:
    """动态正文回退链(联调实证形态):content → 转发原文(rt_con) → [视频]。

    纯图说说(content 空且带 pic)正文保持空——由消息构造层省略文本段,图段承载内容。
    """

    content = str(feed.get("content") or "").strip()
    if not content:
        rt = feed.get("rt_con")
        if isinstance(rt, dict):
            rt_content = str(rt.get("content") or "").strip()
            if rt_content:
                src = str(feed.get("rt_uinname") or "").strip()
                content = f"[转发自{src}]{rt_content}" if src else f"[转发]{rt_content}"
    if not content and (feed.get("video") or []):
        content = "[视频]"
    return content


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
