"""出站消息组件提取测试(text 段拼接/reply·at 忽略/二进制检测/quote 目标提取)。"""
from catsitate_core.qzone.outbound import M1_OUTBOUND_ERROR, extract_outbound_text, extract_quote_target


def _msg(raw):
    return {"raw_message": raw, "message_info": {}, "message_id": "m1"}


def test_extract_plain_text():
    text, has_binary = extract_outbound_text(_msg([
        {"type": "text", "data": "好看!"},
        {"type": "text", "data": "这只猫也太胖了"},
    ]))
    assert text == "好看!这只猫也太胖了" and has_binary is False


def test_extract_ignores_reply_and_at():
    text, has_binary = extract_outbound_text(_msg([
        {"type": "reply", "data": {"target_message_id": "qzone_t1_1"}},
        {"type": "at", "data": {"target_user_id": "10001"}},
        {"type": "text", "data": "同感~"},
    ]))
    assert text == "同感~" and has_binary is False


def test_extract_detects_binary():
    text, has_binary = extract_outbound_text(_msg([
        {"type": "text", "data": "看这个"},
        {"type": "image", "data": "图", "binary_data_base64": "AAAA"},
    ]))
    assert text == "看这个" and has_binary is True


def test_m1_error_message_is_explicit():
    assert "M1" in M1_OUTBOUND_ERROR and "M2" in M1_OUTBOUND_ERROR  # 显式说明阶段与去向


# ---- 深度审查 A-1:reply 段 quote 目标提取(意图绑定校验的输入) ----


def test_extract_quote_target_from_reply_segment():
    """reply 段的 target_message_id 即 quote 目标(意图绑定校验比对用)。"""
    msg = _msg([
        {"type": "reply", "data": {"target_message_id": "qzone_t1_7"}},
        {"type": "text", "data": "同感~"},
    ])
    assert extract_quote_target(msg) == "qzone_t1_7"


def test_extract_quote_target_empty_without_reply_segment():
    """无 reply 段返回空串(大部分 planner 出站不带引用→跳过绑定校验);畸形形态容错。"""
    assert extract_quote_target(_msg([{"type": "text", "data": "好看"}])) == ""
    assert extract_quote_target(_msg([{"type": "reply", "data": "非对象"}])) == ""
    assert extract_quote_target(_msg([{"type": "reply", "data": {}}])) == ""
    assert extract_quote_target(None) == ""  # 消息本体缺失(防御形态)
