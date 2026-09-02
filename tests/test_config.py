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
    # RPC 死线默认 120s(2026-09-02 定位:不设时 RPC 层固定 30s,memory 模型跑
    # 日程大 prompt 必超 → 每晚 RPCError 走模板兜底)
    assert cfg.schedule.schedule_llm_timeout_ms == 120000
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
    # 深度审查 F:慢模型实测轮延迟 31-53s,75s 无余量——默认 150 留余量
    assert q.decision_window_seconds == 150
    # 2026-09-02:伪群号/显示名固化为常量(不可配置),配置节不再有这两字段
    assert not hasattr(q, "virtual_group_id") and not hasattr(q, "virtual_group_name")
    from catsitate_core.qzone import QZONE_VIRTUAL_GROUP_ID, QZONE_VIRTUAL_GROUP_NAME
    assert QZONE_VIRTUAL_GROUP_ID == "qzone_feed" and QZONE_VIRTUAL_GROUP_NAME == "QQ空间"
    assert q.summary_count == 5
    assert q.summary_days == 3
    # M3-r2 Task5:发现层翻页与拉取数量(源A自扫/源B单页与浏览流同口径)
    assert q.discovery_count == 50
    assert q.discovery_max_pages == 3
    assert q.own_feed_scan_count == 20
    assert q.request_timeout_ms == 10000
    assert q.max_retries == 0
    assert q.cookie_refresh_minutes == 60
    # 2026-09-02 全域化:qzone_* 工具不再由白名单管理(默认可用不可剔除),
    # 白名单只管其余虚拟流工具;view_friend_feeds 仍在(虚拟流查看好友说说)
    assert "wait" in q.tool_whitelist and "reply" not in q.tool_whitelist
    assert not any(t.startswith("qzone_") for t in q.tool_whitelist)
    assert "view_friend_feeds" in q.tool_whitelist
    assert q.tool_whitelist.index("view_friend_feeds") == q.tool_whitelist.index("inspect_image") + 1
    assert "tool_search" not in q.tool_whitelist and "msg_react" not in q.tool_whitelist
    # M2 评论轮询两字段(spec §5;间隔字段 T11 起废弃,由 notification_interval_seconds 替代)
    assert q.comment_poll_enabled is True
    assert q.comment_poll_interval_minutes == 30
    # M2.1 统一通知轮询间隔(T11:高频短间隔模拟推送,注册时下限 30s)
    assert q.notification_interval_seconds == 120
    # M3 表达:日记三字段(入睡任务生成并发布空间日记说说)
    assert q.diary_enabled is True
    assert q.diary_llm_model == "memory"
    assert q.diary_llm_timeout_ms == 0


def test_qzone_constants():
    from catsitate_core.qzone import QZONE_GATEWAY_NAME, QZONE_PLATFORM

    assert QZONE_PLATFORM == "qzone-qq"
    assert QZONE_GATEWAY_NAME == "catsitate_qzone"
