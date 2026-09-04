"""QQ 空间写路径纯函数(参数集为 Maizone qzone_api.py 实证,联调期经 jsdelivr 复核)。

comment/reply 响应为 format=fs 的 frameElement.callback 包裹——复用
protocol.extract_callback_json 通用截取;publish 表单 format=json,响应为
纯 JSON 无包裹。仅纯函数,IO 在 client.py。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

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


def build_publish_form(*, content: str, bot_uin: str) -> dict:
    """发表纯文本说说的表单(emotion_cgi_publish_v6 端点)。

    参数集对照上游开源实现 Maizone 的 publish_emotion 核实:纯文本说说不带
    pic_bo/richtype/richval(带图发布需先走图片上传通道生成 pic_bo,当前不
    支持);who 为「以自己身份发表」的固定标志 "1"(不是 QQ 号,空间主人由
    hostuin 承载);format=json 表示响应为纯 JSON,无 frameElement.callback
    包裹;qzreferrer 指向自己的空间主页(与点赞表单的防伪造引用头同源)。
    """
    return {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "who": "1",
        "con": content,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": bot_uin,
        "code_version": "1",
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{bot_uin}",
    }


def extract_publish_tid(payload: dict) -> str:
    """从发布响应载荷提取新说说 tid。

    键形态按端点历史版本逐层尝试,共 5 形态(先取先得):
    顶层 tid / data.tid / data.newtid / 顶层 newtid / content[0].tid
    (数值 tid 归一为字符串);取不到返回空串由调用方告警——发布本身已
    成功,tid 缺失只影响回注锚,不应误报发布失败。
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for candidate in (payload.get("tid"), data.get("tid"), data.get("newtid"), payload.get("newtid")):
        value = str(candidate or "").strip()
        if value:
            return value
    content = payload.get("content") if isinstance(payload.get("content"), list) else []
    if content and isinstance(content[0], dict):
        value = str(content[0].get("tid") or "").strip()
        if value:
            return value
    return ""


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
class LikeEvent:
    """「与我相关」流中的赞事件(键=赞的人_说说主人_说说哈希)。"""

    like_key: str
    liker_uin: str
    liker_nickname: str
    owner_uin: str
    target_tid: str
    create_time: str = ""  # epoch 秒字符串,缺失为空(不编造时间)


@dataclass
class FeedReplyEntry:
    """一条楼中楼回复(commentlist[].list_3 条目)——注入与详情展示用。"""

    reply_tid: str
    uin: str
    nickname: str
    content: str
    create_time: str


@dataclass
class FeedComment:
    """一条顶层评论(commentlist 条目,含楼中楼)——注入与详情展示用;
    comment_tid 是 qzone_reply 的锚,replies 是楼中楼(可能被 QQ 截断,
    reply_total 为响应标注的总数,超展开时用于「共N条」标注)。"""

    comment_tid: str
    uin: str
    nickname: str
    content: str
    create_time: str
    replies: list[FeedReplyEntry] = field(default_factory=list)
    reply_total: int = 0


@dataclass
class CommentBlock:
    """一条说说的评论区块(结构化):comments 为响应给出的顶层评论列表
    (QQ 可能截断),total 为响应标注的评论总数(cmtnum;0=响应未标注)。"""

    comments: list[FeedComment] = field(default_factory=list)
    total: int = 0


def parse_feed_comments_full(payload: dict) -> dict[str, CommentBlock]:
    """解析 msglist 载荷的完整评论区块(顶层评论+楼中楼)→ {feed_tid: CommentBlock}。

    与 parse_feed_comments(通知源A 用,只取顶层四字段)的分工:本函数服务
    浏览注入与详情工具——楼中楼取 commentlist[].list_3,评论总数取 feed 的
    cmtnum(未标注时回退为列表长度,不臆造);畸形条目/缺 uin 容错跳过,
    数值 tid 归一为字符串。"""

    out: dict[str, CommentBlock] = {}
    for feed in (payload or {}).get("msglist") or []:
        if not isinstance(feed, dict):
            continue
        tid = str(feed.get("tid") or "")
        raw_list = feed.get("commentlist") or []
        if not tid or not isinstance(raw_list, list):
            continue
        comments: list[FeedComment] = []
        for c in raw_list:
            if not isinstance(c, dict):
                continue
            uin = str(c.get("uin") or "")
            comment_tid = str(c.get("tid") or "")
            if not uin or not comment_tid:
                continue
            replies: list[FeedReplyEntry] = []
            for r in c.get("list_3") or []:
                if not isinstance(r, dict):
                    continue
                r_uin = str(r.get("uin") or "")
                r_tid = str(r.get("tid") or "")
                if not r_uin or not r_tid:
                    continue
                replies.append(FeedReplyEntry(
                    reply_tid=r_tid, uin=r_uin,
                    nickname=str(r.get("name") or "") or r_uin,
                    content=str(r.get("content") or "").strip(),
                    create_time=str(r.get("create_time") or ""),
                ))
            try:
                reply_total = int(str(c.get("total") or len(replies)))
            except (TypeError, ValueError):
                reply_total = len(replies)
            comments.append(FeedComment(
                comment_tid=comment_tid, uin=uin,
                nickname=str(c.get("name") or "") or uin,
                content=str(c.get("content") or "").strip(),
                create_time=str(c.get("create_time") or ""),
                replies=replies, reply_total=reply_total,
            ))
        try:
            total = int(str(feed.get("cmtnum") or len(comments)))
        except (TypeError, ValueError):
            total = len(comments)
        out[tid] = CommentBlock(comments=comments, total=total)
    return out


