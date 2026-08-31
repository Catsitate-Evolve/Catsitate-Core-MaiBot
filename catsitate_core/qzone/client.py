"""QQ 空间 HTTP 客户端与 cookie 管理(IO 经 callable 注入,离线可测)。

错误纪律(spec §6):请求失败直接抛出由调用方告警跳过,不做重试循环;
cookie 获取失败/缺 p_skey → 显式告警并返回 None(调用方跳过本轮拉取)。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from catsitate_core.qzone.protocol import (
    extract_callback_json,
    generate_gtk,
    parse_msglist,
)
from catsitate_core.qzone.wire import (
    CommentItem,
    build_comment_form,
    build_like_form,
    build_reply_form,
    parse_feed_comments,
)
from catsitate_core.storage import JsonSnapshot

logger = logging.getLogger(__name__)

COOKIE_DOMAIN = "user.qzone.qq.com"
MSGLIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# 登录态/g_tk 失效业务码(联调实证 -3000):命中即抛 QzoneAuthError 触发 cookie 重取
AUTH_ERROR_CODES = {-3000, -10005}
_MISSING_PSKEY_WARNED = False  # 每进程告警一次


class QzoneAuthError(RuntimeError):
    """空间登录态失效(p_skey/g_tk 过期)——调用方应使 cookie 缓存失效并重取。"""


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
        """登录态失效标记(联调缺陷#7):清内存态并置失效——下次 get() 跳过
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
            # 联调修正(2026-08-30):adapter API 形态为单 params 关键字(dict 透传给 NapCat 动作)
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
        self.snapshot.save({"cookies": cookies, "saved_at": now})
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
    避免把 {"cookies": {...}} 整体误当 cookie 表(内部不一致最小修复)。
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
    """QQ 空间图片 CDN 域名判定(深度审查 E-1):*.qpic.cn / *.qq.com。

    动态载荷里的图片 URL 来自远端数据,不可信——非白名单域名可能把登录 Cookie
    带去任意站(或被引去探测内网),一律拒绝下载。
    """

    return host.endswith(".qpic.cn") or host.endswith(".qq.com")


class QzoneClient:
    """空间接口客户端(读路径:M1 好友动态列表与图片下载;写路径:M2 点赞/评论/楼中楼)。"""

    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"

    def __init__(
        self,
        *,
        cookie_provider: Callable[[], Awaitable[dict[str, str] | None]],
        fetch: Callable[..., Awaitable[tuple[int, Any]]],
        timeout_ms: int,
        max_retries: int,
        bot_uin: str = "",
    ) -> None:
        self.cookie_provider = cookie_provider
        self.fetch = fetch
        self.timeout_ms = timeout_ms
        self.max_retries = max(0, int(max_retries))
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
            # 深度防御(深度审查 E-1):白名单已在 download_image 入口拦截,此处兜底
            # ——非 QQ 系域名的二进制请求一律不带登录 Cookie
            headers["Cookie"] = ""
        params = dict(params)
        if not binary:
            # g_tk 只用于空间 cgi 接口;图片 CDN 是签名 URL,附加查询参数会破坏签名致 404(联调缺陷#8)
            params.setdefault("g_tk", generate_gtk(cookies.get("p_skey", "")))
        status, body = await self.fetch(method, url, params=params, headers=headers, timeout_ms=self.timeout_ms)
        return status, body

    async def _fetch_msglist(self, *, target_uin: str, num: int) -> dict:
        """拉取指定用户说说列表的原始 msglist 载荷(get_user_feeds 与自评拉取共用请求+校验)。"""

        params = {
            "uin": str(target_uin), "ftype": "0", "sort": "0", "pos": "0", "num": str(num),
            "replynum": "100", "callback": "_preloadCallback", "code_version": "1",
            "format": "jsonp", "need_comment": "1", "need_private_comment": "1",
        }
        status, raw = await self._request(
            "GET", MSGLIST_URL, params=params, referer=f"https://user.qzone.qq.com/{target_uin}"
        )
        if status != 200:
            raise RuntimeError(f"空间说说列表请求失败(uin={target_uin}): HTTP {status}")
        # fetch 契约统一返回 bytes(联调缺陷#13);jsonp 响应为 UTF-8 文本,严格解码
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        payload = extract_callback_json(text)
        # 注意不可用 `code or -1`:code=0 是成功码,or 短路会误判(内部不一致最小修复)
        code = payload.get("code")
        if code is not None and int(code) in AUTH_ERROR_CODES:
            raise QzoneAuthError(f"空间登录态失效(uin={target_uin}): code={code}")
        if code is None or int(code) != 0:
            raise RuntimeError(f"空间说说列表返回业务错误(uin={target_uin}): code={payload.get('code')}")
        return payload

    async def get_user_feeds(self, *, target_uin: str, nickname: str, num: int = 5) -> list:
        """拉取指定好友最近说说(联调实证参数集:jsonp+need_comment,Referer 指向目标空间)。"""

        payload = await self._fetch_msglist(target_uin=target_uin, num=num)
        return parse_msglist(payload, target_uin=str(target_uin), nickname=nickname)

    async def get_user_feeds_raw(self, *, target_uin: str, num: int = 5) -> dict:
        """返回 msglist 原始 payload(含 commentlist/list_3),供 parse_feed_replies 消费。

        统一通知通道源B(T11):楼中楼解析需要原始载荷(parse_msglist 会丢
        commentlist),薄封装 _fetch_msglist 不重复实现请求与校验。
        """
        return await self._fetch_msglist(target_uin=target_uin, num=num)

    async def _post(self, url: str, *, form: dict, referer_uin: str) -> dict:
        """写路径 POST 通道(独立于 _request:读路径为 GET 语义,参数全进 query;
        写路径为 params=g_tk + form 表单,且需 Origin/Content-Type 头)。

        失败直接抛出由调用方告警跳过,不重试(max_retries 语义=动作 API 失败即告警)。
        """
        cookies = await self.cookie_provider()
        if not cookies:
            raise RuntimeError("空间 cookie 不可用,跳过请求")
        # g_tk 保持 int(与 _request 读路径一致,httpx 对 params 原生值直接编码)
        params = {"g_tk": generate_gtk(cookies.get("p_skey", ""))}
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
        # 批①遗留加固:畸形 200 响应(网关错误页/截断或非对象 JSON)统一 RuntimeError,
        # 不让原始解析异常类型(JSONDecodeError/AttributeError)泄漏到调用方。
        # format=fs 响应为 HTML 包裹 frameElement.callback({...}),前缀超 60 字符——
        # 联调缺陷#16:找 callback( 而非首 60 字符判定
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
            raise RuntimeError(f"空间写请求业务错误: code={payload.get('code')} 响应={text[:120]}")
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
                       comment_nick: str, content: str) -> bool:
        """楼中楼回复自己说说下的好友评论(同评论端点 + commentId/commentUin)。"""
        form = build_reply_form(fid=fid, target_qq=target_qq, bot_uin=self.bot_uin,
                                comment_tid=comment_tid, comment_uin=comment_uin,
                                comment_nick=comment_nick, content=content)
        await self._post(self.COMMENT_URL, form=form, referer_uin=self.bot_uin)
        return True

    async def get_own_feed_comments(
        self, *, bot_uin: str, num: int = 10
    ) -> tuple[dict[str, list[CommentItem]], dict[str, str]]:
        """拉取自己说说下的好友评论(评论回复轮询的输入)。

        单次 msglist 请求产出两视图:评论映射(feed_tid → [CommentItem],
        wire.parse_feed_comments)与正文上下文(feed_tid → 显示文本,转发/视频
        回退链沿用 parse_msglist——get_user_feeds 解析会丢 commentlist,故两用
        共用 _fetch_msglist 的原始载荷,不再发第二次请求)。
        """
        payload = await self._fetch_msglist(target_uin=bot_uin, num=num)
        comments = parse_feed_comments(payload)
        feeds = parse_msglist(payload, target_uin=str(bot_uin), nickname="我")
        ctx = {f.tid: f.content for f in feeds}
        return comments, ctx

    async def download_image(self, url: str) -> bytes | None:
        """下载原图,失败返回 None(调用方以占位注入)。防盗链头带上 Referer。

        域名白名单(深度审查 E-1):仅 *.qpic.cn/*.qq.com——动态载荷的 URL 不可信,
        非白名单域拒绝下载(防 Cookie 外带与内网探测)。体积不加插件侧上限
        (用户裁定 2026-08-31):主程序入站链路对过大图片自有压缩/丢弃处理。
        读路径例外:CDN 偶发瞬态失败(联调实证 404),单次重试;动作 API 的
        「失败不重试」纪律不适用于此。次数固定 1,不消费 max_retries
        (该配置约束 M2 动作 API)。
        """

        import asyncio as _asyncio

        host = urlparse(url).hostname or ""
        if not _is_qq_image_host(host):
            logger.warning("空间图片域名不在白名单(%s),拒绝下载", host)
            return None

        for attempt in (1, 2):
            status, body = await self._request("GET", url, params={}, binary=True)
            if status == 200:
                # fetch 契约统一 bytes(联调缺陷#13);防御分支容忍旧 str 形态(仅限 latin-1 安全字节)
                return body if isinstance(body, (bytes, bytearray)) else str(body).encode("latin-1", errors="ignore")
            if attempt == 1:
                await _asyncio.sleep(1.0)
        logger.warning("空间图片下载失败(重试后仍失败): %s", url)
        return None
