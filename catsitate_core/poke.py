"""戳一戳引擎:主动戳前置校验(每用户冷却,JSON 快照限频)。

入站通知解析已删除(改写在实机中效果不及理想)。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .config import PokeSection
from .storage import JsonSnapshot

_ISO = "%Y-%m-%dT%H:%M:%S"


class PokeEngine:
    """戳一戳:只做主动戳前置校验,被戳反应逻辑不实现(规格剔除)。"""

    def __init__(self, snapshot: JsonSnapshot, config: PokeSection) -> None:
        self.snapshot = snapshot
        self.config = config

    def can_poke(
        self,
        user_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> tuple[bool, str]:
        """主动戳前置校验:仅每用户冷却(用户已取消好感度等级门槛)。"""

        now_fn = now or datetime.now
        data = self.snapshot.load()
        last_str = data.get(user_id)
        if last_str:
            last = datetime.strptime(last_str, _ISO)
            elapsed = (now_fn() - last).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                remaining = int(self.config.cooldown_seconds - elapsed)
                return False, f"主动戳冷却中,剩余 {remaining} 秒"
        return True, ""

    def mark_poked(self, user_id: str, now: Callable[[], datetime] | None = None) -> None:
        now_fn = now or datetime.now
        data = self.snapshot.load()
        data[user_id] = now_fn().strftime(_ISO)
        self.snapshot.save(data)
