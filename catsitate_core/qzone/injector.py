"""串行注入决策核心(纯状态机,IO 由 plugin 接线)。

双优先级队列(统一通知通道):P1=通知(评论/楼中楼回复)、
P2=浏览动态。next_to_inject 优先弹 P1——模拟「刷着动态→弹通知→
先看通知→回完继续刷」的注意力模型;两队列各自按发布时间降序
(阅读顺序:信息流降序,QQ 空间 App 实际形态,最新在上)。串行注入语义不变:一次只允许
一条动态处于 awaiting(已注入待轮完成),推进条件 = 轮完成信号
(planner.after_response 无 tool_calls,由 plugin 转发 on_turn_complete)。
超时兜底:常规 decision_window_s;wait 态(wait 是 tool_call,其响应不满足完成信号)
延长到 hard_cap_multiplier×decision_window_s(自注入时刻起算,wait 不重置起点),
防止 wait 期间注入下一条并入同一决策轮(wait 的回复尚未返回,新条目会
挤进同轮上下文干扰决策)。
窗口结束:浏览队列(P2)与 awaiting 状态一并丢弃(SeenStore.revert_pending 由
plugin 调用回退未读);通知队列(P1)保留——通知是推送语义,不隶属任何窗口,
等注入条件(bot 醒着/泵空闲)满足后继续(P1/P2 分治)。
"""

from __future__ import annotations

from dataclasses import dataclass

from catsitate_core.qzone.protocol import FeedItem


@dataclass
class _Awaiting:
    feed: FeedItem
    since: float
    wait_extension: bool = False


def _abstime_key(abstime: str) -> float:
    """发布时间排序键(数值;非法/缺失按 0,降序下排最尾)。"""
    try:
        return float(str(abstime or "").strip() or 0)
    except ValueError:
        return 0.0


