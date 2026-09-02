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


class _StubPaths:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _StubCtx:
    def __init__(self, data_dir, recent=None, by_id=None):
        self.logger = _StubLogger()
        self.paths = _StubPaths(data_dir)
        self.config = type("_C", (), {"get": staticmethod(lambda key, default="": None)})()
        # call_capability 桩数据:recent = message.get_recent 返回;by_id = message_id -> 序列化消息 dict
        self._recent = [] if recent is None else recent
        self._by_id = {} if by_id is None else by_id
        self._streams = [
            {"session_id": "g1", "is_group_session": True, "user_id": ""},
            {"session_id": "p1", "is_group_session": False, "user_id": "u1"},
        ]
        self.chat = type("_C", (), {"get_all_streams": self._get_all_streams})()

    async def _get_all_streams(self, platform="qq"):
        del platform  # 桩与 SDK 签名对齐(get_all_streams(platform=...),Task 10 双平台取数)
        return list(self._streams)

    async def call_capability(self, name, **kw):
        """桩:message.get_by_id 按注入的 by_id 表应答(异常/None 透传,模拟实机失败路径)。"""
        if name == "message.get_recent":
            return list(self._recent)
        if name == "message.get_by_id":
            return self._by_id.get(str(kw.get("message_id")))
        if name == "chat.get_all_streams":
            return list(self._streams)
        return {"success": True, "response": "{}"}


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
        InjectionBlock("favorability", "fav:u1", build_favorability_block(engine, "u1", include_rule=True)),
    ]
    rendered = assembler.render(blocks)
    assert [m["role"] for m in rendered] == ["user"] * 3
    assert rendered[0]["content"].startswith("[环境]")
    assert "规则「陌生」" in rendered[2]["content"]

    # 好感度:计数→提前触发→结算(fake LLM)
    for _ in range(20):
        engine.count_message("u1", "s1", now=lambda: NOW)
    assert engine.check_trigger("u1", now=lambda: NOW) == "early"
    assert "u1" in engine.iter_today_active(now=lambda: NOW)  # 结算前在活跃列表
    executor = SettleExecutor(engine, _fake_llm)
    history = [
        {"role": "user", "user_id": "u1", "stream_id": "s1", "is_group": False, "addressed": None, "text": f"消息{i}", "seq": i, "ts": f"2026-08-14T11:{i:02d}:00"}
        for i in range(20)
    ]
    result = asyncio.run(executor.settle("u1", history, kind="early"))
    assert result["status"] == "ok" and result["delta"] == 2
    assert "u1" not in engine.iter_today_active(now=lambda: NOW)  # 结算 reset 后 count=0
    assert "累计 2" in build_favorability_block(engine, "u1")  # 2 分仍是「陌生」级

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
    out = backfill_reply_items(items, {"memo_read": "备忘内容"}, ["memo_read"], "")
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
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")  # 醒态 sleep_tick 补注日记读取(on_load 装配,离线手工补)
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


def test_schedule_block_appends_qzone_browsing_suffix(tmp_path):
    """规格 §6 集成表(M3 修正 I-2):read_qzone 窗口的日程注入块行末追加
    「(正在刷QQ空间)」(明文状态,告知 planner 当前正在刷空间);普通 daily
    窗口不追加。"""
    import asyncio
    from datetime import timedelta

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.storage import SQLiteStore

    def _make(tmp_path):
        now = datetime.now()
        p = plugin_mod.CatsitatePlugin()
        p._ctx = _StubCtx(tmp_path)
        p._plugin_config_instance = CatsitateConfig()
        p.config.plugin.enabled = True
        p.memo = type("_M", (), {
            "read": staticmethod(lambda *a, **k: []),
            "due_on": staticmethod(lambda day: []),
        })()
        p._snapshot_cache = {}
        p._env_cache = {}
        p._env_fetched_at = None
        p._stream_cache = {}
        p._stream_cache_at = 0.0
        p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
            "kind": "daily", "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "activity": "窝着刷手机", "read_qzone": True,
        }]}
        p._schedule_tick_fired = {}
        p._qzone_available = False
        p.fav_engine = BatchEngine(SQLiteStore(tmp_path / "fav.db"), p.config.favorability)
        p.fav_engine.ensure_schema()
        return p

    p = _make(tmp_path)
    blocks = asyncio.run(p._build_inject_blocks({"session_id": "p1"}))
    sch = [b for b in blocks if b.module == "schedule"]
    assert len(sch) == 1
    assert sch[0].text.startswith("[日程] 窝着刷手机")
    assert sch[0].text.endswith("(正在刷QQ空间)")  # read_qzone 窗口:行末追加明文状态

    p._schedule_data["windows"][0]["read_qzone"] = False  # 普通窗口:不追加
    blocks = asyncio.run(p._build_inject_blocks({"session_id": "p1"}))
    sch = [b for b in blocks if b.module == "schedule"]
    assert len(sch) == 1
    assert "(正在刷QQ空间)" not in sch[0].text


