"""reply 补传与哨兵判定测试:三条件触发/合并截断/哨兵解析。"""

from catsitate_core.reply_guard import (
    backfill_reply_items,
    build_sentinel_prompt,
    merge_tool_results,
    parse_sentinel_response,
    should_backfill,
)

CTX_TOOLS = ["query_memory", "memo_read"]


def test_should_backfill_all_three_conditions():
    assert should_backfill(["memo_read"], CTX_TOOLS, "", "") is True


def test_should_backfill_reference_present_blocks():
    assert should_backfill(["memo_read"], CTX_TOOLS, "查过资料", "") is False


def test_should_backfill_reasoning_present_blocks():
    assert should_backfill(["memo_read"], CTX_TOOLS, "", "用户问过时间") is False


def test_should_backfill_no_context_tool_called():
    assert should_backfill(["web_search"], CTX_TOOLS, "", "") is False


def test_merge_tool_results_sorted_and_truncated():
    results = {"memo_read": "备忘甲", "query_memory": "记忆乙"}
    merged = merge_tool_results(results)
    assert merged.index("memo_read") < merged.index("query_memory")  # 工具名升序
    long_results = {f"tool{i}": "x" * 100 for i in range(10)}
    assert len(merge_tool_results(long_results, max_chars=400)) <= 400


def test_backfill_reply_items_only_targets_reply():
    items = [
        {"tool_name": "reply", "arguments": {"reply_reference": ""}},
        {"tool_name": "web_search", "arguments": {"query": "天气"}},
        {"tool_name": "reply", "arguments": {"reply_reference": "已有引用"}},
    ]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, CTX_TOOLS, ["memo_read"], "")
    assert out[0]["arguments"]["reply_reference"] == "[memo_read] 备忘内容"  # 合并摘要含工具名前缀
    assert "reply_reference" not in out[1]["arguments"]  # 其它工具不动
    assert out[2]["arguments"]["reply_reference"] == "已有引用"  # 已有引用不动


def test_backfill_reply_items_reasoning_nonempty_skips():
    items = [{"tool_name": "reply", "arguments": {"reply_reference": ""}}]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, CTX_TOOLS, ["memo_read"], "有推理")
    assert out[0]["arguments"]["reply_reference"] == ""


def test_build_sentinel_prompt_stable_prefix():
    messages, cache_key = build_sentinel_prompt("猫耳少女", "回复内容", "聊天上下文")
    assert messages[0]["role"] == "system"
    assert "猫耳少女" in messages[1]["content"]  # 人设背景在稳定段
    assert "回复内容" in messages[-2]["content"]
    assert "聊天上下文" in messages[-1]["content"]
    assert cache_key


def test_parse_sentinel_response():
    assert parse_sentinel_response('{"should_send": false, "reason": "与上下文不符"}') == (False, "与上下文不符")


def test_parse_sentinel_response_invalid():
    ok, reason = parse_sentinel_response("无法判断")
    assert ok is None and reason
    ok2, reason2 = parse_sentinel_response("[]")  # 合法 JSON 非对象同样拒绝
    assert ok2 is None and reason2


def test_parse_sentinel_bare_braces():
    # 无围栏时提取第一段裸花括号(与 parse_judge_response 兜底一致)
    ok, reason = parse_sentinel_response('判定结果:\n{"should_send": false, "reason": "与上下文不符"}')
    assert ok is False and reason == "与上下文不符"
