"""好感度自然衰减引擎(二期 3.1):互动时间判定 + LLM 判定衰减 + apply_delta。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Awaitable, Callable

from .favorability import BatchEngine
from .llm_provider import build_side_prompt

logger = logging.getLogger(__name__)

LlMCall = Callable[[list[dict], str], Awaitable[dict]]
_ISO = "%Y-%m-%dT%H:%M:%S"


def parse_decay_response(text: str) -> tuple[int | None, str]:
    """解析衰减判定 JSON;delta 必须为 [-decay_max, 0] 区间整数,否则 (None, 原因)。"""

    cleaned = text.strip()
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
    delta, note = data.get("delta"), data.get("note")
    if not isinstance(delta, int) or not isinstance(note, str):
        return None, "delta/note 字段缺失或类型错误"
    if delta > 0:
        return None, "delta 必须 ≤ 0(衰减不可加分)"
    return delta, note  # delta 非 None 时第二元为拟人化新注记


def last_bot_interaction_time(
    recent_messages: list[dict], user_id: str, bot_user_id: str, stream_is_group: bool
) -> str | None:
    """流内该用户最近一次被 bot 直接回应的时间(ISO);从未直接回应返回 None。

    私聊:任意 bot 消息即回应;群聊:bot 消息 quote(或 @)了该用户才算——
    bot 回应他人不得重置本用户计时(规格 §3.1 群聊防误判)。
    """

    user_id, bot_user_id = str(user_id), str(bot_user_id)
    if not user_id or not bot_user_id:
        return None
    for m in reversed(list(recent_messages)):
        if not isinstance(m, dict):
            continue
        msg_info = m.get("message_info") or {}
        ui = msg_info.get("user_info") or {}
        if str(ui.get("user_id") or "") != bot_user_id:
            continue
        if stream_is_group:
            quote = str(m.get("reply_to") or "")
            at_hit = any(
                isinstance(seg, dict) and seg.get("type") == "at"
                and str((seg.get("data") or {}).get("target_user_id") or "") == user_id
                for seg in (m.get("raw_message") or [])
            )
            if user_id not in quote and not at_hit:
                continue
        ts = str(m.get("timestamp") or "")
        try:
            return datetime.fromtimestamp(float(ts)).strftime(_ISO)
        except (ValueError, TypeError, OSError):
            return ts or None
    return None


class DecayExecutor:
    """衰减执行:候选流扫描 → 未互动天数判定 → LLM 判定 → apply_delta(judge_id=decay-时间戳)。"""

    def __init__(self, store, config, llm_call: LlMCall) -> None:
        self.engine = BatchEngine(store, config)
        self.config = config
        self.llm_call = llm_call

    async def scan_and_apply(
        self,
        candidates: list[tuple[str, str, str, str]],  # (user_id, stream_id, 互动时间 ISO 或 "", is_group "0"/"1")
        now: Callable[[], datetime] | None = None,
        persona: str = "",
    ) -> list[dict]:
        """对未互动超 decay_after_days 的流执行衰减判定;返回 [{"user_id","stream_id","delta","note"}]。"""

        now_fn = now or datetime.now
        today = now_fn()
        results: list[dict] = []
        for user_id, stream_id, interaction_ts, is_group in candidates:
            if not interaction_ts:  # 从未直接互动:以 judged_at 为基准
                row = self.engine.get_level(user_id, stream_id)
                if row is None:
                    continue
                interaction_ts = row.get("judged_at") or ""
            if not interaction_ts:
                continue
            try:
                last = datetime.strptime(interaction_ts, _ISO)
            except ValueError:
                continue
            days = (today - last).days
            if days <= self.config.decay_after_days:
                continue
            row = self.engine.get_level(user_id, stream_id)
            if row is None or row["score"] <= 0:
                continue
            stable_ctx = ([f"bot 人设:{persona}"] if persona.strip() else []) + [
                f"上次等级:{row['level']},分数:{row['score']},注记:{row['note'] or '无'}",
                f"未互动天数:{days}",
            ]
            messages, _ = build_side_prompt(
                "decay", stable_ctx, [],
                replacements={"decay_max": str(max(1, self.config.decay_max))},
            )
            try:
                result = await self.llm_call(messages, self.config.decay_llm_model)
            except Exception as exc:  # noqa: BLE001
                # 仅记异常类型,不插值 exc 本体:LLM API 错误可能含请求体/PII(安全复审)
                logger.warning("好感度衰减判定失败(stream=%s): %s", stream_id, type(exc).__name__)
                
                continue  # 显式日志后跳过(规格 §3.1 失败不得静默)
            if not isinstance(result, dict) or not result.get("success"):
                continue
            delta, note = parse_decay_response(str(result.get("response") or ""))
            if delta is None:
                continue
            limit = max(1, self.config.decay_max)
            delta = max(-limit, min(0, delta))
            judged_at = now_fn().strftime(_ISO)
            self.engine.apply_delta(
                user_id, stream_id, delta, note, judged_at=judged_at,
                judge_id=f"decay-{judged_at}-{user_id}-{stream_id}",  # 同秒多用户判重(审查 M-5)
            )
            results.append({"user_id": user_id, "stream_id": stream_id, "delta": delta, "note": note})
        return results
