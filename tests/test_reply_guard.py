"""reply 补传与哨兵判定测试:三条件触发/合并截断/哨兵解析;
v1.0.0 增 replyer 内容护栏钩子(实例级)与哨兵空 response 短路。"""

from __future__ import annotations

import asyncio
import re

from catsitate_core.config import CatsitateConfig
from catsitate_core.reply_guard import (
    backfill_reply_items,
    build_sentinel_prompt,
    merge_tool_results,
    parse_sentinel_response,
    should_backfill,
)

CTX_TOOLS = ["query_memory", "memo_read"]


def test_should_backfill_all_three_conditions():
    assert should_backfill(["memo_read"], "", "") is True


def test_should_backfill_reference_present_blocks():
    assert should_backfill(["memo_read"], "查过资料", "") is False


def test_should_backfill_reasoning_present_blocks():
    assert should_backfill(["memo_read"], "", "用户问过时间") is False


def test_should_backfill_no_context_tool_called():
    assert should_backfill(["web_search"], "", "") is False


def test_merge_tool_results_sorted_and_truncated():
    results = {"memo_read": "备忘甲", "query_memory": "记忆乙"}
    merged = merge_tool_results(results)
    assert merged.index("memo_read") < merged.index("query_memory")  # 工具名升序
    long_results = {f"tool{i}": "x" * 100 for i in range(10)}
    assert len(merge_tool_results(long_results, max_chars=400)) <= 400


def _call_item(func_name: str, args: dict, call_id: str = "call-1") -> dict:
    """构造宿主真实快照形态的 FunctionCallItem(实机快照格式,H-1 修复依据):
    工具名在 tool_call.func_name,参数在 tool_call.args——无顶层 tool_name/arguments 键。"""

    return {"item_type": "FunctionCallItem", "tool_call": {"call_id": call_id, "func_name": func_name, "args": args}}


def test_backfill_reply_items_only_targets_reply():
    """H-1 回归:按宿主快照形态(FunctionCallItem+tool_call)匹配 reply——
    一期误判顶层 tool_name/arguments 键,匹配恒不中,补传从未生效。"""
    items = [
        _call_item("reply", {"reply_reference": ""}, "call-r1"),
        _call_item("web_search", {"query": "天气"}, "call-w1"),
        _call_item("reply", {"reply_reference": "已有引用"}, "call-r2"),
    ]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, ["memo_read"], "")
    assert out[0]["tool_call"]["args"]["reply_reference"] == "[memo_read] 备忘内容"  # 合并摘要含工具名前缀
    assert "reply_reference" not in out[1]["tool_call"]["args"]  # 其它工具不动
    assert out[2]["tool_call"]["args"]["reply_reference"] == "已有引用"  # 已有引用不动
    # 浅拷贝纪律:宿主列表条目不被原地改写(入参 items 保持原值)
    assert items[0]["tool_call"]["args"]["reply_reference"] == ""
    assert out[0] is not items[0] and out[0]["tool_call"] is not items[0]["tool_call"]


def test_backfill_reply_items_reasoning_nonempty_skips():
    items = [_call_item("reply", {"reply_reference": ""})]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, ["memo_read"], "有推理")
    assert out[0]["tool_call"]["args"]["reply_reference"] == ""


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


# ---------- plugin 实例级:replyer 内容护栏钩子与哨兵空短路(v1.0.0) ----------


class _CollectLogger:
    """日志收集桩:记录 (level, args) 供告警文案断言。"""

    def __init__(self, logs):
        self._logs = logs

    def _record(self, level, args):
        self._logs.append((level, args))

    def info(self, *args, **kw):
        self._record("info", args)

    def warning(self, *args, **kw):
        self._record("warning", args)

    def error(self, *args, **kw):
        self._record("error", args)

    def exception(self, *args, **kw):
        self._record("exception", args)

    def debug(self, *args, **kw):
        self._record("debug", args)


def _make_guard_plugin():
    """最小插件实例(仅 replyer 钩子所需):收集日志的 ctx + 默认配置,总开关开。
    护栏启用态模拟绕过 on_load(等价 _assemble_guard 装配产物,该路径另有
    test_qzone_wiring.py::test_guard_assembly_compiles_on_load_path 覆盖)。"""

    import plugin as plugin_mod

    logs: list = []
    p = plugin_mod.CatsitatePlugin()
    p._ctx = type("_Ctx", (), {"logger": _CollectLogger(logs)})()
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p.logs = logs  # 测试侧便捷引用(非插件属性约定)
    return p


