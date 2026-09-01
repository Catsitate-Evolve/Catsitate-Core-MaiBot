"""出站意图路由(spec §3.3):注入侧前置设定意图,驱动回调按意图选动作。

**已废弃(工具驱动架构 2026-09-01,v0.7)**:plugin.py 已删除意图系统全套
(出站意图/网关回调路由/意图绑定校验),互动改经 qzone_comment/qzone_reply/
qzone_like 工具发出。本模块无生产调用点,保留仅为 revert 便利与历史测试,
后续清理批次移除。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OutboundIntent:
    kind: str  # "reaction"(浏览窗口对当前动态的评论) | "comment_reply"(窗口外评论轮询的楼中楼)
    tid: str
    target_qq: str
    comment_tid: str = ""
    comment_uin: str = ""
    comment_nick: str = ""
    # 意图绑定的注入消息 id(qzone_{tid}_{seq},深度审查 A-1):出站 reply 段引用的
    # 目标消息与之不一致=超时推进后旧轮回复错发新目标,网关侧据此拒发
    message_id: str = ""
    # 已出站次数(多次出站设计变更 2026-09-01):网关成功路径累计,同一意图允许多段
    # 回复逐条发出(planner 说了三句话→三条评论);上限 5 由网关判定,防无限循环
    outbound_count: int = 0


def route_outbound(intent: OutboundIntent | None, text: str, has_binary: bool) -> tuple[str, str]:
    """→ (action, reason)。action: comment/reply/reject;reason 供日志与测试。"""
    if has_binary:
        return "reject", "含二进制段"
    if not text.strip():
        return "reject", "空文本"
    if intent is None:
        return "reject", "无出站意图"
    if intent.kind == "reaction":
        return "comment", intent.tid
    if intent.kind == "comment_reply":
        return "reply", intent.comment_tid
    return "reject", f"未知意图 {intent.kind}"
