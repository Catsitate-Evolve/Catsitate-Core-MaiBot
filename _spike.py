"""临时 spike 验证脚本 — 验证后删除。"""

import json
import logging
from datetime import datetime

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
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
        msg = kwargs.get("message")
        if isinstance(msg, dict) and "raw_message" in msg:
            raw = msg.get("raw_message")
            if isinstance(raw, list) and not any(isinstance(s, dict) and s.get("text") == "[spike改写]" for s in raw):
                _RECEIVE_INFO.append(f"receive_raw(前60)={str(raw)[:60]}")
                modified = dict(kwargs)
                modified["message"] = {**msg, "raw_message": [{"type": "text", "text": "[spike改写]"}] + raw}
                return {"action": "continue", "modified_kwargs": modified}
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        "spike_probe",
        description="spike 探针工具:触发后调用 message.get_recent 二进制",
        parameters=[],
    )
    async def handle_spike_probe(self, stream_id: str = "", **kwargs):
        try:
            result = await self.ctx.call_capability(
                "message.get_recent", chat_id=stream_id, limit=5, include_binary_data=True
            )
            logger.info("[spike] get_recent(include_binary_data) 结果: %s", json.dumps(result, ensure_ascii=False, default=str)[:800])
            append_result = await self.ctx.maisaka.context.append(stream_id, [{"type": "text", "text": "[spike] 注入上下文探针"}])
            logger.info("[spike] maisaka.context.append 结果: %s", append_result)
            return {"name": "spike_probe", "content": "spike 探针执行完毕,请查看日志"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("[spike] 探针失败: %s", exc)
            return {"name": "spike_probe", "content": f"spike 探针失败: {exc}"}


def create_plugin() -> SpikePlugin:
    return SpikePlugin()
