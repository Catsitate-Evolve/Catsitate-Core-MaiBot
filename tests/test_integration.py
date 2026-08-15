"""离线集成冒烟:把全部引擎按 plugin.py 装配方式串起来跑一遍(不依赖 MaiBot)。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from catsitate_core.config import CatsitateConfig
from catsitate_core.favorability import BatchEngine, SettleExecutor, build_favorability_block
from catsitate_core.inject import InjectAssembler, InjectionBlock
from catsitate_core.memo import MemoService
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.poke import PokeEngine
from catsitate_core.reply_guard import backfill_reply_items
from catsitate_core.storage import JsonSnapshot, SQLiteStore
from catsitate_core.time_aware import build_environment_text

NOW = datetime(2026, 8, 14, 12, 0, 0)


def _fake_llm(messages, model=""):
    async def call(messages, model=""):
        system_text = str(messages[0]["content"])
        if "关系评估助手" in system_text:
            return {"success": True, "response": '{"delta": 2, "note": "冒烟注记"}', "model": model}
        return {"success": True, "response": '{"emoji_id": "em_laugh"}', "model": model}
    return call(messages, model)


def test_full_assembly_smoke(tmp_path):
    """按 plugin.py 的装配顺序:建库→建引擎→注入→结算→贴表情→戳校验→补传。"""

    store = SQLiteStore(tmp_path / "smoke.db")
    cfg = CatsitateConfig()
    memo = MemoService(store, cfg.memo)
    react = MsgReactEngine(JsonSnapshot(tmp_path / "react.json"), cfg.msg_react)
    poke = PokeEngine(JsonSnapshot(tmp_path / "poke.json"), cfg.poke)
    engine = BatchEngine(store, cfg.favorability)
    for service in (memo, engine):
        service.ensure_schema()
    assembler = InjectAssembler()

    # 备忘
    assert memo.write("周四交作业", "s1", "u1", None)[0] is True
    entries = memo.read("s1", "u1", limit=5)
    assert entries and entries[0]["content"] == "周四交作业"

    # 环境块
    env_text = build_environment_text(date(2026, 8, 14), "北京", {"temperature_2m": 29.0, "weather_code": 0}, ["七夕"], [])
    assert "[环境]" in env_text and "北京" in env_text

    # 注入渲染(三块顺序:环境/备忘/好感度;等级规则按等级并入好感度块)
    blocks = [
        InjectionBlock("environment", "env", env_text),
        InjectionBlock("memo", "memo:1", "[备忘] 周四交作业"),
        InjectionBlock("favorability", "fav:u1", build_favorability_block(engine, "u1", "s1", include_rule=True)),
    ]
    rendered = assembler.render(blocks)
    assert [m["role"] for m in rendered] == ["user"] * 3
    assert rendered[0]["content"].startswith("[环境]")
    assert "规则「陌生」" in rendered[2]["content"]

    # 好感度:计数→提前触发→结算(fake LLM)
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    assert engine.check_trigger("u1", "s1", now=lambda: NOW) == "early"
    assert ("u1", "s1") in engine.iter_today_active(now=lambda: NOW)  # 结算前在活跃列表
    executor = SettleExecutor(engine, _fake_llm)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "text": f"消息{i}", "seq": i, "ts": f"2026-08-14T11:{i:02d}:00"}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", "s1", history, kind="early"))
    assert result["status"] == "ok" and result["delta"] == 2
    assert ("u1", "s1") not in engine.iter_today_active(now=lambda: NOW)  # 结算 reset 后 count=0
    assert "累计 2" in build_favorability_block(engine, "u1", "s1")  # 2 分仍是「陌生」级

    # 贴表情(内置 QQ 表情表)
    messages, _ = react.build_choose_prompt("今天好累", "安慰")
    assert messages[0]["role"] == "system"
    choice, err = parse_choice_resp('{"emoji_id": "5"}')
    assert choice == "5" and err == ""

    # 主动戳校验(已取消等级门槛,仅冷却)
    ok, reason = poke.can_poke("u1", now=lambda: NOW)
    assert ok is True and reason == ""

    # reply 补传
    items = [{"tool_name": "reply", "arguments": {"reply_reference": ""}}]
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, cfg.reply_guard.context_tools, ["memo_read"], "")
    assert out[0]["arguments"]["reply_reference"] == "[memo_read] 备忘内容"