class FeedInjector:
    def __init__(self, *, decision_window_s: int, hard_cap_multiplier: int = 3) -> None:
        self.decision_window_s = max(int(decision_window_s), 1)
        self.hard_cap = max(int(hard_cap_multiplier), 1) * self.decision_window_s
        self._queue_p1: list[FeedItem] = []  # P1:通知(评论/楼中楼回复,按发布时间降序)
        self._queue_p2: list[FeedItem] = []  # P2:浏览动态(全局按发布时间降序)
        self._awaiting: _Awaiting | None = None
        self._window_active = False
        self._injected_count = 0
        self._popped: FeedItem | None = None  # next_to_inject 刚弹出的引用,供 mark_injected 关联

    # ---- 窗口 ----
    def window_started(self) -> None:
        self._window_active = True

    def window_ended(self) -> None:
        """浏览窗口结束:清浏览队列与进行中的注入。

        通知队列(P1)保留——通知是推送语义,不隶属任何窗口,
        等注入条件(bot 醒着/泵空闲)满足后继续。
        """
        self._window_active = False
        self._queue_p2.clear()
        self._awaiting = None
        self._popped = None

    @property
    def window_active(self) -> bool:
        return self._window_active

    # ---- 队列 ----
    def enqueue(self, feeds: list[FeedItem]) -> int:
        """浏览动态入队(P2)并保持全局按发布时间降序(信息流降序,
        QQ 空间 App 实际形态,最新在上)。跨好友/跨轮次合并保序:每次入队后
        整体重排(abstime 数值降序,非法/缺失 abstime 排最尾按 0 处理)。"""
        return self._enqueue_into(self._queue_p2, feeds)

    def enqueue_priority(self, items: list[FeedItem]) -> int:
        """通知入队(P1):优先于浏览动态注入;队内同样按发布时间降序——
        多条通知积压时最新先看(与 P2 阅读顺序一致,非 FIFO)。"""
        return self._enqueue_into(self._queue_p1, items)

    @staticmethod
    def _enqueue_into(queue: list[FeedItem], feeds: list[FeedItem]) -> int:
        """共用入队:空 tid 跳过,入队后按 abstime 降序整体重排,返回实入数。"""
        added = 0
        for f in feeds:
            if f.tid:
                queue.append(f)
                added += 1
        if added:
            queue.sort(key=lambda f: _abstime_key(f.abstime), reverse=True)
        return added

    def queue_size(self) -> int:
        return len(self._queue_p1) + len(self._queue_p2)

    # ---- 串行推进 ----
    def next_to_inject(self, now: float) -> FeedItem | None:
        """弹出下一条注入项:P1(通知)非空优先,P1 空取 P2(浏览)。

        awaiting 未释放时返回 None(串行语义不变);P1 任何时刻可弹
        (推送语义,不依赖浏览窗口),P2 仅窗口内可弹;两队列皆空返回 None。
        """
        if self._awaiting is not None:
            return None
        if self._queue_p1:
            self._popped = self._queue_p1.pop(0)  # 通知:推送语义,不依赖浏览窗口
        elif self._window_active and self._queue_p2:
            self._popped = self._queue_p2.pop(0)  # 浏览动态:仅 read_qzone 窗口内注入
        else:
            return None
        return self._popped

    def requeue_popped(self) -> None:
        """把已弹出未标记的项放回原队列队首。

        动机:泵在 next_to_inject(弹出)与 mark_injected(标记)之间有图片下载/
        压缩/route 等真实挂起点——取消(热重载/任务回收)落在该间隙时,弹出项
        会从队列消失且无 awaiting/seen 记录,通知(P1)静默丢失。取消路径据此
        回队,P1/P2 各回各的队首(重注时重新弹出,语义不变);无在途弹出项为
        no-op。"""
        if self._popped is None:
            return
        feed = self._popped
        self._popped = None
        if feed.source == "notify":
            self._queue_p1.insert(0, feed)
        else:
            self._queue_p2.insert(0, feed)

    def mark_injected(self, tid: str, now: float) -> None:
        """标记 tid 已注入:保留完整 FeedItem 供 describe_current 呈现 tid 与内容摘要。

        引用解析顺序:next_to_inject 刚弹出的暂存(tid 一致时)→ 两队列按
        tid 移除(支持不经弹出的直接标记,P1/P2 都查)→ 轻量占位(仅 tid,
        调用方未走弹出流程时)。
        """
        if self._awaiting is not None:
            return
        feed: FeedItem | None = None
        if self._popped is not None and self._popped.tid == tid:
            feed = self._popped
            self._popped = None
        else:
            for queue in (self._queue_p1, self._queue_p2):
                for i, f in enumerate(queue):
                    if f.tid == tid:
                        feed = queue.pop(i)
                        break
                if feed is not None:
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
        # (与用例 test_wait_state_extends_hard_cap_3x 的 +200/+230 语义一致)。
        del now
        if self._awaiting is not None:
            self._awaiting.wait_extension = True

    @property
    def awaiting_tid(self) -> str:
        return self._awaiting.feed.tid if self._awaiting else ""

    @property
    def awaiting_feed(self) -> FeedItem | None:
        """当前 awaiting 动态的完整引用(无 awaiting 时 None)。"""
        return self._awaiting.feed if self._awaiting else None

    @property
    def awaiting_author(self) -> str:
        """当前动态作者 uin(注入块按人上下文/说话人交叉校验用)。"""
        return self._awaiting.feed.uin if self._awaiting else ""

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
        parts = []
        if self._queue_p1:
            parts.append(f"通知队列 {len(self._queue_p1)} 条")
        if self._queue_p2:
            parts.append(f"浏览队列 {len(self._queue_p2)} 条")
        return "/".join(parts) + "待看" if parts else "暂无新动态"

    def stats(self) -> dict:
        return {"injected": self._injected_count, "queued": self.queue_size(),
                "p1_queued": len(self._queue_p1), "p2_queued": len(self._queue_p2),
                "awaiting": bool(self._awaiting), "window_active": self._window_active}
