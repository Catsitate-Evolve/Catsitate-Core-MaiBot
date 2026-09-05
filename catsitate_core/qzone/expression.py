"""表达润色层:planner 直写 content 草稿,旁路 LLM 按人设与表达方式润色。

分工与主程序同构:planner 决定「说什么」(草稿,带全量上下文与意图),
本层只管「怎么说」——按 bot 人设(personality.personality)与表达方式
(personality.reply_style)把草稿顺成 bot 平时的样子。润色失败不阻断
动作:告警后以草稿直发(草稿本身即 planner 的完整表达,显式回退不静默)。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from catsitate_core.llm_provider import build_side_prompt, rpc_error_brief


def _sanitize(text: str) -> str:
    """输出卫生:剥首尾引号与空白(模型偶尔给正文套引号)。

    不做规则剥离 emoji:字符级过滤会误伤表达,
    emoji 与否交给润色模板的措辞约束。"""
    return text.strip().strip('"“”').strip()


async def polish_action_text(
    llm_call: Callable[[list[dict]], Awaitable[dict[str, Any]]],
    *, persona: str, voice: str, draft: str, scene: str = "",
    limit: int = 200, logger: Any = None,
) -> str:
    """按人设与表达方式润色草稿。返回润色后文本;失败/为空返回空串
    (调用方告警后以草稿直发,不静默、不阻断)。"""

    draft = (draft or "").strip()
    if not draft:
        return ""
    stable_ctx = [
        f"bot 人设:{(persona or '').strip() or '(未配置,按轻松自然的口吻)'}",
        f"你平时说话的方式:\n{(voice or '').strip() or '简短自然,像平时说话'}",
    ]
    if (scene or "").strip():
        stable_ctx.append(f"你正在{scene.strip()}。")
    variable_tail = [f"【待发内容】\n{draft}"]
    messages, _ = build_side_prompt("qzone_expression", stable_ctx, variable_tail)
    # 调用异常兜底(2026-09-02):RPC 超时(E_TIMEOUT)等异常原先直接上抛会炸掉
    # 整个动作工具——契约是「润色失败不阻断动作,以草稿直发」;E_TIMEOUT 以
    # 明显的超时警告浮出,异常简报走 rpc_error_brief
    try:
        result = await llm_call(messages)
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("QQ空间表达润色调用异常(%s),以草稿直发", rpc_error_brief(exc))
        return ""
    if not isinstance(result, dict) or not result.get("success"):
        if logger:
            logger.warning("QQ空间表达润色失败,以草稿直发")
        return ""
    text = _sanitize(str(result.get("response") or ""))
    if not text:
        if logger:
            logger.warning("QQ空间表达润色返回空文本,以草稿直发")
        return ""
    if len(text) > limit:
        # 超长只做一次软性改短重润,不设硬截断——
        # 重润仍超长就按模型原样发出,长度交给措辞约束与模型自律)
        if logger:
            logger.warning("QQ空间表达润色超长(%d 字>上限 %d),这次改短一些重新润色", len(text), limit)
        retry_messages, _ = build_side_prompt(
            "qzone_expression", stable_ctx,
            variable_tail + [f"这次改短一些,不超过 {limit} 字。"],
        )
        try:
            result2 = await llm_call(retry_messages)
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("QQ空间表达润色重润调用异常(%s),沿用首次润色结果", rpc_error_brief(exc))
            return text
        if isinstance(result2, dict) and result2.get("success"):
            retry_text = _sanitize(str(result2.get("response") or ""))
            if retry_text:
                text = retry_text
    return text
