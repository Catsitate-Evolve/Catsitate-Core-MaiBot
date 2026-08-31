"""QQ 空间 HTTP 客户端与 cookie 管理(IO 经 callable 注入,离线可测)。

错误纪律(spec §6):请求失败直接抛出由调用方告警跳过,不做重试循环;
cookie 获取失败/缺 p_skey → 显式告警并返回 None(调用方跳过本轮拉取)。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from catsitate_core.qzone.protocol import (
    extract_callback_json,
    generate_gtk,
    parse_msglist,
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


class QzoneClient:
    """空间接口客户端(读路径:M1 仅好友动态列表与图片下载)。"""

    def __init__(
        self,
        *,
        cookie_provider: Callable[[], Awaitable[dict[str, str] | None]],
        fetch: Callable[..., Awaitable[tuple[int, str]]],
        timeout_ms: int,
        max_retries: int,
    ) -> None:
        self.cookie_provider = cookie_provider
        self.fetch = fetch
        self.timeout_ms = timeout_ms
        self.max_retries = max(0, int(max_retries))

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
        params = dict(params)
        if not binary:
            # g_tk 只用于空间 cgi 接口;图片 CDN 是签名 URL,附加查询参数会破坏签名致 404(联调缺陷#8)
            params.setdefault("g_tk", generate_gtk(cookies.get("p_skey", "")))
        status, body = await self.fetch(method, url, params=params, headers=headers, timeout_ms=self.timeout_ms)
        if binary:
            return status, body
        return status, body

    async def get_user_feeds(self, *, target_uin: str, nickname: str, num: int = 5) -> list:
        """拉取指定好友最近说说(联调实证参数集:jsonp+need_comment,Referer 指向目标空间)。"""

        params = {
            "uin": str(target_uin), "ftype": "0", "sort": "0", "pos": "0", "num": str(num),
            "replynum": "100", "callback": "_preloadCallback", "code_version": "1",
            "format": "jsonp", "need_comment": "1", "need_private_comment": "1",
        }
        status, text = await self._request(
            "GET", MSGLIST_URL, params=params, referer=f"https://user.qzone.qq.com/{target_uin}"
        )
        if status != 200:
            raise RuntimeError(f"空间说说列表请求失败(uin={target_uin}): HTTP {status}")
        payload = extract_callback_json(text)
        # 注意不可用 `code or -1`:code=0 是成功码,or 短路会误判(内部不一致最小修复)
        code = payload.get("code")
        if code is not None and int(code) in AUTH_ERROR_CODES:
            raise QzoneAuthError(f"空间登录态失效(uin={target_uin}): code={code}")
        if code is None or int(code) != 0:
            raise RuntimeError(f"空间说说列表返回业务错误(uin={target_uin}): code={payload.get('code')}")
        return parse_msglist(payload, target_uin=str(target_uin), nickname=nickname)

    async def download_image(self, url: str) -> bytes | None:
        """下载原图,失败返回 None(调用方以占位注入)。防盗链头带上 Referer。

        体积不加插件侧上限(用户裁定 2026-08-31):主程序入站链路对过大图片
        自有压缩/丢弃处理。读路径例外:CDN 偶发瞬态失败(联调实证 404),
        单次重试;动作 API 的「失败不重试」纪律不适用于此。
        """

        import asyncio as _asyncio

        for attempt in (1, 2):
            status, body = await self._request("GET", url, params={}, binary=True)
            if status == 200:
                return body.encode("latin-1") if isinstance(body, str) else body
            if attempt == 1:
                await _asyncio.sleep(1.0)
        logger.warning("空间图片下载失败(重试后仍失败): %s", url)
        return None
