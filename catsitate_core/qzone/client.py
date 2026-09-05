"""QQ 空间 HTTP 客户端与 cookie 管理(IO 经 callable 注入,离线可测)。

错误纪律:请求失败直接抛出由调用方告警跳过,不做重试循环;
cookie 获取失败/缺 p_skey → 显式告警并返回 None(调用方跳过本轮拉取)。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from catsitate_core.qzone.discovery import FeedDiscovery, parse_like_events, parse_unified_timeline
from catsitate_core.qzone.protocol import (
    extract_callback_json,
    generate_gtk,
    parse_msglist,
)
from catsitate_core.qzone.wire import (
    parse_feed_comments_full,
    CommentItem,
    LikeEvent,
    ReplyItem,
    build_comment_form,
    build_like_form,
    build_publish_form,
    build_reply_form,
    extract_publish_tid,
    parse_feed_comments,
    parse_feed_replies,
    parse_qzone_mentions,
)
from catsitate_core.storage import JsonSnapshot

logger = logging.getLogger(__name__)

COOKIE_DOMAIN = "user.qzone.qq.com"
MSGLIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
# 统一时间线(发现层):1 次调用覆盖全好友动态,与好友数无关
UNIFIED_TIMELINE_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# 登录态/g_tk 失效业务码(联调实证 -3000):命中即抛 QzoneAuthError 触发 cookie 重取
AUTH_ERROR_CODES = {-3000, -10005}
# 操作过于频繁业务码(联调实证 2026-09-02,-10049):同评论线程反复回复/短时间
# 连续写动作触发——不是登录态问题,重试只会更频;调用方据此给出带限制说明的
# 工具回执,让模型自行收敛(不做硬频控,限制写进工具返回)
BIZ_CODE_TOO_FREQUENT = -10049
# 服务端限流业务码(实机实证 -10001,subcode=-30「network busy」):读路径瞬态
# 限流——重试只会加重,处置是跳过本轮等下个拉取间距自然退避,绝不回退逐好友
# 路径放大请求;写路径同样可能出现,回执提示稍后再试
BIZ_CODE_SERVER_BUSY = -10001
_MISSING_PSKEY_WARNED = False  # 每进程告警一次


class QzoneAuthError(RuntimeError):
    """空间登录态失效(p_skey/g_tk 过期)——调用方应使 cookie 缓存失效并重取。"""


class QzoneRateLimitError(RuntimeError):
    """空间服务端限流(network busy)——瞬态,调用方跳过本轮即可,禁止回退
    逐好友等放大请求的路径;拉取间距+失败占距是天然退避。"""


class QzoneBizError(RuntimeError):
    """空间写请求业务错误(非登录态):code 与响应片段随异常携带,
    调用方按 code 生成带限制说明的工具回执(如 -10049 操作频繁)。"""

    def __init__(self, code, text: str = ""):
        self.code = code
        super().__init__(f"空间写请求业务错误: code={code} 响应={text[:120]}")


class CookieManager:
    """空间 cookie:经 adapter API 获取(唯一合规路径),JsonSnapshot 持久化 + 节流。"""

    def __init__(self, snapshot: JsonSnapshot, *, api_call: Callable[..., Awaitable[Any]], refresh_minutes: int) -> None:
        self.snapshot = snapshot
        self.api_call = api_call
        self.refresh_seconds = max(refresh_minutes, 0) * 60
        self._fetched_at: float = 0.0
        self._cookies: dict[str, str] = {}
        self._invalidated = False

    def invalidate(self) -> None:
        """登录态失效标记:清内存态并置失效——下次 get() 跳过
        节流与持久化快照,强制经 adapter 重取;重取失败不回退旧值。"""
        self._cookies = {}
        self._fetched_at = 0.0
        self._invalidated = True

    async def get(self) -> dict[str, str] | None:
        now = time.monotonic()
        if self._cookies and now - self._fetched_at < self.refresh_seconds:
            return self._cookies
        if not self._invalidated:
            cached = self.snapshot.load().get("cookies")
            if isinstance(cached, dict) and cached.get("p_skey"):
                if not self._cookies:  # 进程内首次:用持久化值垫底,仍按节流窗口尝试刷新
                    self._cookies = {str(k): str(v) for k, v in cached.items()}
                    self._fetched_at = now
                    return self._cookies
        try:
            # 联调实证(2026-08-30):adapter API 形态为单 params 关键字(dict 透传给 NapCat 动作)
            result = await self.api_call("adapter.napcat.account.get_cookies", params={"domain": COOKIE_DOMAIN})
        except Exception:
            logger.exception("空间 cookie 获取失败(adapter.napcat.account.get_cookies),本轮跳过")
            return self._cookies or None
        cookies = _extract_cookies(result)
        if not cookies or not cookies.get("p_skey"):
            global _MISSING_PSKEY_WARNED
            if not _MISSING_PSKEY_WARNED:
                _MISSING_PSKEY_WARNED = True
                logger.warning("空间 cookie 缺少 p_skey(响应形态:%s),无法请求空间接口", type(result).__name__)
            return self._cookies or None
        self._cookies = cookies
        self._fetched_at = now
        self._invalidated = False
        # saved_at 存 epoch 秒(time.time)而非 now(monotonic):monotonic 钟只在
        # 进程内可比,跨重启归零,持久化快照里无意义;epoch 秒跨进程可读
        # (节流用的 _fetched_at 仍是 monotonic,不受影响)
        self.snapshot.save({"cookies": cookies, "saved_at": time.time()})
        return cookies


def _parse_cookie_string(raw: str) -> dict[str, str]:
    """解析 NapCat 形态的 cookie 字符串("k1=v1; k2=v2")。"""

    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.strip().partition("=")
        if key.strip():
            out[key.strip()] = value.strip()
    return out


def _extract_cookies(result: Any) -> dict[str, str]:
    """容忍 adapter/NapCat 返回形态:
    {"cookies": {...}} / {"data": {"cookies": {...}}} / {"data": {"cookies": "k=v; ..."}}
    / {"data": "k=v; ..."} / 裸 dict。

    具体形态优先于裸 dict 兜底:裸 dict 无 "cookies"/"data" 键,只作最后候选,
    避免把 {"cookies": {...}} 整体误当 cookie 表。
    """

    if not isinstance(result, dict):
        return {}
    data = result.get("data")
    for candidate in (
        result.get("cookies"),
        data.get("cookies") if isinstance(data, dict) else None,
        data if isinstance(data, str) else None,
        data if isinstance(data, dict) else None,
        result,
    ):
        if isinstance(candidate, dict) and candidate:
            return {str(k): str(v) for k, v in candidate.items()}
        if isinstance(candidate, str) and "=" in candidate:
            parsed = _parse_cookie_string(candidate)
            if parsed:
                return parsed
    return {}


def _is_qq_image_host(host: str) -> bool:
    """QQ 空间图片 CDN 域名判定:*.qpic.cn / *.qq.com。

    动态载荷里的图片 URL 来自远端数据,不可信——非白名单域名可能把登录 Cookie
    带去任意站(或被引去探测内网),一律拒绝下载。
    """

    return host.endswith(".qpic.cn") or host.endswith(".qq.com")


class QzoneClient:
    """空间接口客户端——按 API 分层组织:

    - 发现层:get_unified_timeline(feeds3_html_more 统一时间线,1 次调用
      覆盖全好友动态,返回轻量索引 FeedDiscovery)
    - 内容层:get_user_feeds / get_user_feeds_raw / get_own_feed_comments
      (emotion_cgi_msglist_v6 指定用户说说,返回完整实体 FeedItem / 原始载荷)
    - 写路径:do_like / do_comment / do_reply / do_publish(点赞 / 评论 / 楼中楼回复 / 发表说说)
    - 基础通道:_request(读 GET)/ _post(写 POST)/ download_image(图片 CDN);
      cookie 经 CookieManager 注入,身份参数(bot_uin)由装配层传入
    """

    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"

    def __init__(
        self,
        *,
        cookie_provider: Callable[[], Awaitable[dict[str, str] | None]],
        fetch: Callable[..., Awaitable[tuple[int, Any]]],
        timeout_ms: int,
        bot_uin: str = "",
    ) -> None:
        self.cookie_provider = cookie_provider
        self.fetch = fetch
        self.timeout_ms = timeout_ms
        # 写路径身份参数(opuin/qzreferrer/topicId.uin)——装配时传 favorability.bot_user_id
        self.bot_uin = str(bot_uin or "")

    async def _request(self, method: str, url: str, *, params: dict, data: dict | None = None, binary: bool = False, referer: str = "https://user.qzone.qq.com/") -> tuple[int, Any]:
        cookies = await self.cookie_provider()
        if not cookies:
            raise RuntimeError("空间 cookie 不可用,跳过请求")
        # UA 必带(联调实证:无 UA 空间接口直接 500 空体)
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            "User-Agent": BROWSER_UA,
            "Referer": referer,
        }
        if binary and not _is_qq_image_host(urlparse(url).hostname or ""):
            # 深度防御:白名单已在 download_image 入口拦截,此处兜底
            # ——非 QQ 系域名的二进制请求一律不带登录 Cookie
            headers["Cookie"] = ""
        params = dict(params)
        if not binary:
            # g_tk 只用于空间 cgi 接口;图片 CDN 是签名 URL,附加查询参数会破坏签名致 404
            params.setdefault("g_tk", generate_gtk(cookies.get("p_skey", "")))
        status, body = await self.fetch(method, url, params=params, headers=headers, timeout_ms=self.timeout_ms)
        return status, body

    async def _fetch_msglist(self, *, target_uin: str, num: int, pos: int = 0) -> dict:
        """拉取指定用户说说列表的原始 msglist 载荷(get_user_feeds 与自评拉取共用请求+校验)。
        pos 为分页偏移(0=首页,翻页=(页码-1)*每页条数)。"""

        params = {
            "uin": str(target_uin), "ftype": "0", "sort": "0", "pos": str(max(int(pos), 0)), "num": str(num),
            "replynum": "100", "callback": "_preloadCallback", "code_version": "1",
            "format": "jsonp", "need_comment": "1", "need_private_comment": "1",
        }
        status, raw = await self._request(
            "GET", MSGLIST_URL, params=params, referer=f"https://user.qzone.qq.com/{target_uin}"
        )
        if status != 200:
            raise RuntimeError(f"空间说说列表请求失败(uin={target_uin}): HTTP {status}")
        # fetch 契约统一返回 bytes;jsonp 响应为 UTF-8 文本,严格解码
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        payload = extract_callback_json(text)
        # 注意不可用 `code or -1`:code=0 是成功码,or 短路会误判
        code = payload.get("code")
        if code is not None and int(code) in AUTH_ERROR_CODES:
            raise QzoneAuthError(f"空间登录态失效(uin={target_uin}): code={code}")
        if code is not None and int(code) == BIZ_CODE_SERVER_BUSY:
            raise QzoneRateLimitError(f"空间服务限流(uin={target_uin}): code={code}")
        if code is None or int(code) != 0:
            raise RuntimeError(f"空间说说列表返回业务错误(uin={target_uin}): code={payload.get('code')}")
        return payload

    async def get_user_feeds(self, *, target_uin: str, nickname: str, num: int = 5,
                             page: int = 1) -> list:
        """拉取指定好友最近说说(联调实证参数集:jsonp+need_comment,Referer 指向目标空间)。

        page 为页码(1 起,透传 msglist pos 偏移)。同一原始载荷两用不发第二次
        请求:parse_msglist 出说说实体,parse_feed_comments_full 出结构化评论
        区块(顶层+楼中楼+总数)填 FeedItem.comments/comment_total——浏览注入、
        详情工具与评论级锚(comment_map)共用。"""

        payload = await self._fetch_msglist(
            target_uin=target_uin, num=num, pos=(max(int(page), 1) - 1) * num)
        feeds = parse_msglist(payload, target_uin=str(target_uin), nickname=nickname)
        blocks = parse_feed_comments_full(payload)
        for f in feeds:
            block = blocks.get(f.tid)
            if block is not None:
                f.comments = block.comments
                f.comment_total = block.total
            # 评论/楼中楼正文里的 @{uin,nick,...} 机器格式解析为可读 @昵称
            # (联调实证 2026-09-03:楼中楼回复正文带机器 @ 原样泄漏进详情输出;
            # 通知路径同款解析,此处对齐——detail 与浏览注入两路共用此数据)
            for c in f.comments:
                c.content = parse_qzone_mentions(c.content, bot_uin=self.bot_uin)
                for r in c.replies:
                    r.content = parse_qzone_mentions(r.content, bot_uin=self.bot_uin)
        return feeds

    async def get_user_feeds_raw(self, *, target_uin: str, num: int = 5) -> dict:
        """返回 msglist 原始 payload(含 commentlist/list_3),供 parse_feed_replies 消费。

        统一通知通道源B:楼中楼解析需要原始载荷(parse_msglist 会丢
        commentlist),薄封装 _fetch_msglist 不重复实现请求与校验。
        """
        return await self._fetch_msglist(target_uin=target_uin, num=num)

    async def _fetch_unified(self, *, count: int, scope: int = 0,
                             begintime: str | int | None = None) -> str:
        """拉取统一时间线原始响应文本(feeds3_html_more,发现层基础通道)。

        翻页为 **begintime 游标**(2026-09-03 双路逆向+开源交叉实证):顶层参数
        begintime=上页响应 main.begintime(epoch 秒),续页仅此一参即可;旧 begin
        偏移被服务端无视(实测 0/5/10/50 同响应),已删除。scope:0=小窗口流
        (实测 2 天窗口且本账号仅自己动态)、1=「与我相关」(源C 赞事件,经
        _fetch_likes_raw)、**2=全好友动态流(默认,7 天窗口+游标可回溯)**。
        响应外层 JSON、内层为 JS 对象字面量(见 discovery 模块)——不走 callback
        截取/JSON 解码,原文直返交 parse_unified_timeline 消费。外层 code 校验
        与读/写路径同一纪律:登录态失效抛 QzoneAuthError,非 0 业务码/畸形 200
        显式 RuntimeError,不静默当空时间线。
        """
        params = {
            "uin": self.bot_uin, "format": "json", "count": str(count),
            "update": "1", "scope": str(scope), "filter": "all",
        }
        if begintime is not None and str(begintime).strip():
            params["begintime"] = str(begintime).strip()
        status, raw = await self._request(
            "GET", UNIFIED_TIMELINE_URL, params=params,
            referer=f"https://user.qzone.qq.com/{self.bot_uin}/home",
        )
        if status != 200:
            raise RuntimeError(f"空间统一时间线请求失败(uin={self.bot_uin}): HTTP {status}")
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        # 外层 JSON 壳以 "code":0 起头,首个匹配即外层业务码(内层 JS 数据在其后);
        # 键与冒号间容忍空白(兼容壳层格式微差)
        code_match = re.search(r'"code"\s*:\s*(-?\d+)', text)
        if code_match is None:
            raise RuntimeError(f"空间统一时间线响应不可解析: {text[:120]}")
        code = int(code_match.group(1))
        if code in AUTH_ERROR_CODES:
            raise QzoneAuthError(f"空间登录态失效(统一时间线): code={code}")
        if code == BIZ_CODE_SERVER_BUSY:
            raise QzoneRateLimitError(f"空间服务限流(统一时间线): code={code} 响应={text[:120]}")
        if code != 0:
            raise RuntimeError(f"空间统一时间线返回业务错误: code={code} 响应={text[:120]}")
        return text

    @staticmethod
    def extract_timeline_cursor(text: str) -> str:
        """从响应文本提取下一页游标(main.begintime,externparam 的 basetime
        交叉校验,2026-09-03 实证两者恒等)。无游标返回空串(调用方终止)。"""
        m = re.search(r"begintime[\"']?\s*[:=]\s*[\"']?(\d{10})", str(text or ""))
        if m:
            return m.group(1)
        e = re.search(r"basetime=(\d{10})", str(text or ""))
        return e.group(1) if e else ""

    async def get_unified_timeline(self, *, count: int = 20,
                                   begintime: str | int | None = None,
                                   scope: int = 2) -> tuple[list[FeedDiscovery], str]:
        """发现层 API:统一时间线好友动态流(与好友数无关)。

        scope 默认 2(全好友动态流,7 天窗口;2026-09-03 实证——旧 scope=0 实为
        2 天小窗口且本账号只见自己动态,好友发现形同虚设)。begintime 为续页
        游标(取上页返回的第二元素)。返回 (FeedDiscovery 列表, 下一页游标);
        游标为空串=到底。appid 不过滤——说说(311)筛选与新 tid 判重由调用方
        决定;完整正文/图片由充实层 get_user_feeds 按作者分组拉取。
        """
        text = await self._fetch_unified(count=count, scope=scope, begintime=begintime)
        return parse_unified_timeline(text), self.extract_timeline_cursor(text)

    async def _fetch_likes_raw(self, *, count: int) -> str:
        """拉取「与我相关」流原始文本(feeds3_html_more?scope=1,源C 赞事件输入)。"""
        return await self._fetch_unified(count=count, scope=1)

    async def get_like_events(self, *, count: int = 30) -> list[LikeEvent]:
        """拉取「与我相关」赞事件(feeds3_html_more?scope=1)。"""
        text = await self._fetch_likes_raw(count=count)
        return parse_like_events(text)

    async def _post(self, url: str, *, form: dict, referer_uin: str, extra_params: dict | None = None) -> dict:
        """写路径 POST 通道(独立于 _request:读路径为 GET 语义,参数全进 query;
        写路径为 params=g_tk + form 表单,且需 Origin/Content-Type 头)。
        extra_params 为 g_tk 之外的查询参数(发布端点需带 uin,上游实现同款)。

        失败直接抛出由调用方告警跳过,不重试(动作 API 失败即告警的固定纪律)。
        """
        cookies = await self.cookie_provider()
        if not cookies:
            raise RuntimeError("空间 cookie 不可用,跳过请求")
        # g_tk 保持 int(与 _request 读路径一致,httpx 对 params 原生值直接编码)
        params = {"g_tk": generate_gtk(cookies.get("p_skey", ""))}
        if extra_params:
            params.update(extra_params)
        headers = {
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            "User-Agent": BROWSER_UA,
            "Referer": f"https://user.qzone.qq.com/{referer_uin}",
            "Origin": "https://user.qzone.qq.com",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        status, raw = await self.fetch("POST", url, params=params, headers=headers,
                                       timeout_ms=self.timeout_ms, data=form)
        if status != 200:
            raise RuntimeError(f"空间写请求失败: HTTP {status}")
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        # 畸形 200 响应(网关错误页/截断或非对象 JSON)统一 RuntimeError,
        # 不让原始解析异常类型(JSONDecodeError/AttributeError)泄漏到调用方。
        # format=fs 响应为 HTML 包裹 frameElement.callback({...}),前缀超 60 字符——
        # 找 callback( 而非首 60 字符判定
        try:
            marker = "frameElement.callback("
            if marker in text:
                idx = text.rindex(marker)
                payload = json.loads(text[idx + len(marker): text.rindex(")")])
            elif "(" in text[:60]:
                payload = extract_callback_json(text)
            else:
                payload = json.loads(text)
            code = payload.get("code")
        except (ValueError, AttributeError) as e:
            raise RuntimeError(f"空间写响应不可解析: {text[:120]}") from e
        if code is not None and int(code) in AUTH_ERROR_CODES:
            raise QzoneAuthError(f"空间登录态失效: code={code}")
        if code is None or int(code) != 0:
            raise QzoneBizError(code, text)
        return payload

    async def do_like(self, *, fid: str, target_qq: str) -> bool:
        """点赞指定好友说说(internal_dolike_app,表单由 wire.build_like_form 构造)。"""
        form = build_like_form(fid=fid, target_qq=target_qq,
                               bot_uin=self.bot_uin, now_epoch=time.time())
        await self._post(self.DOLIKE_URL, form=form, referer_uin=self.bot_uin)
        return True

    async def do_comment(self, *, fid: str, target_qq: str, content: str) -> bool:
        """评论指定好友说说(emotion_cgi_re_feeds,响应为 format=fs 的 callback 包裹)。"""
        form = build_comment_form(fid=fid, target_qq=target_qq, bot_uin=self.bot_uin, content=content)
        await self._post(self.COMMENT_URL, form=form, referer_uin=self.bot_uin)
        return True

    async def do_reply(self, *, fid: str, target_qq: str, comment_tid: str, comment_uin: str,
                       comment_nick: str, content: str, at_uin: str = "", at_nick: str = "") -> bool:
        """楼中楼回复评论(同评论端点 + commentId/commentUin 二元组匹配主评论;
        at_uin/at_nick 为 @ 前缀目标,与二元组解耦——缺省 @ 主评论作者)。"""
        form = build_reply_form(fid=fid, target_qq=target_qq, bot_uin=self.bot_uin,
                                comment_tid=comment_tid, comment_uin=comment_uin,
                                comment_nick=comment_nick, content=content,
                                at_uin=at_uin, at_nick=at_nick)
        await self._post(self.COMMENT_URL, form=form, referer_uin=self.bot_uin)
        return True

    async def do_publish(self, *, content: str) -> str:
        """发表一条纯文本说说(emotion_cgi_publish_v6,表单由 wire.build_publish_form
        构造;查询串除 g_tk 外带 uin,与上游 Maizone publish_emotion 请求一致;
        format=json 响应为纯 JSON)。

        返回新说说 tid(wire.extract_publish_tid 提取),取不到为空串——发布已
        远端成功,仅影响回注锚,由调用方告警,不误报发布失败。
        """

        form = build_publish_form(content=content, bot_uin=self.bot_uin)
        payload = await self._post(self.PUBLISH_URL, form=form, referer_uin=self.bot_uin,
                                   extra_params={"uin": self.bot_uin})
        return extract_publish_tid(payload)

    async def get_own_feed_comments(
        self, *, bot_uin: str, num: int = 10
    ) -> tuple[dict[str, list[CommentItem]], dict[str, str], list[ReplyItem]]:
        """拉取自己说说下的好友评论与 bot 评论的楼中楼回复(通知源A 的输入)。

        单次 msglist 请求产出三视图(同载荷补跑解析,不发第二次请求):
        评论映射(feed_tid → [CommentItem],wire.parse_feed_comments)、正文
        上下文(feed_tid → 显示文本,转发/视频回退链沿用 parse_msglist——
        get_user_feeds 解析会丢 commentlist,故两用共用 _fetch_msglist 的原始
        载荷)与楼中楼回复列表(wire.parse_feed_replies,bot 评论下的 list_3)
        ——自己说说下载荷本就含 bot 评论的楼中楼,补跑解析即可让源A 覆盖
        「好友评论 → bot 楼中楼回复 → 好友再回复」线程,与源B 共用同解析层。
        """
        payload = await self._fetch_msglist(target_uin=bot_uin, num=num)
        comments = parse_feed_comments(payload)
        feeds = parse_msglist(payload, target_uin=str(bot_uin), nickname="我")
        ctx = {f.tid: f.content for f in feeds}
        replies = parse_feed_replies(payload, bot_uin=str(bot_uin))
        return comments, ctx, replies

    async def download_image(self, url: str) -> bytes | None:
        """下载原图,失败返回 None(调用方以占位注入)。防盗链头带上 Referer。

        域名白名单:仅 *.qpic.cn/*.qq.com——动态载荷的 URL 不可信,
        非白名单域拒绝下载(防 Cookie 外带与内网探测)。体积不加插件侧上限:
        主程序入站链路对过大图片自有压缩/丢弃处理。
        读路径例外:CDN 偶发瞬态失败(联调实证 404),单次重试;动作 API 的
        「失败不重试」纪律不适用于此。次数固定 1,与动作 API 无关。
        """

        import asyncio as _asyncio

        host = urlparse(url).hostname or ""
        if not _is_qq_image_host(host):
            logger.warning("空间图片域名不在白名单(%s),拒绝下载", host)
            return None

        for attempt in (1, 2):
            status, body = await self._request("GET", url, params={}, binary=True)
            if status == 200:
                # fetch 契约统一 bytes;防御分支容忍旧 str 形态(仅限 latin-1 安全字节)
                return body if isinstance(body, (bytes, bytearray)) else str(body).encode("latin-1", errors="ignore")
            if attempt == 1:
                await _asyncio.sleep(1.0)
        logger.warning("空间图片下载失败(重试后仍失败): %s", url)
        return None
