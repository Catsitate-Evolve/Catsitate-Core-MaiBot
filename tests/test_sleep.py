"""睡眠状态机测试:入睡/唤醒/clamp/持久化。"""
from datetime import datetime
from catsitate_core.config import SleepSection
from catsitate_core.sleep import SleepManager
from catsitate_core.storage import JsonSnapshot
from catsitate_core.sleep import is_goodnight_utterance, parse_sleep_confirm_response

NOW = datetime(2026, 8, 15, 23, 30, 0)


def make_mgr(tmp_path):
    return SleepManager(JsonSnapshot(tmp_path / "sleep_state.json"), SleepSection())


def test_clamp_wake_normal_keeps_planned(tmp_path):
    mgr = make_mgr(tmp_path)
    # 计划 23:00 睡 07:00 起(8h=480min,在 [240,660] 内)
    assert mgr.clamp_wake_time("2026-08-15T23:00:00", "2026-08-16T07:00:00") == "2026-08-16T07:00:00"


def test_clamp_wake_early_sleep_keeps_planned(tmp_path):
    mgr = make_mgr(tmp_path)
    # 提前 2 小时睡,醒来时间不变(拟人)
    assert mgr.clamp_wake_time("2026-08-15T21:00:00", "2026-08-16T07:00:00") == "2026-08-16T07:00:00"


def test_clamp_wake_too_long_forces_earlier(tmp_path):
    mgr = make_mgr(tmp_path)
    # 睡 12h > max 660min,提前醒
    # 20:00 睡到 07:00 = 11h=660min 恰好等于 max 边界 → 醒来不变
    assert mgr.clamp_wake_time("2026-08-15T20:00:00", "2026-08-16T07:00:00") == "2026-08-16T07:00:00"
    # 真正超界:19:00 睡 → 12h > 660 → 提前到 19:00+660min=06:00
    assert mgr.clamp_wake_time("2026-08-15T19:00:00", "2026-08-16T07:00:00") == "2026-08-16T06:00:00"


def test_clamp_wake_too_short_extends(tmp_path):
    mgr = make_mgr(tmp_path)
    # 计划 06:00 睡 07:00 起(1h < 240min)→ 顺延到 06:00+240min=10:00
    assert mgr.clamp_wake_time("2026-08-16T06:00:00", "2026-08-16T07:00:00") == "2026-08-16T10:00:00"


def test_enter_and_persist(tmp_path):
    mgr = make_mgr(tmp_path)
    assert mgr.is_sleeping(now=lambda: NOW) is False
    mgr.enter_sleep(now=lambda: NOW, wake_at="2026-08-16T07:00:00")
    assert mgr.is_sleeping(now=lambda: NOW) is True
    mgr2 = make_mgr(tmp_path)  # 重新加载(持久化恢复)
    assert mgr2.is_sleeping(now=lambda: NOW) is True
    mgr2.wake(now=lambda: datetime(2026, 8, 16, 7, 1, 0))
    assert mgr2.is_sleeping(now=lambda: datetime(2026, 8, 16, 7, 1, 0)) is False


def test_parse_sleep_confirm():
    assert parse_sleep_confirm_response('{"result": "SLEEP"}')[0] == "SLEEP"
    assert parse_sleep_confirm_response('{"result": "UNSURE"}')[0] == "UNSURE"
    assert parse_sleep_confirm_response("不知道")[0] is None
    assert parse_sleep_confirm_response('{"result": "WEIRD"}')[0] is None


def test_goodnight_filter():
    assert is_goodnight_utterance("我睡了") is True
    assert is_goodnight_utterance("晚安") is True
    assert is_goodnight_utterance("该睡了,大家晚安") is True
    assert is_goodnight_utterance("@Hesitate_P 晚安") is False  # @ 不触发
    assert is_goodnight_utterance("晚安,小明") is False  # 称呼他人不触发
    assert is_goodnight_utterance("我今天睡了八个小时好舒服啊睡得真好") is False  # 超短句上限
    assert is_goodnight_utterance("吃饭了") is False  # 无睡眠关键词
