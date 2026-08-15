"""贴表情引擎(规格 §4.5):白名单 LLM 选表情 + 每流冷却护栏(JSON 快照),无概率旁路。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable

from .config import MsgReactSection
from .llm_provider import build_side_prompt
from .qq_emoji import compact_emoji_table, load_emoji_table
from .storage import JsonSnapshot

_ISO = "%Y-%m-%dT%H:%M:%S"


def parse_choice_resp(response: str) -> tuple[str | None, str]:
    """从 LLM 文本提取所选表情 id;必须命中内置 QQ 表情表,否则返回 (None, 原因)。"""

    cleaned = response.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace:
            cleaned = brace.group(0)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None, "LLM 未返回合法 JSON"
    if not isinstance(data, dict):
        return None, "LLM 返回非对象 JSON"
    emoji = data.get("emoji_id")
    if not isinstance(emoji, str):
        return None, "LLM 未给出 emoji_id 字段"
    if emoji not in load_emoji_table():
        return None, f"emoji_id {emoji!r} 不在表情表内"
    return emoji, ""


class MsgReactEngine:
    """贴表情引擎:选表情 prompt 组装与每流冷却(JSON 快照限频,规格 §4.5)。"""

    def __init__(self, snapshot: JsonSnapshot, config: MsgReactSection) -> None:
        self.snapshot = snapshot
        self.config = config

    def check_cooldown(
        self, stream_id: str, now: Callable[[], datetime] | None = None
    ) -> tuple[bool, str]:
        """返回 (可用?, 原因);距上次贴表情 < 冷却秒数时不可用。"""

        now_fn = now or datetime.now
        data = self.snapshot.load()
        last_str = data.get(stream_id)
        if not last_str:
            return True, ""
        last = datetime.strptime(last_str, _ISO)
        elapsed = (now_fn() - last).total_seconds()
        if elapsed < self.config.per_stream_cooldown_seconds:
            remaining = int(self.config.per_stream_cooldown_seconds - elapsed)
            return False, f"本流冷却中,剩余 {remaining} 秒"
        return True, ""

    def mark_used(self, stream_id: str, now: Callable[[], datetime] | None = None) -> None:
        now_fn = now or datetime.now
        data = self.snapshot.load()
        data[stream_id] = now_fn().strftime(_ISO)
        self.snapshot.save(data)

    def build_choose_prompt(
        self, target_text: str, intent: str
    ) -> tuple[list[dict], str]:
        """组装选表情 prompt:内置 QQ 表情表属稳定段,目标消息+意图为变量尾(§4.10)。"""

        return build_side_prompt(
            "msg_react",
            [f"可选表情(id 描述):{compact_emoji_table()}"],
            [f"目标消息:{target_text}", f"贴表情意图:{intent}"],
        )
