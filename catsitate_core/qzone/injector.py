"""串行注入决策核心(纯状态机,IO 由 plugin 接线;spec §2.4)。

一次只允许一条动态处于 awaiting(已注入待轮完成)。推进条件 = 轮完成信号
(planner.after_response 无 tool_calls,由 plugin 转发 on_turn_complete)。
超时兜底:常规 decision_window_s;wait 态(wait 是 tool_call,其响应不满足完成信号)
延长到 hard_cap_multiplier×decision_window_s(自注入时刻起算,wait 不重置起点),
防止 wait 期间注入下一条并入批处理导致出站意图错靶(spec §2.4 回顾修订)。
窗口结束:队列状态丢弃(SeenStore.revert_pending 由 plugin 调用回退未读)。
"""

from __future__ import annotations

from dataclasses import dataclass

from catsitate_core.qzone.protocol import FeedItem


@dataclass
class _Awaiting:
    feed: FeedItem
    since: float
    wait_extension: bool = False


class FeedInjector:
    def __init__(self, *, decision_window_s: int, hard_cap_multiplier: int = 3) -> None:
        self.decision_window_s = max(int(decision_window_s), 1)
        self.hard_cap = max(int(hard_cap_multiplier), 1) * self.decision_window_s
        self._queue: list[FeedItem] = []
        self._awaiting: _Awaiting | None = None
        self._window_active = False
        self._injected_count = 0
        self._popped: FeedItem | None = None  # next_to_inject 刚弹出的引用,供 mark_injected 关联

    # ---- 窗口 ----
    def window_started(self) -> None:
        self._window_active = True

    def window_ended(self) -> None:
        self._window_active = False
        self._queue.clear()
        self._awaiting = None
        self._popped = None

    @property
    def window_active(self) -> bool:
        return self._window_active

    # ---- 队列 ----
    def enqueue(self, feeds: list[FeedItem]) -> int:
        added = 0
        for f in feeds:
            if f.tid:
                self._queue.append(f)
                added += 1
        return added

    def queue_size(self) -> int:
        return len(self._queue)

    # ---- 串行推进 ----
    def next_to_inject(self, now: float) -> FeedItem | None:
        if not self._window_active or self._awaiting is not None or not self._queue:
            return None
        self._popped = self._queue.pop(0)
        return self._popped

    def mark_injected(self, tid: str, now: float) -> None:
        """标记 tid 已注入:保留完整 FeedItem 供 describe_current 呈现 tid 与内容摘要。

        引用解析顺序:next_to_inject 刚弹出的暂存(tid 一致时)→ 队列按 tid 移除
        (支持不经弹出的直接标记)→ 轻量占位(仅 tid,调用方未走弹出流程时)。
        """
        if self._awaiting is not None:
            return
        feed: FeedItem | None = None
        if self._popped is not None and self._popped.tid == tid:
            feed = self._popped
            self._popped = None
        else:
            for i, f in enumerate(self._queue):
                if f.tid == tid:
                    feed = self._queue.pop(i)
                    break
        if feed is None:
            feed = FeedItem(tid=tid, abstime="", uin="", nickname="", content="")
        self._awaiting = _Awaiting(feed=feed, since=now)
        self._injected_count += 1

    def on_turn_complete(self, now: float) -> None:
        del now
        self._awaiting = None

    def on_wait_state(self, now: float) -> None:
        # wait 只切换上限档位(常规→hard_cap),不重算起点:硬上限锚定注入时刻
        # (spec §2.4,与用例 test_wait_state_extends_hard_cap_3x 的 +200/+230 语义一致)。
        del now
        if self._awaiting is not None:
            self._awaiting.wait_extension = True

    @property
    def awaiting_tid(self) -> str:
        return self._awaiting.feed.tid if self._awaiting else ""

    def awaiting_timed_out(self, now: float) -> bool:
        if self._awaiting is None:
            return False
        limit = self.hard_cap if self._awaiting.wait_extension else self.decision_window_s
        return now - self._awaiting.since > limit

    def force_release(self, now: float) -> None:
        del now
        self._awaiting = None

    # ---- 状态呈现 ----
    def describe_current(self) -> str:
        if self._awaiting is not None:
            wait = "(等待中-wait)" if self._awaiting.wait_extension else ""
            content = self._awaiting.feed.content or "(无正文)"
            return f"当前看到:tid={self._awaiting.feed.tid}{wait} 内容={content[:50]}"
        if self._queue:
            return f"队列中还有 {len(self._queue)} 条待看"
        return "暂无新动态"

    def stats(self) -> dict:
        return {"injected": self._injected_count, "queued": len(self._queue),
                "awaiting": bool(self._awaiting), "window_active": self._window_active}
