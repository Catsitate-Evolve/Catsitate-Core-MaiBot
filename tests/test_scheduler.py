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


@pytest.mark.asyncio
async def test_jitter_ratio_keeps_interval_in_bounds():
    """抖动注册:每周期生效间隔独立抽取(随周期落库,不逐 tick 重抽),落在
    base×(1±ratio) 内且间隔出现差异;比例 0 保持精确间隔(旧行为,由既有
    用例覆盖)。"""

    import random

    scheduler = Scheduler(tick_seconds=1)
    fired_ticks: list[int] = []

    async def job():
        fired_ticks.append(scheduler._tick)

    random.seed(11)  # 固定种子保证可复现
    scheduler.register("jittered", 10, job, jitter_ratio=0.5)
    for _ in range(120):
        scheduler._tick += 1
        await scheduler._run_due_tasks()
    diffs = [b - a for a, b in zip(fired_ticks, fired_ticks[1:])]
    assert all(5 <= d <= 15 for d in diffs)  # base×(1±0.5),tick=1s 下取整仍在界内
    assert len(set(diffs)) > 1  # 间隔确实在抖(恒值即未生效)