@dataclass
class ReplyItem:
    """bot 参与的评论线程下的一条楼中楼回复(msglist.commentlist[].list_3 条目)。

    feed_content 为所属说说正文(通知 reply 段引用预览用,通知源B构造 FeedItem
    时截 30 字传入);parent_comment_content 为线程顶层评论正文;bot_reply_content
    为 bot 在该线程最近一条回复正文(通知上下文用:好友楼中楼回复的对象通常是
    bot 的这条回复而非顶层评论——实机抓包实证:QQ 楼中楼全部挂顶层评论的
    list_3,不区分回复谁,故线程内上下文取 bot 自己最近的发言);旧
    调用方不填默认空串。
    """

    reply_tid: str  # 回复自身 tid
    parent_comment_tid: str  # 线程顶层评论 tid(qzone_reply 楼中楼锚)
    feed_tid: str  # 所属说说 tid
    friend_uin: str  # 说说主人(用于意图路由 target_qq)
    uin: str  # 回复者
    nickname: str
    content: str
    create_time: str
    feed_content: str = ""  # 所属说说正文(通知 reply 段引用预览)
    parent_comment_content: str = ""  # 线程顶层评论正文
    parent_comment_uin: str = ""  # 线程顶层评论作者(qzone_reply 二元组锚)
    bot_reply_content: str = ""  # bot 在该线程最近一条回复(通知上下文)


def parse_feed_replies(payload: dict, *, bot_uin: str) -> list[ReplyItem]:
    """解析 msglist 载荷中 bot 参与的评论线程的楼中楼回复(list_3)。

    线程筛选=「bot 参与过」:顶层评论作者是 bot,或 list_3 中存在 bot 的回复
    (实机抓包实证 2026-09-04:QQ 楼中楼一律挂在**顶层评论**的 list_3 下,与
    回复谁无关——「好友评论→bot 楼中楼回复→好友再回复」的常见形态里,顶层
    作者是好友,按顶层作者==bot 筛选会漏掉好友对 bot 回复的回应)。
    bot 未参与的纯好友线程不产出(不通知自己没插过话的对话);
    bot 自己的楼中楼回复跳过(不通知自己);字段缺失容错跳过。

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
            if not isinstance(c, dict):
                continue
            entries = [r for r in c.get("list_3") or [] if isinstance(r, dict)]
            parent_is_bot = str(c.get("uin") or "") == bot_uin
            bot_in_thread = any(str(r.get("uin") or "") == bot_uin for r in entries)
            if not parent_is_bot and not bot_in_thread:
                continue  # bot 未参与的纯好友线程:不通知(不插话他人对话)
            # bot 在该线程最近一条回复(list_3 按时间序,取最后一条 bot 条目)
            bot_reply_content = ""
            for r in entries:
                if str(r.get("uin") or "") == bot_uin:
                    bot_reply_content = str(r.get("content") or "").strip()
            parent_tid = str(c.get("tid") or "")
            parent_content = str(c.get("content") or "").strip()
            parent_uin = str(c.get("uin") or "")
            for r in entries:
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
                    parent_comment_uin=parent_uin,
                    bot_reply_content=bot_reply_content,
                ))
    if out and not friend_uin:
        logger.warning(
            "楼中楼解析:载荷缺 usrinfo.uin(说说主人未知),%d 条回复 friend_uin 降级空串", len(out)
        )
    return out
