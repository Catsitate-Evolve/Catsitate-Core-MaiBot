"""虚拟流出站处理(M1)。评论/发布的意图路由是 M2 交付;本模块只提供组件提取与显式拒发。

提取规则(spec §2.19⑦):text 段按序拼接为出站文本;reply/at 段忽略(reply 的
target_message_sender_id（推迟 M3,见 spec §3.3）);image/emoji 二进制段标记 has_binary
(驱动层拒发——无映射且有 16MB RPC 帧风险)。
"""

from __future__ import annotations

# M1 拒发文案,M2 起生产无调用点,留 M3 清理
M1_OUTBOUND_ERROR = "M1 感知阶段:QQ空间出站未实现(评论路由见 M2)"


def extract_outbound_text(message: dict) -> tuple[str, bool]:
    """返回 (text 段拼接, 是否含二进制段)。"""

    parts: list[str] = []
    has_binary = False
    for comp in (message or {}).get("raw_message") or []:
        if not isinstance(comp, dict):
            continue
        ctype = str(comp.get("type") or "")
        if ctype == "text":
            data = comp.get("data")
            if isinstance(data, str) and data.strip():
                parts.append(data.strip())
        elif ctype in ("image", "emoji"):
            has_binary = True
        # reply/at 忽略
    return "".join(parts), has_binary


def extract_quote_target(message: dict) -> str:
    """取出站消息 reply 段的 target_message_id(无 reply 段返回空串)。

    深度审查 A-1 意图绑定校验的输入:出站引用的目标与意图的注入消息不一致
    (超时推进后旧轮回复错靶)= 拒发;大部分 planner 出站不带 reply 段,
    空串表示跳过校验(覆盖带引用的高风险场景)。
    """

    for comp in (message or {}).get("raw_message") or []:
        if isinstance(comp, dict) and comp.get("type") == "reply":
            data = comp.get("data")
            if isinstance(data, dict):
                return str(data.get("target_message_id") or "")
    return ""