def test_generate_tomorrow_schedule_sets_generated_flag(tmp_path):
    """I-2:模板兜底日 _schedule_generated=False(备忘提醒兜底保持开启);LLM 成功日 True。"""
    import asyncio
    from datetime import datetime, timedelta

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.schedule import DEFAULT_TEMPLATE_SCHEDULE, _materialize_template

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()

    async def _no_persona():
        return ""
    p._persona = _no_persona
    p._today_review_text = lambda: "无"
    p._weather_text = lambda: "无"
    p._fav_summary_text = lambda: "无"
    p.memo = type("_M", (), {"due_on": staticmethod(lambda day: [])})()
    p._schedule_data = {}
    p._schedule_edit_history = []
    p._schedule_generated = False

    class _FailingGen:
        async def generate(self, **kw):
            return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, kw["target_date"]), "测试错误"

    p.schedule_gen = _FailingGen()
    asyncio.run(p._generate_tomorrow_schedule())
    assert p._schedule_generated is False  # 模板兜底日不视为生成日程

    class _OkGen:
        async def generate(self, **kw):
            target = kw["target_date"]
            nxt_day = (datetime.strptime(target, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            return {"date": target, "windows": [
                {"kind": "sleep", "start": f"{target}T23:00", "end": f"{nxt_day}T07:00"},
                {"kind": "daily", "start": f"{target}T09:00", "end": f"{target}T11:00",
                 "activity": "写代码", "plan_speak": False, "topic": ""},
            ]}, ""

    p.schedule_gen = _OkGen()
    asyncio.run(p._generate_tomorrow_schedule())
    assert p._schedule_generated is True


def test_remind_fallback_tick_injects_when_schedule_not_generated(tmp_path):
    """I-2:模板撑场日(_schedule_generated=False)到点提醒仍兜底注入;LLM 生成日不重复兜底。"""
    import asyncio
    from datetime import datetime

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.schedule import DEFAULT_TEMPLATE_SCHEDULE, _materialize_template
    from catsitate_core.sleep import SleepManager
    from catsitate_core.storage import JsonSnapshot

    appended: list[dict] = []

    class _ContextStub:
        def __init__(self, out):
            self._out = out

        async def append(self, **kw):
            self._out.append(kw)

    ctx = _StubCtx(tmp_path)
    ctx.maisaka = type("_M", (), {"context": _ContextStub(appended)})()

    p = plugin_mod.CatsitatePlugin()
    p._ctx = ctx
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p.sleep = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), p.config.sleep)
    p._schedule_data = _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, datetime.now().strftime("%Y-%m-%d"))
    p._schedule_generated = False
    p._schedule_tick_fired = {}  # _prune_day_keys 依赖(M13:兜底 tick 同步清理)
    p._speak_counts = {}
    p._remind_fired = {}
    p._remind_fired_snapshot = JsonSnapshot(tmp_path / "remind_fired.json")
    p.memo = type("_M", (), {"due_on": staticmethod(lambda day: [
        {"id": 1, "content": "交作业", "stream_id": "s1", "user_id": "u1", "remind_at": "2000-01-01T00:00:00"},
    ])})()
    asyncio.run(p._remind_fallback_tick())
    assert len(appended) == 1 and appended[0]["stream_id"] == "s1"
    # LLM 生成日:提醒走日程收录,不重复兜底
    p._schedule_generated = True
    appended.clear()
    asyncio.run(p._remind_fallback_tick())
    assert appended == []


def test_enter_sleep_idempotent_when_already_sleeping(tmp_path):
    """I-3:已睡时 _enter_sleep 直接返回,不二次落盘、不二次 spawn 次日日程生成。"""
    import asyncio
    from datetime import datetime

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.sleep import SleepManager
    from catsitate_core.storage import JsonSnapshot

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p._background_tasks = set()
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")  # 醒态 sleep_tick 补注日记读取(on_load 装配,离线手工补)
    p._schedule_data = {}
    p.sleep = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), p.config.sleep)
    p.sleep.enter_sleep(wake_at="2099-01-01T07:00:00", now=lambda: datetime(2026, 8, 15, 23, 0, 0))
    calls = {"gen": 0}

    async def _fake_gen():
        calls["gen"] += 1
    p._generate_tomorrow_schedule = _fake_gen

    asyncio.run(p._enter_sleep())
    assert calls["gen"] == 0  # 已睡幂等:不再 spawn
    assert p.sleep.state.wake_at == "2099-01-01T07:00:00"  # 状态未被二次覆盖


def test_restore_schedule_from_file_today_only(tmp_path):
    """I-4:重启恢复——当日 schedule.json 恢复日程/历史/生成标记;过期删除、损坏忽略(告警)。"""
    import json
    from datetime import datetime

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.schedule import DEFAULT_TEMPLATE_SCHEDULE, _materialize_template

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p._schedule_data = {}
    p._schedule_edit_history = []
    p._schedule_generated = False

    today = datetime.now().strftime("%Y-%m-%d")
    p._ctx.paths.data_dir.mkdir(parents=True, exist_ok=True)
    (p._ctx.paths.data_dir / "schedule.json").write_text(json.dumps({
        "data": _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, today),
        "edit_history": [{"time": "x", "action": "add", "before": "{}", "after": "{}"}],
        "generated": True, "saved_at": "2026-08-15T10:00:00",
    }, ensure_ascii=False), encoding="utf-8")
    p._restore_schedule()
    assert p._schedule_data.get("date") == today
    assert p._schedule_generated is True
    assert len(p._schedule_edit_history) == 1

    # 过期文件:删除并忽略
    stale = {"data": _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, "2000-01-01"), "generated": True}
    (p._ctx.paths.data_dir / "schedule.json").write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    p._schedule_data = {}
    p._restore_schedule()
    assert p._schedule_data == {}
    assert not (p._ctx.paths.data_dir / "schedule.json").exists()  # 过期文件已删除

    # 损坏文件:告警并忽略(不删除,保留现场)
    (p._ctx.paths.data_dir / "schedule.json").write_text("{broken", encoding="utf-8")
    p._restore_schedule()
    assert p._schedule_data == {}
    assert (p._ctx.paths.data_dir / "schedule.json").exists()


