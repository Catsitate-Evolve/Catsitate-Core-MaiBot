"""reply 上下文补传与哨兵判定(规格 §4.7):纯逻辑,SDK 适配在 Task 14。"""

from __future__ import annotations

import json
import re

from .llm_provider import build_side_prompt

# 上下文工具列表(联调裁定:内置常量,不再作为可配置项)
CONTEXT_TOOLS: tuple[str, ...] = (
    "query_memory", "query_person_profile", "fetch_history", "view_forward_message", "memo_read",
)


def should_backfill(
    called_tools: list[str],
    reply_reference: str,
    reasoning: str,
) -> bool:
    """三条件全真才补传:本轮调用过上下文工具 且 reply_reference 为空 且 reasoning 为空。"""

    return (
        bool(set(called_tools) & set(CONTEXT_TOOLS))
        and not reply_reference.strip()
        and not reasoning.strip()
    )


def merge_tool_results(tool_results: dict[str, str], max_chars: int = 400) -> str:
    """合并工具结果为文本摘要:按工具名排序,超长在条目边界截断。"""

    parts: list[str] = []
    total = 0
    for name in sorted(tool_results):
        value = str(tool_results[name]).strip()
        if not value:
            continue
        line = f"[{name}] {value}"
        if total + len(line) > max_chars:
            parts.append("…(超出截断)")
            break
        parts.append(line)
        total += len(line)
    return "\n".join(parts)


def backfill_reply_items(
    output_items: list[dict],
    tool_results: dict[str, str],
    called_tools: list[str],
    reasoning: str,
    max_chars: int = 400,
) -> list[dict]:
    """为满足触发条件的 reply 调用补 reply_reference,不改动其它工具调用。

    匹配宿主真实快照形态(H-1 修复,2026-09-03):reply 调用是 FunctionCallItem,
    工具名在 item["tool_call"]["func_name"],参数在 item["tool_call"]["args"]——
    一期误判顶层 tool_name/arguments 键(宿主快照无此二键),匹配恒不中,
    补传自上线以来从未生效。命中项浅拷贝 item 与 tool_call 后写入
    reply_reference,不原地改宿主列表中的条目。"""

    if not should_backfill(called_tools, "", reasoning):
        return output_items
    merged = merge_tool_results(tool_results, max_chars=max_chars)
    if not merged:
        return output_items
    out: list[dict] = []
    for item in output_items:
        tool_call = item.get("tool_call") if item.get("item_type") == "FunctionCallItem" else None
        if isinstance(tool_call, dict) and tool_call.get("func_name") == "reply":
            args = tool_call.get("args")
            if isinstance(args, dict) and not str(args.get("reply_reference") or "").strip():
                item = {**item, "tool_call": {**tool_call, "args": {**args, "reply_reference": merged}}}
        out.append(item)
    return out


def build_sentinel_prompt(
    persona_background: str, reply_text: str, chat_context: str
) -> tuple[list[dict], str]:
    """哨兵层 prompt:指令(system)+人设背景为稳定段,待判定回复+上下文为变量尾(§4.10)。"""

    return build_side_prompt(
        "sentinel",
        [f"人设背景:{persona_background}"],
        [f"待判定回复:{reply_text}", f"聊天上下文:{chat_context}"],
    )


def parse_sentinel_response(response: str) -> tuple[bool | None, str]:
    """解析哨兵判定 JSON;失败返回 (None, 原因)。"""

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
        return None, "哨兵判定 JSON 解析失败"
    if not isinstance(data, dict):
        return None, "哨兵判定返回非对象 JSON"
    if not isinstance(data.get("should_send"), bool):
        return None, "哨兵判定缺少 should_send"
    return data["should_send"], str(data.get("reason") or "")
