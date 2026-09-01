"""FeedContextRegistry——注入消息的上下文追踪(工具目标解析用,替代意图绑定)。

纯内存 LRU(上限 128,48h 过期)。写入点:泵注入成功后;读取点:qzone_comment/
qzone_reply/qzone_like 工具的目标解析。

键与解析口径(工具驱动架构 2026-09-01):键为**真实说说 tid**(通知项登记
origin_tid——消息尾部锚展示的是真实 tid,合成 tid 模型不可见);resolve 支持
**精确→前缀**两级匹配——注入消息尾部的 ID 锚只取 tid 前 12 位(防超长),
模型照抄锚值调用工具,qzone_like 内部缺省路径则传全量 tid,两种形态都要能命中。
asyncio 单线程事件循环内使用,无锁(与 FeedInjector 同款纪律)。
"""

from __future__ import annotations
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class FeedContext:
    tid: str
    owner_uin: str
    owner_nickname: str = ""  # 通知回退路径可能无昵称(owner_uin 兜底展示),默认空
    commenter_uin: str = ""   # 通知场景:评论者/回复者
    commenter_nickname: str = ""
    comment_tid: str = ""     # 通知场景:主评论 tid(楼中楼回复用)
    comment_uin: str = ""     # 通知场景:主评论作者 uin(楼中楼二元组)
    kind: str = "feed"        # "feed"=浏览动态 / "notify_comment"=说说被评论 / "notify_reply"=评论被回复 / "self"=自己发布
    # M3-r2 表达生成层场景素材:说说正文摘要(前 100 字)与近期评论(「昵称:内容」),
    # qzone_comment/qzone_reply 生成正文时作防复读上下文;旧登记点不传保持默认空。
    content_summary: str = ""
    recent_comments: list[str] = field(default_factory=list)


class FeedContextRegistry:
    def __init__(self, *, max_entries: int = 128, ttl_seconds: int = 48 * 3600) -> None:
        self._entries: OrderedDict[str, tuple[FeedContext, float]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_seconds

    def register(self, ctx: FeedContext) -> None:
        self._entries[ctx.tid] = (ctx, time.monotonic())
        self._entries.move_to_end(ctx.tid)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def resolve(self, tid: str) -> FeedContext | None:
        """按 tid 解析上下文:精确命中优先;未命中再按「键以查询串为前缀」回退
        (消息尾部锚为 tid 前 12 位,键为全量 tid)。多键同前缀时取最近使用项;
        命中即 LRU 触底+TTL 校验,过期删除返回 None。"""

        key = str(tid or "").strip()
        if not key:
            return None
        entry = self._entries.get(key)
        if entry is None:
            # 前缀回退:从最近使用端扫描,同前缀多键取最新
            for k in reversed(self._entries):
                if k.startswith(key):
                    entry = self._entries[k]
                    break
        if entry is None:
            return None
        ctx, ts = entry
        if time.monotonic() - ts > self._ttl:
            self._entries.pop(ctx.tid, None)
            return None
        self._entries.move_to_end(ctx.tid)
        return ctx

    def clear(self) -> None:
        self._entries.clear()