def test_restore_schedule_tomorrow_date_restored_and_kept(tmp_path):
    """I2:入睡当晚生成的是次日日程,夜间重启不得误删——date=明天 → 恢复成功且不删文件。"""
    import json
    from datetime import datetime, timedelta

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.schedule import DEFAULT_TEMPLATE_SCHEDULE, _materialize_template

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p._schedule_data = {}
    p._schedule_edit_history = []
    p._schedule_generated = False

    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    path = p._ctx.paths.data_dir / "schedule.json"
    p._ctx.paths.data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "data": _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, tomorrow),
        "edit_history": [{"time": "x", "action": "add", "before": "{}", "after": "{}"}],
        "generated": True, "saved_at": "2026-08-15T23:00:00",
    }, ensure_ascii=False), encoding="utf-8")
    p._restore_schedule()
    assert p._schedule_data.get("date") == tomorrow  # 明日期恢复成功
    assert p._schedule_generated is True
    assert path.exists()  # 明日期文件不删除(仅真正过期才 unlink)


def test_restore_schedule_stale_but_sleep_window_active(tmp_path):
    """跨午夜边界(公测发现):过期日程的睡眠窗口仍覆盖当前时刻 → 保留恢复,不删除。

    场景:昨夜 23:00 睡、今晨 00:30 重启——直接删除旧日程会导致当天无睡眠窗口、无法入睡。
    """
    import json
    from datetime import datetime, timedelta

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p._schedule_data = {}
    p._schedule_edit_history = []
    p._schedule_generated = False

    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    # 睡眠窗口覆盖当前时刻(用当前时间 ±1h 构造,测试任意时刻运行都命中)
    stale = {"data": {
        "date": yesterday,
        "windows": [{
            "kind": "sleep",
            "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
            "activity": "",
        }],
    }, "generated": True}
    path = p._ctx.paths.data_dir / "schedule.json"
    p._ctx.paths.data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    p._restore_schedule()
    assert p._schedule_data.get("date") == yesterday  # 睡眠窗口进行中:保留恢复
    assert path.exists()  # 文件不删除

    # 睡眠窗口已结束的过期日程:仍删除(原语义)
    ended = {"data": {
        "date": yesterday,
        "windows": [{
            "kind": "sleep",
            "start": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
            "end": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "activity": "",
        }],
    }, "generated": True}
    path.write_text(json.dumps(ended, ensure_ascii=False), encoding="utf-8")
    p._schedule_data = {}
    p._restore_schedule()
    assert p._schedule_data == {}
    assert not path.exists()


def test_schedule_tick_cross_midnight_sleep_window_enters_sleep(tmp_path):
    """跨午夜(公测发现,联调裁定 2026-08-17):换日时旧日程睡眠窗口仍在进行 →
    _schedule_tick 保留旧日程不换模板;静默关闭时 _sleep_tick 按旧窗口直接入睡。"""
    import asyncio
    from datetime import datetime, timedelta

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.sleep import SleepManager
    from catsitate_core.storage import JsonSnapshot

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p.config.schedule.enabled = True
    p.config.sleep.silent_sleep_enabled = False  # 静默关:窗口起点直接睡(Q3)
    p.sleep = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), p.config.sleep)
    p._background_tasks = set()
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")  # 醒态 sleep_tick 补注日记读取(on_load 装配,离线手工补)
    p._schedule_tick_fired = {}
    p._speak_counts = {}
    p._remind_fired = {}

    async def _fake_gen():
        pass
    p._generate_tomorrow_schedule = _fake_gen

    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    p._schedule_data = {"date": yesterday, "windows": [{
        "kind": "sleep",
        "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "",
    }]}
    p._schedule_generated = True

    asyncio.run(p._schedule_tick())
    assert p.sleep.state.state != "sleep"  # schedule_tick 不入睡:保留旧日程交给 sleep_tick
    assert p._schedule_data.get("date") == yesterday  # 睡眠窗口进行中不换模板

    asyncio.run(p._sleep_tick())
    assert p.sleep.state.state == "sleep"  # 静默关闭:直接入睡


def test_sleep_tick_silent_on_quiet_elapsed_enters_sleep(tmp_path):
    """静默睡眠开(联调裁定 2026-08-17):睡眠窗口内安静满 N 分钟才入睡;有活动不睡。"""
    import asyncio
    from datetime import datetime, timedelta

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.sleep import SleepManager
    from catsitate_core.storage import JsonSnapshot

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p.config.sleep.enabled = True
    p.config.sleep.silent_sleep_enabled = True
    p.config.sleep.silent_sleep_minutes = 60
    p.sleep = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), p.config.sleep)
    p._background_tasks = set()
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")  # 醒态 sleep_tick 补注日记读取(on_load 装配,离线手工补)
    p._sleep_window_settled = ""

    async def _fake_gen():
        pass
    p._generate_tomorrow_schedule = _fake_gen

    now = datetime.now()
    p._schedule_data = {"date": now.strftime("%Y-%m-%d"), "windows": [{
        "kind": "sleep",
        "start": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "",
    }]}

    # 刚有活动:不睡
    p._last_activity_ts = now.timestamp() - 60  # 1 分钟前
    asyncio.run(p._sleep_tick())
    assert p.sleep.state.state != "sleep"

    # 安静超过 N 分钟:入睡(基准=max(窗口起点,最后活动))
    p._last_activity_ts = now.timestamp() - 3600  # 60 分钟前,与窗口起点同刻
    asyncio.run(p._sleep_tick())
    assert p.sleep.state.state == "sleep"


