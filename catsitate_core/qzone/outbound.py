"""虚拟流出站处理(M1)。评论/发布的意图路由是 M2 交付;本模块只提供组件提取与显式拒发。

提取规则(spec §2.19⑦):text 段按序拼接为出站文本;reply/at 段忽略(reply 的
target_message_sender_id 供 M2 意图交叉校验);image/emoji 二进制段标记 has_binary
(驱动层拒发——无映射且有 16MB RPC 帧风险)。
"""

from __future__ import annotations

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
