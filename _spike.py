"""临时 spike 验证脚本 — 验证后删除。"""

import json
import logging

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

logger = logging.getLogger("catsitate.spike")


class SpikePlugin(MaiBotPlugin):
    """Spike 验证插件。"""

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
            probe = {"role": "user", "content": "[spike] 注入探针消息"}
            modified = dict(kwargs)
            if isinstance(items, list):
                # 找 system 索引,插其后
                idx = next((i for i, it in enumerate(items) if isinstance(it, dict) and it.get("role") == "system"), -1)
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
