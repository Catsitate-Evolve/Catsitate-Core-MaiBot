"""配置模型默认值测试(与规格 §6 一致)。"""

from catsitate_core.config import CatsitateConfig


def test_config_defaults():
    cfg = CatsitateConfig()
    assert cfg.plugin.enabled is False
    assert cfg.plugin.llm_daily_call_warning_threshold == 50
    assert cfg.favorability.llm_timeout_ms is None
    assert cfg.reply_guard.sentinel_timeout_ms is None
    assert cfg.inject.enabled is True
    assert cfg.time_aware.city == "珠海"
    assert cfg.time_aware.weather_refresh_minutes == 45
    assert cfg.favorability.window_hours == 24
    assert cfg.favorability.early_settle_threshold == 20
    assert cfg.favorability.daily_max_early_settle == 3
    assert cfg.favorability.daily_settle_min == 3
    assert cfg.favorability.note_max_chars == 40
    assert cfg.favorability.material_max_messages == 30
    assert cfg.favorability.material_message_max_chars == 200
    assert cfg.favorability.llm_model == "memory"
    assert cfg.memo.default_ttl_hours == 24
    assert cfg.memo.max_ttl_hours == 168
    assert cfg.memo.entry_max_chars == 80
    assert cfg.memo.inject_max == 5
    assert cfg.msg_react.per_stream_cooldown_seconds == 30
    assert cfg.poke.cooldown_seconds == 600
    assert cfg.reply_guard.sentinel_enabled is False
    from catsitate_core.reply_guard import CONTEXT_TOOLS
    assert "memo_read" in CONTEXT_TOOLS


def test_default_config_dump():
    cfg = CatsitateConfig()
    data = cfg.model_dump(mode="json")
    assert data["plugin"]["config_version"] == "1.0.0"
    assert data["favorability"]["level_rule_familiar"] == "认识一段时间,可自然闲聊"
    assert len(data["favorability"]) >= 5


def test_phase2_sections_defaults():
    cfg = CatsitateConfig()
    assert cfg.sleep.enabled is True
    assert cfg.sleep.min_sleep_minutes == 240
    assert cfg.sleep.max_sleep_minutes == 660
    assert cfg.sleep.silent_sleep_enabled is True
    assert cfg.sleep.silent_sleep_minutes == 60
    assert cfg.sleep.review_enabled is True
    assert cfg.schedule.enabled is True
    assert cfg.schedule.max_regenerate == 1
    assert cfg.schedule.speak_threshold_level == "熟悉"
    assert cfg.schedule.speak_max_streams_per_window == 1
    assert cfg.schedule.daily_speak_limit == 5
    assert cfg.favorability.decay_enabled is True
    assert cfg.favorability.decay_after_days == 7
    assert cfg.favorability.decay_max == 3
    assert cfg.favorability.decay_llm_model == "memory"
    assert cfg.favorability.decay_llm_timeout_ms is None
