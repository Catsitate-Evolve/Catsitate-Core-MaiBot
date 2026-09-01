"""出站意图路由测试(spec §3.3)。"""
from catsitate_core.qzone.routing import OutboundIntent, route_outbound


def test_reaction_intent_routes_to_comment():
    intent = OutboundIntent(kind="reaction", tid="tA", target_qq="8888")
    action, reason = route_outbound(intent, "好看!", has_binary=False)
    assert action == "comment" and reason == "tA"


def test_comment_reply_intent_routes_to_reply():
    intent = OutboundIntent(kind="comment_reply", tid="tA", target_qq="3545773341",
                            comment_tid="c1", comment_uin="10001", comment_nick="小明")
    action, reason = route_outbound(intent, "谢谢关注~", has_binary=False)
    assert action == "reply" and "c1" in reason


def test_reject_cases():
    assert route_outbound(None, "x", False)[0] == "reject"          # 无意图
    assert route_outbound(OutboundIntent(kind="reaction", tid="t", target_qq="8"), "", False)[0] == "reject"  # 空文本
    intent = OutboundIntent(kind="reaction", tid="t", target_qq="8")
    assert route_outbound(intent, "x", True)[0] == "reject"         # 二进制段
    assert route_outbound(OutboundIntent(kind="publish", tid="", target_qq=""), "x", False)[0] == "reject"  # M3 预留


# ---- 多次出站(设计变更 2026-09-01):同一意图允许多段回复逐条发出 ----


def test_outbound_intent_outbound_count_field_default_zero():
    """outbound_count 字段:默认 0,已出站次数由网关成功路径累计(多次出站防无限循环)。"""
    intent = OutboundIntent(kind="reaction", tid="tA", target_qq="8888")
    assert intent.outbound_count == 0
    intent.outbound_count += 1
    assert intent.outbound_count == 1  # 可变计数(网关成功路径自增)


def test_route_outbound_ignores_count_below_gateway_limit():
    """路由层不设限(上限由网关在意图层判,route_outbound 保持纯路由):
    outbound_count>0 的意图照常路由 comment/reply。"""
    reaction = OutboundIntent(kind="reaction", tid="tA", target_qq="8888", outbound_count=4)
    assert route_outbound(reaction, "第五条", has_binary=False) == ("comment", "tA")
    reply = OutboundIntent(kind="comment_reply", tid="tA", target_qq="8888",
                           comment_tid="c1", outbound_count=2)
    assert route_outbound(reply, "继续说", has_binary=False) == ("reply", "c1")
