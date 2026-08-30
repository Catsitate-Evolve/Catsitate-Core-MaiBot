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
_MISSING_PSKEY_WARNED = False  # 每进程告警一次


class CookieManager:
    """空间 cookie:经 adapter API 获取(唯一合规路径),JsonSnapshot 持久化 + 节流。"""

    def __init__(self, snapshot: JsonSnapshot, *, api_call: Callable[..., Awaitable[Any]], refresh_minutes: int) -> None:
        self.snapshot = snapshot
        self.api_call = api_call
        self.refresh_seconds = max(refresh_minutes, 0) * 60
        self._fetched_at: float = 0.0
        self._cookies: dict[str, str] = {}

    async def get(self) -> dict[str, str] | None:
        now = time.monotonic()
        if self._cookies and now - self._fetched_at < self.refresh_seconds:
            return self._cookies
        cached = self.snapshot.load().get("cookies")
        if isinstance(cached, dict) and cached.get("p_skey"):
            if not self._cookies:  # 进程内首次:用持久化值垫底,仍按节流窗口尝试刷新
                self._cookies = {str(k): str(v) for k, v in cached.items()}
                self._fetched_at = now
                return self._cookies
        try:
            result = await self.api_call("adapter.napcat.account.get_cookies", domain=COOKIE_DOMAIN)
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
        self.snapshot.save({"cookies": cookies, "saved_at": now})
        return cookies


def _extract_cookies(result: Any) -> dict[str, str]:
    """容忍 adapter 返回形态:{"cookies": {...}} / {"data": {"cookies": {...}}} / 裸 dict。

    具体形态优先于裸 dict 兜底:裸 dict 无 "cookies" 键,只作最后候选,
    避免把 {"cookies": {...}} 整体误当 cookie 表(内部不一致最小修复)。
    """

    if not isinstance(result, dict):
        return {}
    for candidate in (
        result.get("cookies"),
        (result.get("data") or {}).get("cookies"),
        result,
    ):
        if isinstance(candidate, dict) and candidate:
            return {str(k): str(v) for k, v in candidate.items()}
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

    async def _request(self, method: str, url: str, *, params: dict, data: dict | None = None, binary: bool = False) -> tuple[int, Any]:
        cookies = await self.cookie_provider()
        if not cookies:
            raise RuntimeError("空间 cookie 不可用,跳过请求")
        headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
        if not binary:
            headers["Referer"] = "https://user.qzone.qq.com/"
        params = dict(params)
        params.setdefault("g_tk", generate_gtk(cookies.get("p_skey", "")))
        status, body = await self.fetch(method, url, params=params, headers=headers, timeout_ms=self.timeout_ms)
        if binary:
            return status, body
        return status, body

    async def get_friend_feeds(self, *, pos: int = 0, num: int = 10) -> tuple[list, int]:
        params = {
            "ftype": "0", "sort": "0", "pos": str(pos), "num": str(num), "replynum": "100",
            "code_version": "1", "format": "json", "need_private_comment": "1",
        }
        status, text = await self._request("GET", MSGLIST_URL, params=params)
        if status != 200:
            raise RuntimeError(f"空间动态列表请求失败: HTTP {status}")
        payload = extract_callback_json(text)
        # 注意不可用 `code or -1`:code=0 是成功码,or 短路会误判(内部不一致最小修复)
        code = payload.get("code")
        if code is None or int(code) != 0:
            raise RuntimeError(f"空间动态列表返回业务错误: code={payload.get('code')}")
        return parse_msglist(payload)

    async def download_image(self, url: str, *, max_kb: int) -> bytes | None:
        """下载原图;超过体积上限返回 None(调用方以占位注入)。防盗链头带上 Referer。"""

        # fetch 契约的关键字是 headers,包装参数名必须一致;包装内部须引用原 fetch,
        # 不能写 self.fetch(替换期间 self.fetch 即包装自身,会无限递归)——内部不一致最小修复
        original_fetch = self.fetch

        async def _fetch(method: str, u: str, *, params: dict, headers: dict, timeout_ms: int):
            headers = {**headers, "Referer": "https://user.qzone.qq.com/"}
            return await original_fetch(method, u, params=params, headers=headers, timeout_ms=timeout_ms)

        self.fetch = _fetch
        try:
            status, body = await self._request("GET", url, params={}, binary=True)
        finally:
            self.fetch = original_fetch
        if status != 200:
            logger.warning("空间图片下载失败(HTTP %s): %s", status, url)
            return None
        data = body.encode("latin-1") if isinstance(body, str) else body
        if len(data) > max_kb * 1024:
            logger.warning("空间图片超体积上限(%d KB): %s", max_kb, url)
            return None
        return data
