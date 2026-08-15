"""临时 spike 验证脚本 — 验证后删除。"""

import json
import logging
from datetime import datetime

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

logger = logging.getLogger("catsitate.spike")

# 插件 logger 不桥接主进程日志,观测信息经探针文本 → LLM prompt → 主程序日志链路可视化
_RECEIVE_INFO: list[str] = []


class SpikePlugin(MaiBotPlugin):
    """Spike 验证插件。"""

    async def on_load(self) -> None:
        logger.info("[spike] SpikePlugin 已加载")

    async def on_unload(self) -> None:
        """卸载。"""

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热更新。"""

    @HookHandler(
        "maisaka.planner.before_request",
        name="spike_before_request",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def spike_before_request(self, **kwargs):
        logger.info("[spike] before_request kwargs keys: %s", list(kwargs.keys()))
        items = kwargs.get("items")
        if items is not None:
            logger.info("[spike] items 类型=%s 长度=%s 首项=%s", type(items).__name__, len(items), json.dumps(items[0], ensure_ascii=False, default=str)[:300])
            # 快照格式的合法 UserMessageItem(item_type/meta/parts,无 role 字段)
            probe_text = "[spike] 注入探针消息" + (f" | {'; '.join(_RECEIVE_INFO[-2:])}" if _RECEIVE_INFO else "")
            # 把进入 LLM 的最近用户消息文本带出,验证 receive 改写是否到达主链路
            user_texts: list[str] = []
            for it in items:
                if isinstance(it, dict) and it.get("item_type") == "UserMessageItem":
                    for part in it.get("parts") or []:
                        if isinstance(part, dict) and part.get("type") == "text":
                            user_texts.append(str(part.get("text"))[:50])
            if user_texts:
                real = [t for t in user_texts if not t.startswith("<system-reminder") and not t.startswith("时间:") and "spike" not in t]
                probe_text += f" | last_user_texts={real[-2:]}"
            probe = {
                "item_type": "UserMessageItem",
                "meta": {"item_id": "spike-probe-1", "logical_turn_id": None, "timestamp": datetime.now().isoformat()},
                "parts": [{"type": "text", "text": probe_text}],
            }
            modified = dict(kwargs)
            if isinstance(items, list):
                # 找 system item 索引,插其后
                idx = next((i for i, it in enumerate(items) if isinstance(it, dict) and it.get("item_type") == "SystemMessageItem"), -1)
                modified["items"] = items[: idx + 1] + [probe] + items[idx + 1 :]
                return {"action": "continue", "modified_kwargs": modified}
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "chat.receive.before_process",
        name="spike_receive_before",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
    )
    async def spike_receive_before(self, **kwargs):
        logger.info("[spike] receive.before_process kwargs keys: %s", list(kwargs.keys()))
        info = f"receive_keys={list(kwargs.keys())}"
        for key, val in kwargs.items():
            if isinstance(val, dict) and ("user_id" in val or "segments" in val or "message_id" in val):
                info += f"|receive.{key}={type(val).__name__}(keys={list(val.keys())[:12]})"
                break
        _RECEIVE_INFO.append(info)
        # 验证改写能力:raw_message 是段列表,头部插入 text 段加前缀,看下游是否可见
        # (命令消息跳过改写,避免干扰命令链识别)
        msg = kwargs.get("message")
        if isinstance(msg, dict) and "raw_message" in msg and not msg.get("is_command"):
            raw = msg.get("raw_message")
            if isinstance(raw, list) and not any(isinstance(s, dict) and s.get("text") == "[spike改写]" for s in raw):
                _RECEIVE_INFO.append(f"receive_raw(前60)={str(raw)[:60]}")
                modified = dict(kwargs)
                modified["message"] = {**msg, "raw_message": [{"type": "text", "data": "[spike改写]"}] + raw}
                return {"action": "continue", "modified_kwargs": modified}
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        "spike_probe",
        description="spike 探针工具:触发后调用 message.get_recent 二进制",
        parameters=[],
    )
    async def handle_spike_probe(self, stream_id: str = "", **kwargs):
        summary = await self._run_probe(stream_id)
        return {"name": "spike_probe", "content": summary}

    @Command("/spike_probe", description="spike 探针命令(绕过 LLM):验证 get_recent 二进制与 context.append", pattern=r"^/spike_probe\s*$", aliases=[])
    async def spike_probe_cmd(self, **kwargs):
        stream_id = str(kwargs.get("stream_id") or "")
        summary = await self._run_probe(stream_id)
        return f"spike_probe 执行结果:\n{summary}"

    async def _run_probe(self, stream_id: str) -> str:
        try:
            result = await self.ctx.call_capability(
                "message.get_recent", chat_id=stream_id, limit=5, include_binary_data=True
            )
            logger.info("[spike] get_recent(include_binary_data) 结果: %s", json.dumps(result, ensure_ascii=False, default=str)[:800])
            # 概括返回结构:消息数、每条消息的段类型、image 段是否有 data
            messages = result.get("messages") if isinstance(result, dict) else result
            lines = [f"get_recent 返回类型={type(result).__name__}"]
            if isinstance(messages, list):
                lines.append(f"消息数={len(messages)}")
                for msg in messages[-3:]:
                    if not isinstance(msg, dict):
                        lines.append(f"  非 dict 消息: {type(msg).__name__}")
                        continue
                    seg_types = [(s.get("type"), bool(s.get("data")), bool(s.get("hash"))) for s in (msg.get("segments") or []) if isinstance(s, dict)]
                    lines.append(f"  msg {str(msg.get('message_id'))[:8]} 段={seg_types}")
            else:
                lines.append(f"messages 非列表: {str(result)[:200]}")
            append_result = await self.ctx.maisaka.context.append(stream_id, [{"type": "text", "text": "[spike] 注入上下文探针"}])
            lines.append(f"append 返回={str(append_result)[:200]}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[spike] 探针失败: %s", exc)
            return f"spike 探针失败: {exc}"


def create_plugin() -> SpikePlugin:
    return SpikePlugin()
