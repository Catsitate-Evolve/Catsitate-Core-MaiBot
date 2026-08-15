"""戳一戳引擎(规格 §4.6):入站通知解析增强 + 主动戳前置校验(好感度门槛/冷却)。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .config import PokeSection
from .storage import JsonSnapshot

_ISO = "%Y-%m-%dT%H:%M:%S"


class PokeEngine:
    """戳一戳:只做解析增强与主动戳前置校验,被戳反应逻辑不实现(规格剔除)。"""

    def __init__(self, snapshot: JsonSnapshot, config: PokeSection) -> None:
        self.snapshot = snapshot
        self.config = config

    def parse_notice(self, payload: dict) -> dict | None:
        """解析 napcat_notice_payload;结构不符返回 None(调用方记录日志,不静默)。"""

        raw = payload.get("raw_info")
        if not isinstance(raw, list) or not raw:
            return None
        first = raw[0]
        if not isinstance(first, dict):
            return None
        user_id = first.get("user_id")
        text = self.enhance_notice_text(payload)
        if user_id is None or text is None:
            return None
        return {"text": text, "user_id": str(user_id)}

    def enhance_notice_text(self, payload: dict, fallback_nickname: str = "") -> str | None:
        """把 raw_info 渲染为拟人文本:「小猫 拍了拍你,说:"该睡了"」。

        fallback_nickname: 从消息上下文(message_info.user_info.user_nickname)解析的昵称,
        raw_info.nm 实测为空串时兜底用。
        """

        raw = payload.get("raw_info")
        if not isinstance(raw, list) or not raw:
            return None
        first = raw[0]
        if not isinstance(first, dict):
            return None
        # 实测 raw_info 字段为 nm(昵称,常为空串)/uid/col/type;顶层 user_id 为发起者
        nickname = str(
            first.get("nm") or first.get("nickname") or fallback_nickname
            or first.get("user_id") or payload.get("user_id") or "有人"
        )
        user_id = str(first.get("user_id") or first.get("uid") or payload.get("user_id") or "")
        who = f"{nickname}({user_id})" if user_id else nickname
        target_id = str(payload.get("target_id") or "")
        self_id = str(payload.get("self_id") or "")
        # 目标昵称 payload 无来源:目标为 bot 自身时用「你」,其余用 qq 号
        target = "你" if target_id and target_id == self_id else (f"({target_id})" if target_id else "")
        remark = first.get("remark") or first.get("msg") or ""
        if remark:
            return f'{who} 戳了 {target},说:"{remark}"'
        return f"{who} 戳了 {target}"

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
