"""后台 asyncio 任务引擎:固定 tick,各模块注册周期性任务(规格 §3.2)。"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("catsitate.scheduler")


class Scheduler:
    """60s tick 调度器:每 tick 检查各任务是否到达间隔,到期则执行。

    任务异常记录日志并隔离,不中断其它任务与主循环(错误完整暴露)。
    间隔按 tick 换算:ticks_needed = ceil(interval_seconds / tick_seconds)(最小 1)。
    """

    def __init__(self, tick_seconds: int = 60) -> None:
        self.tick_seconds = tick_seconds
        self._tasks: dict[str, tuple[int, int, Callable[[], Awaitable[None]]]] = {}
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._tick = 0  # 已推进的 tick 数(测试可手动驱动)

    def register(
        self,
        name: str,
        interval_seconds: int,
        coro_factory: Callable[[], Awaitable[None]],
    ) -> None:
        """注册周期任务;interval_seconds 为执行间隔(秒),首次到期自注册时起算。"""

        if name in self._tasks:
            raise ValueError(f"调度任务重名: {name}")
        self._tasks[name] = (interval_seconds, 0, coro_factory)

    def unregister(self, name: str) -> None:
        self._tasks.pop(name, None)

    async def _run_due_tasks(self) -> None:
        """执行所有到期任务(异常隔离)。"""

        for name, (interval, last_run_tick, factory) in list(self._tasks.items()):
            ticks_needed = max(1, -(-interval // self.tick_seconds))  # ceil(interval / tick)
            if self._tick - last_run_tick < ticks_needed:
                continue
            self._tasks[name] = (interval, self._tick, factory)
            try:
                await factory()
            except Exception:
                logger.exception("调度任务 %s 执行失败", name)

    async def run(self) -> None:
        """主循环:按 tick_seconds 推进,直到 stop()。"""

        self._running = True
        while self._running:
            await asyncio.sleep(self.tick_seconds)
            self._tick += 1
            await self._run_due_tasks()

    async def stop(self) -> None:
        """停止主循环并等待协程退出。"""

        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    def start(self) -> asyncio.Task:
        """启动主循环(后台任务),返回 task。"""

        if self._loop_task is not None and not self._loop_task.done():
            raise RuntimeError("调度器已在运行")
        self._loop_task = asyncio.create_task(self.run())
        return self._loop_task
