"""后台调度器测试:注册/tick 执行/间隔/异常隔离/停止。"""

import asyncio

import pytest

from catsitate_core.services.scheduler import Scheduler


@pytest.mark.asyncio
async def test_task_runs_after_interval():
    scheduler = Scheduler(tick_seconds=60)
    fired: list[str] = []

    async def job():
        fired.append("a")

    scheduler.register("job_a", 120, job)
    scheduler._tick = 1
    await scheduler._run_due_tasks()  # 第 1 tick:未到间隔
    assert fired == []
    scheduler._tick = 2
    await scheduler._run_due_tasks()
    assert fired == ["a"]


@pytest.mark.asyncio
async def test_task_exception_does_not_block_others():
    scheduler = Scheduler(tick_seconds=60)
    fired: list[str] = []

    async def bad():
        raise RuntimeError("任务失败")

    async def good():
        fired.append("good")

    scheduler.register("bad", 60, bad)
    scheduler.register("good", 60, good)
    scheduler._tick = 1
    await scheduler._run_due_tasks()
    assert fired == ["good"]  # 异常被隔离并记录


@pytest.mark.asyncio
async def test_interval_semantics_independent():
    scheduler = Scheduler(tick_seconds=60)
    fired: list[str] = []

    async def fast():
        fired.append("f")

    async def slow():
        fired.append("s")

    scheduler.register("fast", 60, fast)
    scheduler.register("slow", 180, slow)
    for tick in (1, 2, 3):
        scheduler._tick = tick
        await scheduler._run_due_tasks()
    assert fired.count("f") == 3
    assert fired.count("s") == 1


@pytest.mark.asyncio
async def test_stop_cancels_loop():
    scheduler = Scheduler(tick_seconds=60)
    task = scheduler.start()
    await asyncio.sleep(0.01)
    await scheduler.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
