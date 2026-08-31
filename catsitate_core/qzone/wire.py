"""QQ 空间写路径纯函数(参数集为 Maizone qzone_api.py 实证,联调期经 jsdelivr 复核)。

comment/reply 响应为 format=fs 的 frameElement.callback 包裹——复用
protocol.extract_callback_json 通用截取。仅纯函数,IO 在 client.py。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommentItem:
    """自己说说下的一条好友评论(msglist.commentlist 条目)。"""

    comment_tid: str
    uin: str
    nickname: str
    content: str
    create_time: str


def build_like_form(*, fid: str, target_qq: str, bot_uin: str, now_epoch: float) -> dict:
    """点赞 internal_dolike_app 的表单(unikey/curkey 为动态唯一标识,appid=311)。"""
    unikey = f"http://user.qzone.qq.com/{target_qq}/mood/{fid}"
    return {
        "qzreferrer": f"https://user.qzone.qq.com/{bot_uin}",
        "opuin": bot_uin,
        "unikey": unikey,
        "curkey": unikey,
        "appid": 311,
        "from": 1,
        "typeid": 0,
        "abstime": int(now_epoch),
        "fid": fid,
        "active": 0,
        "format": "json",
        "fupdate": 1,
    }


def build_comment_form(*, fid: str, target_qq: str, bot_uin: str, content: str) -> dict:
    """评论 emotion_cgi_re_feeds 的表单(topicId={host}_{fid}__1,feedsType=100,format=fs)。"""
    return {
        "topicId": f"{target_qq}_{fid}__1",
        "uin": bot_uin,
        "hostUin": target_qq,
        "feedsType": 100,
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "plat": "qzone",
        "source": "ic",
        "platformid": 52,
        "format": "fs",
        "ref": "feeds",
        "content": content,
    }


def build_reply_form(*, fid: str, target_qq: str, bot_uin: str, comment_tid: str,
                     comment_uin: str, comment_nick: str, content: str) -> dict:
    """楼中楼回复表单(同评论端点 + commentId/commentUin;@ 前缀为 QQ 空间回复格式)。"""
    form = build_comment_form(fid=fid, target_qq=target_qq, bot_uin=bot_uin, content=content)
    form["content"] = f"@{{uin:{comment_uin},nick:{comment_nick},auto:1}}{content}"
    form["commentId"] = str(comment_tid)
    form["commentUin"] = str(comment_uin)
    form["richtype"] = ""
    form["richval"] = ""
    form["paramstr"] = "1"
    return form


def parse_feed_comments(payload: dict) -> dict[str, list[CommentItem]]:
    """解析 msglist 载荷的 commentlist → {feed_tid: [CommentItem]}。

    无评论/缺字段容错跳过;数值 tid 归一为字符串。
    """
    out: dict[str, list[CommentItem]] = {}
    for feed in (payload or {}).get("msglist") or []:
        if not isinstance(feed, dict):
            continue
        tid = str(feed.get("tid") or "")
        items: list[CommentItem] = []
        for c in feed.get("commentlist") or []:
            if not isinstance(c, dict):
                continue
            uin = str(c.get("uin") or "")
            if not uin:
                continue
            items.append(CommentItem(
                comment_tid=str(c.get("tid") or ""),
                uin=uin,
                nickname=str(c.get("name") or "") or uin,
                content=str(c.get("content") or "").strip(),
                create_time=str(c.get("create_time") or ""),
            ))
        if tid and items:
            out[tid] = items
    return out
