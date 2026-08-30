"""配置模型默认值测试(与规格 §6 一致)。"""

from catsitate_core.config import CatsitateConfig


def test_config_defaults():
    cfg = CatsitateConfig()
    assert cfg.plugin.enabled is False
    assert cfg.plugin.llm_daily_call_warning_threshold == 50
    assert cfg.favorability.llm_timeout_ms == 0  # 0=主程序默认超时(默认值不得为 None:主机 tomlkit 回写会崩溃)
    assert cfg.reply_guard.sentinel_timeout_ms == 0
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
    assert cfg.favorability.decay_llm_timeout_ms == 0


def test_default_config_has_no_none_values():
    """回归守卫:默认配置不得含 None——主机用 tomlkit 回写配置,None 会致插件激活失败(公测发现)。"""

    from maibot_sdk.config import build_plugin_default_config

    defaults = build_plugin_default_config(CatsitateConfig)

    def walk(d: dict, path: str = "") -> list[str]:
        found = []
        for k, v in d.items():
            p = f"{path}.{k}"
            if isinstance(v, dict):
                found += walk(v, p)
            elif v is None:
                found.append(p)
        return found

    assert walk(defaults) == []


def test_qzone_section_defaults():
    from catsitate_core.config import CatsitateConfig

    cfg = CatsitateConfig()
    q = cfg.qzone
    assert q.enabled is True
    assert q.poll_interval_minutes == 15
    assert q.decision_window_seconds == 75
    assert q.image_max_kb == 3072
    assert q.virtual_group_id == "qzone_feed"
    assert q.virtual_group_name == "QQ空间"
    assert q.summary_count == 5
    assert q.summary_days == 3
    assert q.request_timeout_ms == 10000
    assert q.max_retries == 0
    assert q.cookie_refresh_minutes == 60
    assert "wait" in q.tool_whitelist and "reply" in q.tool_whitelist
    assert "tool_search" not in q.tool_whitelist and "msg_react" not in q.tool_whitelist


def test_qzone_constants():
    from catsitate_core.qzone import QZONE_GATEWAY_NAME, QZONE_PLATFORM

    assert QZONE_PLATFORM == "qzone-qq"
    assert QZONE_GATEWAY_NAME == "catsitate_qzone"
