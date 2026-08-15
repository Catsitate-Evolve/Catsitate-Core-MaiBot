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


def test_phase2_engines_assemble(tmp_path):
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.decay import DecayExecutor
    from catsitate_core.favorability import BatchEngine
    from catsitate_core.schedule import ScheduleGenerator, apply_schedule_edit
    from catsitate_core.sleep import SleepManager
    from catsitate_core.storage import JsonSnapshot, SQLiteStore
    cfg = CatsitateConfig()
    store = SQLiteStore(tmp_path / "catsitate.db")
    BatchEngine(store, cfg.favorability).ensure_schema()

    async def fake_llm(messages, model=""):
        return {"success": True, "response": "{}", "model": model}

    decay = DecayExecutor(store, cfg.favorability, fake_llm)
    sleep_mgr = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), cfg.sleep)
    gen = ScheduleGenerator(fake_llm, cfg.schedule, cfg.sleep)
    assert decay and sleep_mgr and gen
    # 工具修改冒烟
    data, err, hist = apply_schedule_edit(
        {"date": "2026-08-16", "windows": [
            {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:00"},
            {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
             "activity": "a", "plan_speak": False, "topic": ""}]},
        "add", None,
        {"kind": "daily", "start": "2026-08-16T14:00", "end": "2026-08-16T16:00",
         "activity": "b", "plan_speak": False, "topic": ""},
        [], min_sleep=cfg.sleep.min_sleep_minutes, max_sleep=cfg.sleep.max_sleep_minutes,
    )
    assert err == ""


def test_sleep_tick_natural_wake(tmp_path):
    """自然醒行为验证(审查 Critical #1):wake_at 过后 _sleep_tick 必须经 _wake_up 唤醒。

    23:00 入睡(wake_at 次日 07:00),tick 时刻推进到 07:00:05 后断言 state 已 awake、
    睡醒回顾触发;并验证醒来补跑仅经 _daily_settle(内部先衰减后结算),
    不再单独并发 spawn 衰减(审查 Important #2)。
    """

    import asyncio
    from datetime import datetime

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.sleep import SleepManager
    from catsitate_core.storage import JsonSnapshot

    class _FakeDateTime(datetime):
        _current = datetime(2026, 8, 15, 23, 0, 0)

        @classmethod
        def now(cls, tz=None):
            del tz
            return cls._current

    class _StubLogger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    class _StubCtx:
        def __init__(self):
            self.logger = _StubLogger()
            self.config = type("_C", (), {"get": staticmethod(lambda key, default="": None)})()

        async def call_capability(self, *a, **k):
            return {"success": True, "response": "{}"}

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx()
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p.config.sleep.enabled = True
    p.config.sleep.review_enabled = True
    p._background_tasks = set()
    p._schedule_data = {}
    p._last_activity_ts = 0.0
    p.sleep = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), p.config.sleep)
    p.sleep.enter_sleep(wake_at="2026-08-16T07:00:00", now=lambda: datetime(2026, 8, 15, 23, 0, 0))
    calls = {"review": 0, "settle": 0, "decay": 0}

    async def _fake_review():
        calls["review"] += 1

    async def _fake_settle():
        calls["settle"] += 1

    async def _fake_decay():
        calls["decay"] += 1

    p._write_sleep_review = _fake_review
    p._daily_settle = _fake_settle
    p._daily_decay = _fake_decay

    def run_tick():
        old = plugin_mod.datetime
        plugin_mod.datetime = _FakeDateTime
        try:
            async def _tick_and_pump():
                await p._sleep_tick()
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            asyncio.run(_tick_and_pump())
        finally:
            plugin_mod.datetime = old

    # 睡眠中(未到 wake_at):不醒,state 保持 sleep
    _FakeDateTime._current = datetime(2026, 8, 16, 6, 59, 59)
    run_tick()
    assert p.sleep.state.state == "sleep"
    assert calls == {"review": 0, "settle": 0, "decay": 0}

    # wake_at 过后(07:00:05):自然醒 → _wake_up → 回顾触发;补跑仅经 _daily_settle,不单独 spawn 衰减
    _FakeDateTime._current = datetime(2026, 8, 16, 7, 0, 5)
    run_tick()
    assert p.sleep.state.state == "awake"
    assert calls["review"] == 1
    assert calls["settle"] == 1
    assert calls["decay"] == 0  # 衰减由 _daily_settle 内部先衰减后结算,防并发双计


def test_build_proactive_intent_contains_schedule_and_fav():
    from catsitate_core.schedule import build_proactive_intent
    text = build_proactive_intent(
        {"kind": "daily", "activity": "发呆看雨", "plan_speak": True, "topic": "天气", "start": "2026-08-16T15:00", "end": "2026-08-16T18:00"},
        {"stream_id": "s1", "user_id": "u1", "level_name": "熟悉", "note": "无"},
        "今天:发呆→早睡",
    )
    assert "发呆看雨" in text and "熟悉" in text and "主动" in text


def test_schedule_inject_block_text():
    from catsitate_core.schedule import current_window, next_window
    data = {"date": "2026-08-16", "windows": [
        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00",
         "activity": "发呆看雨", "plan_speak": False, "topic": ""},
        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:00"},
    ]}
    now = "2026-08-16T10:00"
    win = current_window(data, now)
    nxt = next_window(data, now)
    line = f"[日程] 发呆看雨(至11:00)"
    if nxt:
        line += f";接下来:{nxt['kind'] if nxt['kind'] == 'sleep' else nxt.get('activity', '')}"
    assert "发呆看雨" in line and "接下来" in line
