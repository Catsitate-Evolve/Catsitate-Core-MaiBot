"""LLM Provider 声明与旁路请求组装辅助。

旁路 LLM 请求缓存规范(规格 §4.10):稳定段在前、变量素材在后,模板版本化。
结构 = [任务指令+输出格式(system,模板固定)][稳定上下文(5 级规则/白名单/人设背景,配置数据)][变量素材]。
"""

from __future__ import annotations

# 模板:system = 任务指令+输出格式(固定,版本化);稳定上下文由调用方经 stable_ctx 传入
SIDE_TEMPLATES: dict[str, dict] = {
    "favorability": {
        "version": 1,
        "system": (
            "你是一个关系评估助手。根据对话素材评估「用户与 bot」的关系变化。\n"
            '严格输出 JSON,格式:{"delta": 整数(-5 到 5 之间), "note": "一句话关系注记(不超过40字)"}。'
            "delta 为正表示关系变好,为负表示变差,0 表示无明显变化。不要输出其它内容。"
        ),
    },
    "msg_react": {
        "version": 1,
        "system": (
            "你是表情包选择助手。从白名单中选择一个最贴合目标消息与意图的表情,"
            '严格输出 JSON:{"emoji_id": "白名单中的 id"}。不要输出其它内容。'
        ),
    },
    "sentinel": {
        "version": 1,
        "system": (
            "你是回复质检助手。判断「待判定回复」是否与聊天上下文明显不符或本不该回复。"
            '严格输出 JSON:{"should_send": true/false, "reason": "一句话理由"}。不要输出其它内容。'
        ),
    },
    "image_relook": {
        "version": 1,
        "system": (
            "你是图像观察助手。仔细观察图片,回答用户的具体问题。用简体中文,简洁准确。"
        ),
    },
}


def build_side_prompt(
    template_id: str, stable_ctx: list[str], variable_tail: list[str]
) -> tuple[list[dict], str]:
    """按稳定段前置纪律组装旁路 prompt(规格 §4.9 签名)。

    Args:
        template_id: 模板 id(SIDE_TEMPLATES 键)。
        stable_ctx: 稳定上下文段列表(5 级规则/白名单/人设背景;内容稳定,配置变更才变)。
        variable_tail: 变量素材段列表(按序追加为 user 消息)。

    Returns:
        (messages, cache_key): messages 为 OpenAI 兼容消息列表;cache_key 标识模板版本。
    """

    template = SIDE_TEMPLATES.get(template_id)
    if template is None:
        raise ValueError(f"未知旁路模板 id: {template_id}")
    messages: list[dict] = [{"role": "system", "content": template["system"]}]
    messages += [{"role": "user", "content": part} for part in stable_ctx]
    messages += [{"role": "user", "content": part} for part in variable_tail]
    return messages, f"{template_id}:v{template['version']}"