def test_sleep_window_passed_awake_settles_once(tmp_path):
    """Q1 裁定:睡眠窗口已过而未入睡 → 不入睡,补执行次日日程生成;每窗口仅一次。"""
    import asyncio
    from datetime import datetime, timedelta

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.sleep import SleepManager
    from catsitate_core.storage import JsonSnapshot

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p.config.sleep.enabled = True
    p.sleep = SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), p.config.sleep)
    p._background_tasks = set()
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")  # 醒态 sleep_tick 补注日记读取(on_load 装配,离线手工补)
    p._sleep_window_settled = ""

    calls = {"gen": 0}

    async def _fake_gen():
        calls["gen"] += 1
    p._generate_tomorrow_schedule = _fake_gen

    now = datetime.now()
    p._schedule_data = {"date": (now - timedelta(days=1)).strftime("%Y-%m-%d"), "windows": [{
        "kind": "sleep",
        "start": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
        "end": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        "activity": "",
    }]}

    asyncio.run(p._sleep_tick())
    assert p.sleep.state.state != "sleep"  # 不入睡
    assert calls["gen"] == 1  # 补执行次日日程生成

    asyncio.run(p._sleep_tick())
    assert calls["gen"] == 1  # 每窗口仅一次(标记去重)

    # 入睡过的窗口不补执行(入睡时已生成并标记)
    calls["gen"] = 0
    p.sleep.wake()  # 复位状态便于构造:直接模拟已标记场景
    p._sleep_window_settled = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    asyncio.run(p._sleep_tick())
    assert calls["gen"] == 0


class _CollectLogger:
    """收集日志的 stub logger(断言主动问候跳过日志)。"""

    def __init__(self, logs: list):
        self._logs = logs

    def _record(self, level, a, k):
        self._logs.append((level, a, k))

    def info(self, *a, **k):
        self._record("info", a, k)

    def warning(self, *a, **k):
        self._record("warning", a, k)

    def exception(self, *a, **k):
        self._record("exception", a, k)

    def error(self, *a, **k):
        self._record("error", a, k)

    def debug(self, *a, **k):
        self._record("debug", a, k)


class _ProactiveStub:
    def __init__(self, out: list):
        self._out = out

    async def trigger(self, **kw):
        self._out.append(kw)


def _make_greet_plugin(tmp_path, logs):
    """构造 _greet_exclusive 测试用插件实例:真实 store/fav_engine + 收集日志。"""

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.favorability import BatchEngine
    from catsitate_core.storage import SQLiteStore

    ctx = _StubCtx(tmp_path)
    ctx.logger = _CollectLogger(logs)
    p = plugin_mod.CatsitatePlugin()
    p._ctx = ctx
    p._plugin_config_instance = CatsitateConfig()
    p._speak_counts = {}
    p.store = SQLiteStore(tmp_path / "greet.db")
    p.fav_engine = BatchEngine(p.store, p.config.favorability)
    p.fav_engine.ensure_schema()
    return p


def _make_exclusive_plugin(tmp_path, logs, streams=None):
    """装配特别者(u1)与流缓存:streams 为 {stream_id: {is_group_session, user_id}}。"""

    p = _make_greet_plugin(tmp_path, logs)
    p.fav_engine.apply_delta("u1", 100, "特别之选", judged_at="2026-08-16T08:00:00")
    if streams is not None:
        async def _stub_refresh():
            p._stream_cache = {
                sid: {"session_id": sid, "is_group_session": info.get("is_group_session", False),
                      "user_id": info.get("user_id", "")}
                for sid, info in streams.items()
            }
        p._refresh_stream_cache = _stub_refresh
    return p


_GREET_WIN = {"kind": "greeting", "activity": "早安", "start": "2026-08-16T08:00", "end": "2026-08-16T08:30"}


def test_greet_exclusive_no_special_no_trigger(tmp_path):
    """主动问候:无特别者 → 不 trigger(库空直接返回 False)。"""

    logs: list = []
    p = _make_greet_plugin(tmp_path, logs)
    triggered: list[dict] = []
    p._ctx.maisaka = type("_M", (), {"proactive": _ProactiveStub(triggered)})()
    assert asyncio.run(p._greet_exclusive("2026-08-16", _GREET_WIN)) is False
    assert triggered == []


def test_greet_exclusive_no_private_stream_logs_skip(tmp_path):
    """主动问候:特别者无私聊流 → 不 trigger 且有跳过日志。"""

    logs: list = []
    p = _make_exclusive_plugin(tmp_path, logs, streams={"g1": {"is_group_session": True, "user_id": "u1"}})
    triggered: list[dict] = []
    p._ctx.maisaka = type("_M", (), {"proactive": _ProactiveStub(triggered)})()
    assert asyncio.run(p._greet_exclusive("2026-08-16", _GREET_WIN)) is False
    assert triggered == []
    assert any(level == "info" and "无私聊流" in str(a) for level, a, k in logs)