def test_content_guard_replyer_hit_blanks_response():
    """①护栏命中:modified_kwargs.response 置空串、output_items 原样保留
    (同一对象,未手工改 items)、入参 kwargs 不被原地改写;告警含规则号与
    文本前 60 字。群聊会话(护栏内容级,全部会话生效)。"""

    p = _make_guard_plugin()
    p.config.guard.enabled = True
    p._guard_compiled = [re.compile("敏感词")]
    items = [{"tool_name": "reply", "arguments": {"text": "这句话里有敏感词"}}]
    kwargs = {"response": "这句话里有敏感词,不能发", "output_items": items,
              "session_id": "group:12345", "attempt": 2}
    result = asyncio.run(p.content_guard_replyer(**kwargs))
    assert result["action"] == "continue"
    assert result["modified_kwargs"]["response"] == ""
    assert result["modified_kwargs"]["output_items"] is items  # 原样(主程序 replace_output_projection 自行投影)
    assert kwargs["response"] == "这句话里有敏感词,不能发"  # 入参不被原地改写
    assert kwargs["output_items"] is items
    assert any(
        level == "warning"
        and a[0] == "内容护栏拦截:回复 命中规则%d,置空未发送(文本:%s...)"
        and a[1] == 1 and a[2] == "这句话里有敏感词,不能发"
        for level, a in p.logs
    )


def test_content_guard_replyer_miss_passes_through():
    """②护栏启用未命中:kwargs 原样返回(同一 dict 对象),零拦截告警。私聊会话。"""

    p = _make_guard_plugin()
    p.config.guard.enabled = True
    p._guard_compiled = [re.compile("敏感词")]
    kwargs = {"response": "今天天气不错", "output_items": [], "session_id": "private:999", "attempt": 1}
    result = asyncio.run(p.content_guard_replyer(**kwargs))
    # 「原样」=内容全等(**kwargs 解包后 callee 持新 dict,身份断言无意义)
    assert result == {"action": "continue", "modified_kwargs": kwargs}
    assert kwargs["response"] == "今天天气不错"  # 入参不被原地改写
    assert not any("内容护栏拦截" in str(a[0]) for _, a in p.logs)


def test_content_guard_replyer_disabled_matching_text_passes():
    """③护栏关(默认):_guard_compiled 空列表匹配恒 0(天然短路),含命中
    模式的文本原样通过,零行为变化(零告警)。"""

    p = _make_guard_plugin()
    assert p.config.guard.enabled is False and p._guard_compiled == []
    kwargs = {"response": "带着敏感词也照发", "output_items": [], "session_id": "group:12345"}
    result = asyncio.run(p.content_guard_replyer(**kwargs))
    assert result == {"action": "continue", "modified_kwargs": kwargs}
    assert result["modified_kwargs"]["response"] == "带着敏感词也照发"
    assert p.logs == []


def test_sentinel_check_empty_response_skips_llm():
    """④组合链(EARLY→LATE 同钩子真实次序):guard 命中改空 response 后,
    哨兵拿到空文本在入口直接 continue——零 LLM 调用(免一次哨兵判定浪费;
    哨兵默认关,此为卫生处理)。"""

    p = _make_guard_plugin()
    p.config.guard.enabled = True
    p._guard_compiled = [re.compile("敏感词")]
    p.config.reply_guard.enabled = True
    p.config.reply_guard.sentinel_enabled = True
    llm_calls: list = []

    async def _llm(*args, **kw):
        llm_calls.append(args)
        return {"success": True, "response": '{"should_send": true, "reason": ""}'}

    p._side_llm_call = _llm
    # guard(EARLY)先拦截改空
    guard_result = asyncio.run(p.content_guard_replyer(
        response="含敏感词的回复要拦下", output_items=[], session_id="group:12345"))
    assert guard_result["modified_kwargs"]["response"] == ""
    # 哨兵(LATE)拿到改空后的 kwargs:入口短路原样 continue,零 LLM
    sent_result = asyncio.run(p.sentinel_check(**guard_result["modified_kwargs"]))
    assert sent_result == {"action": "continue", "modified_kwargs": guard_result["modified_kwargs"]}
    assert llm_calls == []
