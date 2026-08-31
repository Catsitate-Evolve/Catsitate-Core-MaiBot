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