def test_greet_exclusive_private_stream_triggers(tmp_path):
    """主动问候:有私聊流 → trigger 且 speak_counts+1。"""

    logs: list = []
    p = _make_exclusive_plugin(tmp_path, logs, streams={"p1": {"is_group_session": False, "user_id": "u1"}})
    triggered: list[dict] = []
    p._ctx.maisaka = type("_M", (), {"proactive": _ProactiveStub(triggered)})()
    assert asyncio.run(p._greet_exclusive("2026-08-16", _GREET_WIN)) is True
    assert len(triggered) == 1
    assert triggered[0]["stream_id"] == "p1"
    assert "特别" in str(triggered[0]["intent"])
    assert p._speak_counts["2026-08-16"] == 1


def test_greet_exclusive_speak_limit_blocks(tmp_path):
    """主动问候:达 daily_speak_limit → 不 trigger。"""

    logs: list = []
    p = _make_exclusive_plugin(tmp_path, logs, streams={"p1": {"is_group_session": False, "user_id": "u1"}})
    triggered: list[dict] = []
    p._ctx.maisaka = type("_M", (), {"proactive": _ProactiveStub(triggered)})()
    p._speak_counts = {"2026-08-16": p.config.schedule.daily_speak_limit}
    assert asyncio.run(p._greet_exclusive("2026-08-16", _GREET_WIN)) is False
    assert triggered == []


def test_greet_exclusive_multiple_windows_no_daily_limit(tmp_path):
    """主动问候:连续两个 greeting 窗口都触发(无每日一次限制,仅受 daily_speak_limit 约束)。"""

    logs: list = []
    p = _make_exclusive_plugin(tmp_path, logs, streams={"p1": {"is_group_session": False, "user_id": "u1"}})
    triggered: list[dict] = []
    p._ctx.maisaka = type("_M", (), {"proactive": _ProactiveStub(triggered)})()
    morning = dict(_GREET_WIN)
    night = {"kind": "greeting", "activity": "晚安", "start": "2026-08-16T22:00", "end": "2026-08-16T23:00"}
    assert asyncio.run(p._greet_exclusive("2026-08-16", morning)) is True
    assert asyncio.run(p._greet_exclusive("2026-08-16", night)) is True
    assert len(triggered) == 2
    assert p._speak_counts["2026-08-16"] == 2


def test_greet_exclusive_trigger_exception_no_count(tmp_path):
    """主动问候:proactive.trigger 抛异常 → 返回 False 且不增加 speak_counts(错误显式暴露)。"""

    logs: list = []
    p = _make_exclusive_plugin(tmp_path, logs, streams={"p1": {"is_group_session": False, "user_id": "u1"}})

    class _FailingProactive:
        async def trigger(self, **kw):
            del kw
            raise RuntimeError("proactive 不可用")

    p._ctx.maisaka = type("_M", (), {"proactive": _FailingProactive()})()
    assert asyncio.run(p._greet_exclusive("2026-08-16", _GREET_WIN)) is False
    assert p._speak_counts.get("2026-08-16", 0) == 0  # 失败不计数
    assert any(level == "exception" for level, a, k in logs)  # logger.exception 记录现场


def test_settle_and_log_per_user_aggregates_streams(tmp_path):
    """按人结算接线:check_trigger(user_id) 与 _settle_and_log(user_id, kind) 按人调用不抛错,
    且结算聚合该人全部流的素材;同 (user, kind) 并发防护直接跳过。"""

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.favorability import BatchEngine
    from catsitate_core.storage import SQLiteStore

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p._settling = set()
    p._speak_counts = {}
    p.store = SQLiteStore(tmp_path / "settle.db")
    p.fav_engine = BatchEngine(p.store, p.config.favorability)
    p.fav_engine.ensure_schema()
    p.fav_executor = SettleExecutor(p.fav_engine, _fake_llm)  # 与 plugin.py on_load 同装配
    p.fav_engine.count_message("u1", "s1")
    p.fav_engine.count_message("u1", "s2")
    fetched: list[tuple[str, str]] = []

    async def _fetch_stub(stream_id, limit, target_user_id=""):
        del limit
        fetched.append((stream_id, target_user_id))
        return []  # 空素材 → settle 返回 failed(素材为空),不走 LLM

    async def _no_persona():
        return "猫耳少女"

    p._fetch_recent_for_history = _fetch_stub
    p._persona = _no_persona
    # check_trigger 按人调用不抛错(2 条 < 阈值 20,不触发早结)
    assert p.fav_engine.check_trigger("u1") is None
    asyncio.run(p._settle_and_log("u1", kind="daily"))
    assert set(s for s, _ in fetched) == {"s1", "s2"}  # 聚合该人全部流素材
    assert all(t == "u1" for _, t in fetched)  # addressed 判定对象传结算目标用户
    # 并发防护:该用户任一结算已在飞(键按人)时直接跳过,不再取数
    p._settling.add("u1")
    asyncio.run(p._settle_and_log("u1", kind="daily"))
    assert len(fetched) == 2


class _RaisingById(dict):
    """message.get_by_id 桩:任何查找抛异常(模拟能力不可用)。"""

    def get(self, key, default=None):
        del key, default
        raise RuntimeError("get_by_id 能力不可用")


