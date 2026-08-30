"""注入框架:主链路注入的唯一出口(缓存纪律在此保证)。

顺序固定:等级规则块 → 环境块 → 备忘块 → 好感度块(按稳定性降序,规格 §4.1)。
空块跳过;同一 (module, content_key, text) 内容未变时字节级复用上一轮渲染结果。
"""

from __future__ import annotations

from dataclasses import dataclass

BLOCK_ORDER: tuple[str, ...] = ("level_rule", "environment", "schedule", "qzone", "memo", "favorability")  # 三期:qzone 块插日程块之后(spec §3.4)


@dataclass(frozen=True)
class InjectionBlock:
    """一个注入块(一条 user 消息)。"""

    module: str
    content_key: str  # 语义键(如说话人 user_id、备忘集合 hash)
    text: str  # 完整块文本,含标签前缀(如 "[环境] ...")


class InjectAssembler:
    """注入块组装器:排序 + 版本化缓存复用。"""

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}

    def render(self, blocks: list[InjectionBlock]) -> list[dict]:
        """按固定顺序渲染为消息列表(role=user)。"""

        by_module: dict[str, InjectionBlock] = {}
        for block in blocks:
            if block.module not in BLOCK_ORDER:
                continue
            if block.module in by_module:
                # 规格 §4.1:每模块每轮仅一块,重复属调用方错误,显式暴露不静默覆盖
                raise ValueError(f"注入块模块重复: {block.module}(每模块每轮仅允许一块)")
            by_module[block.module] = block
        messages: list[dict] = []
        for module in BLOCK_ORDER:
            block = by_module.get(module)
            if block is None:
                continue
            cache_key = f"{module}|{block.content_key}|{block.text}"
            message = self._cache.get(cache_key)
            if message is None:
                message = {"role": "user", "content": block.text}
                self._cache[cache_key] = message
            messages.append(message)
        return messages

    def reset(self) -> None:
        """清空缓存(配置热重载时调用)。"""

        self._cache = {}
