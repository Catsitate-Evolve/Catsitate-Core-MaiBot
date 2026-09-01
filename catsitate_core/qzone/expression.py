"""表达生成层:空间动作正文由带完整人设的旁路 LLM 产出。

两段式分工(与主程序 reply 工具/planner 同构):planner 决定「是否动作、
表达什么方向」(reply_reference/reply_style),本层按人设写正文——planner
提示词只含行为风格摘要,完整人设在表达层,空间动作与真实聊天由此共享
同一个人设出口。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from catsitate_core.llm_provider import build_side_prompt

MODE_LABELS = {"comment": "评论好友的说说", "reply": "回复好友的评论", "post": "发一条自己的说说"}
VALID_STYLES = ("简短表达", "正常回复", "长回复")


def _sanitize(text: str) -> str:
    """输出卫生:剥首尾引号与空白(模型偶尔给正文套引号)。"""
    return text.strip().strip('"“”').strip()


async def generate_action_text(
    llm_call: Callable[[list[dict]], Awaitable[dict[str, Any]]],
    *, mode: str, persona: str, reference: str, style: str = "",
    context_lines: list[str] | None = None, limit: int = 200, logger: Any = None,
) -> tuple[str, str]:
    """生成动作正文。返回 (正文, 错误串),二者互斥;失败不静默兜底。"""

    if mode not in MODE_LABELS:
        return "", f"未知动作类型:{mode}"
    reference = (reference or "").strip()
    if not reference:
        return "", "缺少 reply_reference(表达方向)"
    style = style if style in VALID_STYLES else "正常回复"
    context_lines = [str(x) for x in (context_lines or []) if str(x).strip()]
    stable_ctx = [
        f"bot 人设:{(persona or '').strip() or '(未配置,按轻松自然的口吻)'}",
        f"动作:{MODE_LABELS[mode]};篇幅:{style};正文上限 {limit} 字",
    ]
    variable_tail = ["表达方向(planner 的意图说明,不是正文,不要照抄):\n" + reference] + context_lines
    messages, _ = build_side_prompt("qzone_expression", stable_ctx, variable_tail)
    result = await llm_call(messages)
    if not isinstance(result, dict) or not result.get("success"):
        return "", "表达生成 LLM 调用失败"
    text = _sanitize(str(result.get("response") or ""))
    if not text:
        return "", "表达生成返回空文本"
    if len(text) > limit:
        if logger:
            logger.warning("QQ空间表达生成超长(%d 字>上限 %d),带字数硬约束重新生成一次", len(text), limit)
        retry_messages, _ = build_side_prompt(
            "qzone_expression", stable_ctx,
            variable_tail + [f"硬约束:正文不超过 {limit} 字,超出将被截断。"],
        )
        result2 = await llm_call(retry_messages)
        if isinstance(result2, dict) and result2.get("success"):
            retry_text = _sanitize(str(result2.get("response") or ""))
            if retry_text:
                text = retry_text
    if len(text) > limit:
        if logger:
            logger.warning("QQ空间表达生成重生成仍超长(%d 字),截断至 %d 字", len(text), limit)
        text = text[:limit]
    return text, ""
