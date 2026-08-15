"""睡眠状态机与入睡判定(二期 3.2):全局唯一状态 + clamp 醒来 + 晚安判定器 + 睡醒回顾。"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Callable

from .config import SleepSection
from .storage import JsonSnapshot

_ISO = "%Y-%m-%dT%H:%M:%S"


@dataclass
class SleepState:
    state: str = "awake"  # awake | sleep
    sleep_at: str = ""
    wake_at: str = ""


class SleepManager:
    """睡眠状态机:全局唯一;持久化含状态与窗口时刻(重启恢复不依赖日程文件)。"""

    def __init__(self, snapshot: JsonSnapshot, config: SleepSection) -> None:
        self.snapshot = snapshot
        self.config = config
        data = snapshot.load()
        self.state = SleepState(**{k: data.get(k) for k in ("state", "sleep_at", "wake_at") if k in data})

    def persist(self) -> None:
        self.snapshot.save(asdict(self.state))

    def is_sleeping(self, now: Callable[[], datetime] | None = None) -> bool:
        now_fn = now or datetime.now
        return self.state.state == "sleep" and now_fn().strftime(_ISO) < (self.state.wake_at or "9999")

    def enter_sleep(self, *, now: Callable[[], datetime] | None = None, wake_at: str) -> None:
        now_fn = now or datetime.now
        self.state.state = "sleep"
        self.state.sleep_at = now_fn().strftime(_ISO)
        self.state.wake_at = wake_at
        self.persist()

    def wake(self, now: Callable[[], datetime] | None = None) -> None:
        del now
        self.state.state = "awake"
        self.state.sleep_at = ""
        self.state.wake_at = ""
        self.persist()

    def clamp_wake_time(self, sleep_at: str, planned_wake: str) -> str:
        """醒来时刻 = clamp(计划醒来, 入睡+min, 入睡+max);正常等于计划醒来。"""

        sleep_dt = datetime.strptime(sleep_at, _ISO)
        planned = datetime.strptime(planned_wake, _ISO)
        min_wake = sleep_dt + timedelta(minutes=self.config.min_sleep_minutes)
        max_wake = sleep_dt + timedelta(minutes=self.config.max_sleep_minutes)
        return min(max(planned, min_wake), max_wake).strftime(_ISO)
