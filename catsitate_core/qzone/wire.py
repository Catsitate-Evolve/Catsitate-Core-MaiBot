"""QQ 空间写路径纯函数(参数集为 Maizone qzone_api.py 实证,联调期经 jsdelivr 复核)。

comment/reply 响应为 format=fs 的 frameElement.callback 包裹——复用
protocol.extract_callback_json 通用截取。仅纯函数,IO 在 client.py。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def parse_qzone_mentions(text: str, *, bot_uin: str) -> str:
    """将 QQ 空间 @{uin:xxx,nick:xxx,...} 格式解析为可读 @昵称(提示词可读性 2026-09-01)。

    好友回复正文里的 @ 是花括号机器格式,直接拼进通知会糊住语义——解析为
    「@昵称 」(后接一个空格,拟 QQ 客户端 @ 展示形态;原文紧跟的至多一个
    空格被合并,不产生双空格)。缺 nick 回退 @uin;无 uin 的畸形花括号原样
    保留(不吞文本)。bot_uin 仅作语境(Q2=a 用户裁定:@bot 自己也保留,
    不过滤)。
    """
    del bot_uin

    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        uin_m = re.search(r"uin:(\d+)", inner)
        nick_m = re.search(r"nick:([^,}]+)", inner)
        if not uin_m:
            return m.group(0)
        nick = nick_m.group(1).strip() if nick_m else uin_m.group(1)
        return f"@{nick} "

    return re.sub(r"@\{([^}]+)\} ?", _replace, text)


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
                     comment_uin: str, comment_nick: str, content: str,
                     at_uin: str = "", at_nick: str = "") -> dict:
    """楼中楼回复表单(同评论端点 + commentId/commentUin;@ 前缀为 QQ 空间回复格式)。

    二元组与 @ 目标解耦(工具驱动 2026-09-01):commentId+commentUin 精确匹配
    主评论(源B=bot 自己的评论线程头),而 @ 的是正在对话的评论者/回复者
    ——两者在「回复他人评论」场景重合(缺省 at_* 回退二元组作者,旧行为不变)。
    """

    form = build_comment_form(fid=fid, target_qq=target_qq, bot_uin=bot_uin, content=content)
    at_target_uin = at_uin or comment_uin
    at_target_nick = at_nick or comment_nick or at_target_uin
    form["content"] = f"@{{uin:{at_target_uin},nick:{at_target_nick},auto:1}}{content}"
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


@dataclass
class ReplyItem:
    """bot 评论下的一条楼中楼回复(msglist.commentlist[].list_3 条目)。

    feed_content 为所属说说正文(通知 reply 段引用预览用,通知源B构造 FeedItem
    时截 30 字传入);parent_comment_content 为被回复的 bot 主评论正文(楼中楼
    上下文,可读性优化 2026-09-01:通知正文引用「bot 原评论前 20 字」);旧
    调用方不填默认空串。
    """

    reply_tid: str  # 回复自身 tid
    parent_comment_tid: str  # 被回复的 bot 评论 tid
    feed_tid: str  # 所属说说 tid
    friend_uin: str  # 说说主人(用于意图路由 target_qq)
    uin: str  # 回复者
    nickname: str
    content: str
    create_time: str
    feed_content: str = ""  # 所属说说正文(通知 reply 段引用预览)
    parent_comment_content: str = ""  # 被回复的 bot 主评论正文(楼中楼上下文)


def parse_feed_replies(payload: dict, *, bot_uin: str) -> list[ReplyItem]:
    """解析 msglist 载荷中 bot 评论的楼中楼回复(list_3)。

    在 commentlist 中找 uin==bot_uin 的条目(即 bot 自己的评论),
    解析其 list_3 数组中的每条回复为 ReplyItem(携带主评论正文
    parent_comment_content 供通知正文楼中楼上下文引用)。无 bot 评论/无 list_3/
    字段缺失容错跳过;bot 自己的楼中楼回复跳过(不通知自己)。

    friend_uin(说说主人,意图路由 target_qq)取自载荷 usrinfo.uin——
    联调实测(test_qzone_client.MSGLIST_JSONP):usrinfo 是被拉取者,
    logininfo 是访客(bot)自身。usrinfo 缺失时降级空串并告警(调用方
    不得用空 target 发楼中楼)。
    """
    p = payload or {}
    info = p.get("usrinfo")
    friend_uin = str((info.get("uin") or "")) if isinstance(info, dict) else ""
    out: list[ReplyItem] = []
    for feed in p.get("msglist") or []:
        if not isinstance(feed, dict):
            continue
        feed_tid = str(feed.get("tid") or "")
        for c in feed.get("commentlist") or []:
            if not isinstance(c, dict) or str(c.get("uin") or "") != bot_uin:
                continue
            parent_tid = str(c.get("tid") or "")
            parent_content = str(c.get("content") or "").strip()
            for r in c.get("list_3") or []:
                if not isinstance(r, dict):
                    continue
                uin = str(r.get("uin") or "")
                if not uin or uin == bot_uin:
                    continue  # bot 自己的楼中楼跳过
                out.append(ReplyItem(
                    reply_tid=str(r.get("tid") or ""),
                    parent_comment_tid=parent_tid,
                    feed_tid=feed_tid,
                    friend_uin=friend_uin,
                    uin=uin,
                    nickname=str(r.get("name") or "") or uin,
                    content=str(r.get("content") or "").strip(),
                    create_time=str(r.get("create_time") or ""),
                    feed_content=str(feed.get("content") or "").strip(),
                    parent_comment_content=parent_content,
                ))
    if out and not friend_uin:
        logger.warning(
            "楼中楼解析:载荷缺 usrinfo.uin(说说主人未知),%d 条回复 friend_uin 降级空串", len(out)
        )
    return out