class _CountingById(dict):
    """message.get_by_id 桩:记录 get 调用次数(断言同 id 去重只解析一次)。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = 0

    def get(self, key, default=None):
        self.calls += 1
        return super().get(key, default)


def _make_history_plugin(tmp_path, logs, recent, by_id=None):
    """构造 _fetch_recent_for_history 测试用插件实例:真实 config + 收集日志 + capability 桩。"""

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig

    ctx = _StubCtx(tmp_path, recent=recent, by_id=by_id)
    ctx.logger = _CollectLogger(logs)
    p = plugin_mod.CatsitatePlugin()
    p._ctx = ctx
    p._plugin_config_instance = CatsitateConfig()
    p.config.favorability.bot_user_id = "3545773341"
    p._stream_cache = {}
    p._stream_cache_at = 0.0
    return p


def test_fetch_recent_for_history_quote_resolves_sender(tmp_path):
    """结算路径 quote 语义(规格 §3.1):bot 消息 reply 段经 message.get_by_id 解析原发送者,
    与结算目标一致 → addressed=True;解析出他人 → False;用户消息不读 addressed。"""

    logs: list = []
    by_id = {
        "m1": {"message": {"message_info": {"user_info": {"user_id": "u1"}}}},
        "m2": {"message": {"message_info": {"user_info": {"user_id": "u2"}}}},
    }
    recent = [
        {"message_id": "b1", "timestamp": "1755225600.0", "processed_plain_text": "回 u1",
         "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m1"},
        {"message_id": "b2", "timestamp": "1755225660.0", "processed_plain_text": "回 u2",
         "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m2"},
        {"message_id": "u3", "timestamp": "1755225720.0", "processed_plain_text": "普通发言",
         "message_info": {"user_info": {"user_id": "u3"}}, "reply_to": "m1"},
    ]
    p = _make_history_plugin(tmp_path, logs, recent, by_id)
    history = asyncio.run(p._fetch_recent_for_history("g1", 50, "u1"))
    by_seq = {h["seq"]: h for h in history}
    assert by_seq[0]["addressed"] is True   # 解析原发送者 == 结算目标
    assert by_seq[1]["addressed"] is False  # 解析原发送者 != 结算目标
    assert by_seq[2]["addressed"] is None   # 用户消息不读 addressed
    assert not [lv for lv, a, k in logs if lv == "warning"]  # 全部解析成功,无告警


def test_fetch_recent_for_history_quote_resolve_failure_warns_once(tmp_path):
    """解析失败(能力抛异常)→ 该条未 addressed(False)且有 warning,每轮至多一条(不静默)。"""

    logs: list = []
    recent = [
        {"message_id": "b1", "timestamp": "1755225600.0", "processed_plain_text": "回 u1",
         "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m1"},
        {"message_id": "b2", "timestamp": "1755225660.0", "processed_plain_text": "回 u2",
         "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m2"},
    ]
    p = _make_history_plugin(tmp_path, logs, recent, _RaisingById())
    history = asyncio.run(p._fetch_recent_for_history("g1", 50, "u1"))
    by_seq = {h["seq"]: h for h in history}
    assert by_seq[0]["addressed"] is False  # 解析失败按未 quote 命中
    assert by_seq[1]["addressed"] is False
    warns = [(lv, a, k) for lv, a, k in logs if lv == "warning"]
    assert len(warns) == 1  # 每轮至多一条告警
    args = warns[0][1]
    assert "quote 发送者解析失败(stream=%s):%s" in args[0] and args[1] == "g1"


def test_daily_decay_quote_resolve_failure_warns_once(tmp_path):
    """衰减路径告警粒度(修复轮 R1):两条群流各含一条带 reply_to 的 bot 消息,
    get_by_id 桩均抛异常 → _daily_decay 整轮恰一条「quote 发送者解析失败」warning。"""

    import plugin as plugin_mod
    from catsitate_core.favorability import BatchEngine
    from catsitate_core.storage import SQLiteStore

    logs: list = []
    p = _make_history_plugin(tmp_path, logs, [], _RaisingById())
    p.config.plugin.enabled = True
    p.config.favorability.decay_enabled = True
    p.sleep = type("_S", (), {"is_sleeping": staticmethod(lambda: False)})()
    p.store = SQLiteStore(tmp_path / "decay_quote.db")
    p.fav_engine = BatchEngine(p.store, p.config.favorability)
    p.fav_engine.ensure_schema()
    p.fav_engine.apply_delta("u1", 42, "很好", judged_at="2026-08-01T12:00:00")
    p.fav_engine.count_message("u1", "g1")  # batch_counter:u1 活跃于两条群流
    p.fav_engine.count_message("u1", "g2")

    async def _stub_refresh():
        p._stream_cache = {
            "g1": {"session_id": "g1", "is_group_session": True, "user_id": ""},
            "g2": {"session_id": "g2", "is_group_session": True, "user_id": ""},
        }
    p._refresh_stream_cache = _stub_refresh

    recent_by_stream = {
        "g1": [{"message_id": "b1", "timestamp": "1755225600.0", "processed_plain_text": "回 u1",
                "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m1"}],
        "g2": [{"message_id": "b2", "timestamp": "1755225660.0", "processed_plain_text": "回 u1",
                "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m2"}],
    }

    async def _fetch_stub(stream_id, limit):
        del limit
        return list(recent_by_stream[stream_id])

    p._fetch_recent = _fetch_stub

    class _DecayStub:
        async def scan_and_apply(self, candidates, persona=""):
            del candidates, persona
            return []

    p.decay = _DecayStub()

    async def _no_persona():
        return ""
    p._persona = _no_persona

    asyncio.run(p._daily_decay())
    quote_warns = [(lv, a, k) for lv, a, k in logs
                   if lv == "warning" and "quote 发送者解析失败" in a[0]]
    assert len(quote_warns) == 1  # 整轮至多一条(两条群流均解析失败只报一条)


def test_resolve_quote_senders_dedup_and_failure_passthrough(tmp_path):
    """_resolve_quote_senders(修复轮 R1):同 reply_to id 去重只解析一次;
    能力抛异常 → 该 id 记 None 且不抛异常(失败透传)。"""

    logs: list = []
    recent = [
        {"message_id": "b1", "timestamp": "1755225600.0", "processed_plain_text": "x",
         "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m1"},
        {"message_id": "b2", "timestamp": "1755225660.0", "processed_plain_text": "y",
         "message_info": {"user_info": {"user_id": "3545773341"}}, "reply_to": "m1"},
        {"message_id": "u3", "timestamp": "1755225720.0", "processed_plain_text": "用户消息",
         "message_info": {"user_info": {"user_id": "u3"}}, "reply_to": "m1"},
    ]
    by_id = _CountingById({"m1": {"message": {"message_info": {"user_info": {"user_id": "u1"}}}}})
    p = _make_history_plugin(tmp_path, logs, recent, by_id)
    senders, first_err = asyncio.run(p._resolve_quote_senders(recent, "g1"))
    assert senders == {"m1": "u1"} and first_err == ""
    assert by_id.calls == 1  # 两条 bot 消息同 id 去重只解析一次;用户消息 reply_to 不参与
    # 失败透传:能力抛异常 → 该 id 记 None 且不抛异常
    p2 = _make_history_plugin(tmp_path, logs, recent, _RaisingById())
    senders2, first_err2 = asyncio.run(p2._resolve_quote_senders(recent, "g1"))
    assert senders2 == {"m1": None} and "能力调用异常" in first_err2


def test_debug_logging_switch(tmp_path):
    import logging
    import os

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    cfg = CatsitateConfig()
    p._plugin_config_instance = cfg  # SDK 的 config 属性无 setter,走内部实例(与既有测试同款)
    plogger = logging.getLogger("catsitate.core")
    before = len(plogger.handlers)
    p._setup_debug_logging()
    assert p._debug_handler is None  # 默认关闭,不挂 handler
    cfg.debug.enabled = True
    p._setup_debug_logging()
    assert p._debug_handler is not None
    assert any(isinstance(h, logging.FileHandler) and str(h.baseFilename).endswith(".log") for h in plogger.handlers)
    log_file = next(h.baseFilename for h in plogger.handlers if isinstance(h, logging.FileHandler))
    import stat
    assert stat.S_IMODE(os.stat(log_file).st_mode) == 0o600  # 仅属主可读(安全复审)
    assert (tmp_path / "logs").exists()
    cfg.debug.enabled = False
    p._setup_debug_logging()
    assert p._debug_handler is None
    assert len(plogger.handlers) == before  # handler 已移除且关闭,无泄漏


def test_on_unload_removes_debug_handler_and_restores_level(tmp_path):
    """I5:开启 debug 后 on_unload → debug handler 已移除并关闭、logger 级别恢复开启前。"""
    import asyncio
    import logging

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.config.debug.enabled = True
    p._background_tasks = set()
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")  # 醒态 sleep_tick 补注日记读取(on_load 装配,离线手工补)

    async def _stop():
        return None

    p._scheduler = type("_S", (), {"stop": staticmethod(_stop)})()  # staticmethod 避免绑定 self 传参
    p.store = type("_S", (), {"close": staticmethod(lambda: None)})()

    plogger = logging.getLogger("catsitate.core")
    prev_level = plogger.level
    p._setup_debug_logging()
    assert p._debug_handler is not None
    assert plogger.level == logging.DEBUG

    asyncio.run(p.on_unload())
    assert p._debug_handler is None  # handler 已移除并 close
    assert not any(isinstance(h, logging.FileHandler) for h in plogger.handlers)
    assert plogger.level == prev_level  # logger 级别已恢复


def test_sleep_review_report_file_permission(tmp_path):
    """M15:睡醒回顾报告 write_text 后 chmod 0600(报告含消息文本,仅属主可读)。"""
    import asyncio
    import os
    import stat

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.storage import JsonSnapshot

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.memo = type("_M", (), {"due_on": staticmethod(lambda day: [])})()
    p._sleep_review_buffer = [
        {"stream_id": "s1", "user_id": "u1", "nickname": "昵称", "text": "睡觉时发的消息", "ts": "2026-08-16T01:00:00"},
    ]
    p._sleep_review_buffer_snapshot = JsonSnapshot(tmp_path / "sleep_review_buffer.json")

    async def _stub_side_llm(messages, model="", module="", timeout_ms=None):
        del messages, model, module, timeout_ms
        return {"success": True, "response": "摘要", "model": ""}

    p._side_llm_call = _stub_side_llm
    asyncio.run(p._write_sleep_review())
    reports = list((tmp_path / "sleep_review" / "reports").glob("sleep_review_*.md"))
    assert len(reports) == 1
    assert stat.S_IMODE(os.stat(reports[0]).st_mode) == 0o600  # 仅属主可读(安全复审)


def test_memo_write_tool_related_ids_and_speaker_fallback(tmp_path):
    """§3.10 接线:memo_write 解析 related_user_ids(逗号分隔,兼容中文逗号)传附带 QQ;
    群聊 user_id 空时以 fav_count 维护的最近说话人映射兜底为主 QQ。"""
    import asyncio
    import json

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.storage import SQLiteStore

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    p.store = SQLiteStore(tmp_path / "memo_tool.db")
    p.memo = MemoService(p.store, p.config.memo)
    p.memo.ensure_schema()
    p._last_speaker_map["g1"] = "10001"  # fav_count hook 维护的 流→最近真实说话人

    out = asyncio.run(p.memo_write(content="周末一起看电影", stream_id="g1", ttl_hours=None, related_user_ids="10002，10003"))
    assert "已记下" in out
    rows = p.store.query("SELECT user_id, extra_user_ids FROM memo")
    assert rows[0][0] == "10001"  # 说话人映射兜底为主 QQ(群聊 kwargs 无 user_id)
    assert json.loads(rows[0][1]) == ["10002", "10003"]  # 中文逗号分隔正确解析


def test_inject_blocks_memo_single_read_or_semantics(tmp_path):
    """§3.10 注入块:备忘原「流+说话人」两查合并为一次 read(stream_id, speaker)
    (OR 语义单次含两维度),去重与 inject_max 截断保留。"""
    import asyncio

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.storage import SQLiteStore

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    p.config.plugin.enabled = True
    calls = []

    class _Memo:
        def read(self, stream_id, user_id, limit=10):
            calls.append((stream_id, user_id, limit))
            return [
                {"id": 2, "content": "跨流备忘", "remaining_hours": 10.0},
                {"id": 1, "content": "流内备忘", "remaining_hours": 20.0},
            ]

        def due_on(self, day):
            del day
            return []

    p.memo = _Memo()
    # _build_inject_blocks 依赖的实例状态(on_load 才初始化,离线补齐)
    p._snapshot_cache = {}
    p._env_cache = {}
    p._env_fetched_at = None
    p._stream_cache = {}
    p._stream_cache_at = 0.0
    p._schedule_data = {}
    p._schedule_tick_fired = {}
    p._qzone_available = False
    p.fav_engine = BatchEngine(SQLiteStore(tmp_path / "fav.db"), p.config.favorability)
    p.fav_engine.ensure_schema()

    blocks = asyncio.run(p._build_inject_blocks({"session_id": "p1"}))
    # 仅一次查询,说话人取私聊流对端 u1(流缓存桩);limit 随 memo.inject_max 默认 5
    # (批③遗留修正:取数与截断统一按 inject_max,不再硬编码 3)
    assert calls == [("p1", "u1", p.config.memo.inject_max)]
    assert p.config.memo.inject_max == 5  # 防默认值悄悄变化使断言失去意义
    memo_blocks = [b for b in blocks if b.module == "memo"]
    assert len(memo_blocks) == 1
    assert "跨流备忘" in memo_blocks[0].text and "流内备忘" in memo_blocks[0].text


def test_diary_stable_ctx_includes_persona(tmp_path):
    """M3-r2 人设前置:日记 stable_ctx 首段为「bot 人设:{persona}」,模板以
    本人身份口吻书写——日记与表达生成层(qzone_expression)同人设,人设背景
    属稳定段(前置),不混入变量素材尾。"""

    import asyncio

    import plugin as plugin_mod
    from catsitate_core.config import CatsitateConfig
    from catsitate_core.storage import JsonSnapshot

    p = plugin_mod.CatsitatePlugin()
    p._ctx = _StubCtx(tmp_path)
    p._plugin_config_instance = CatsitateConfig()
    captured = {}

    async def side_llm(messages, model, module, timeout_ms=None):
        captured["messages"] = messages
        return {"success": True, "response": "今天很平静。" * 5}

    async def publish(*, content):
        return "tid123"

    p._side_llm_call = side_llm
    p.qzone_client = type("_QC", (), {})()
    p.qzone_client.do_publish = publish
    # 日记方法门控(qzone.enabled+diary_enabled+_qzone_available)与素材依赖,
    # on_load 装配项离线手工补齐
    p.config.qzone.enabled = True
    p.config.qzone.diary_enabled = True
    p._qzone_available = True
    p._persona_cache = "温柔猫娘"  # 人设缓存预置,绕开 config.get 桩(None 走兜底路径)
    p._style_cache = ""
    p._schedule_data = {}
    p._stream_cache = {}  # 聊天时间线素材依赖(on_load 装配,离线手工补;空=素材行省略)
    p._stream_cache_at = 0.0
    p.memo = type("_M", (), {"due_on": staticmethod(lambda day: [])})()
    p.qzone_seen = type("_S", (), {"recent_seen": staticmethod(lambda limit, days, now=None: [])})()
    p._pending_diary_snapshot = JsonSnapshot(tmp_path / "qzone_pending_diary.json")

    asyncio.run(p._generate_and_publish_diary())

    stable = captured["messages"][1]["content"]
    assert stable.startswith("bot 人设:温柔猫娘")  # 人设前置为 stable_ctx 首段
    assert "今天是" in stable  # v5 蓝本:日期入素材(开头必须是日期和天气的依据)
    assert "今天的日程:自由活动" in stable  # 原有当日素材段保留(人设前置不挤掉素材)
    assert captured["messages"][0]["role"] == "system"
    data = p._pending_diary_snapshot.load()  # 发布链走完:tid 随快照透传(Task 4 口径)
    assert data.get("tid") == "tid123"
