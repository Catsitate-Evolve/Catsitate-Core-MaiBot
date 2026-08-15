"""Catsitate 插件配置模型(与规格 §6 一致;字段中文 label 供 WebUI 展示)。"""

from maibot_sdk import Field, PluginConfigBase


def _f(default, description, *, label="", **extra):
    """构造带中文 label 的字段:label 缺省用 description 首段(冒号前)或 description 本身。"""

    if not label:
        label = description.split(":", 1)[0].split("(", 1)[0].strip()
    return Field(default=default, description=description, json_schema_extra={"label": label, **extra})


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = _f(False, "插件总开关", label="插件总开关")
    config_version: str = _f("1.0.0", "配置版本", label="配置版本")
    llm_daily_call_warning_threshold: int = _f(50, "旁路 LLM 每日调用告警阈值", label="旁路 LLM 每日告警阈值")


class InjectSection(PluginConfigBase):
    __ui_label__ = "注入框架"
    __ui_order__ = 1

    enabled: bool = _f(True, "注入管线总开关(无截断,长度在源头控制)", label="注入管线总开关")
    level_rule_enabled: bool = _f(True, "等级规则块注入开关", label="等级规则块注入")
    environment_enabled: bool = _f(True, "环境块(节日/天气)注入开关", label="环境块注入")
    memo_enabled: bool = _f(True, "备忘块注入开关", label="备忘块注入")
    favorability_enabled: bool = _f(True, "好感度块注入开关", label="好感度块注入")


class TimeAwareSection(PluginConfigBase):
    __ui_label__ = "时间感知"
    __ui_order__ = 2

    enabled: bool = _f(True, "节日/天气感知开关", label="时间感知开关")
    city: str = _f("珠海", "城市名", label="城市名")
    city_lat: float = _f(22.279410, "城市纬度(Open-Meteo)", label="城市纬度")
    city_lon: float = _f(113.528098, "城市经度(Open-Meteo)", label="城市经度")
    weather_refresh_minutes: int = _f(45, "天气后台刷新间隔(分钟)", label="天气刷新间隔(分钟)")
    holiday_online: bool = _f(True, "节日数据在线刷新开关", label="节日在线刷新")


class FavorabilitySection(PluginConfigBase):
    __ui_label__ = "好感度"
    __ui_order__ = 3

    enabled: bool = _f(True, "好感度模块开关", label="好感度模块开关")
    window_hours: int = _f(24, "日终结算周期(小时)", label="日终结算周期(小时)")
    early_settle_threshold: int = _f(20, "提前结算消息数阈值", label="提前结算消息数阈值")
    daily_max_early_settle: int = _f(3, "每用户每日提前结算上限", label="每日提前结算上限")
    daily_settle_min: int = _f(3, "日终结算最小消息数(不足顺延)", label="日终结算最小消息数")
    level_rules: str = _f(
        "与用户的关系分五级:陌生(仅按普通网友对待,保持礼貌与距离)、"
        "熟悉(认识一段时间,可自然闲聊)、亲近(关系较好,可主动关心)、"
        "挚友(非常信任,可分享心事)、特别(最重要的人,格外在意其感受)。",
        "5 级行为准则文本(注入等级规则块)", label="5 级行为准则",
        rows=8,
    )
    note_max_chars: int = _f(40, "关系注记最大字符数(结算落库时强制)", label="关系注记最大字符数")
    material_max_messages: int = _f(30, "结算素材锚定的用户消息条数", label="素材锚定消息条数")
    material_message_max_chars: int = _f(200, "单条素材截断长度", label="单条素材截断长度")
    bot_user_id: str = _f(
        "",
        "bot 自身账号 id(实机确认 napcat 账号,如 3545773341);结算素材中该 id 发言标记为 bot 随附;留空=不启用识别",
        label="bot 账号 id",
    )
    llm_model: str = _f(
        "memory",
        "旁路模型:填主程序 model_task_config 的 task 名(默认 memory;可自定义填 planner/replyer 等任意已配置 task)",
        label="判定模型(task 名)",
    )
    llm_timeout_ms: int | None = _f(
        None,
        "好感度判定 LLM 调用超时(毫秒);留空=主程序默认(30s);慢模型建议 120000",
        label="判定调用超时(毫秒)",
    )


class MemoSection(PluginConfigBase):
    __ui_label__ = "备忘录"
    __ui_order__ = 4

    enabled: bool = _f(True, "备忘录模块开关", label="备忘录模块开关")
    tool_enabled: bool = _f(True, "memo_write/memo_read 工具开关", label="备忘录工具开关")
    command_enabled: bool = _f(True, "/记一下 命令开关", label="命令开关")
    default_ttl_hours: int = _f(24, "单条备忘缺省有效期(小时)", label="缺省有效期(小时)")
    max_ttl_hours: int = _f(168, "单条备忘有效期上限(小时)", label="有效期上限(小时)")
    entry_max_chars: int = _f(80, "备忘内容最大字符数(写入时强制)", label="内容最大字符数")
    inject_max: int = _f(5, "备忘注入合计条数上限", label="注入合计条数上限")


class MsgReactSection(PluginConfigBase):
    __ui_label__ = "贴表情"
    __ui_order__ = 5

    enabled: bool = _f(True, "贴表情工具开关", label="贴表情工具开关")
    per_stream_cooldown_seconds: int = _f(30, "每流冷却秒数", label="每流冷却秒数")
    llm_model: str = _f(
        "replyer",
        "旁路模型:填主程序 model_task_config 的 task 名(默认 replyer;可自定义填 planner/memory 等任意已配置 task)",
        label="选表情模型(task 名)",
    )
    llm_timeout_ms: int | None = _f(
        None,
        "选表情 LLM 调用超时(毫秒);留空=主程序默认(30s)",
        label="选表情调用超时(毫秒)",
    )


class PokeSection(PluginConfigBase):
    __ui_label__ = "戳一戳"
    __ui_order__ = 6

    enabled: bool = _f(True, "戳一戳模块开关", label="戳一戳模块开关")
    poke_tool_enabled: bool = _f(True, "主动戳工具开关", label="主动戳工具开关")
    cooldown_seconds: int = _f(600, "主动戳每用户冷却秒数", label="主动戳冷却秒数")


class ReplyGuardSection(PluginConfigBase):
    __ui_label__ = "reply 补传"
    __ui_order__ = 7

    enabled: bool = _f(True, "reply_guard 模块开关", label="reply 补传模块开关")
    context_backfill_enabled: bool = _f(True, "上下文补传开关", label="上下文补传开关")
    context_tools: list[str] = _f(
        ["query_memory", "query_person_profile", "fetch_history", "view_forward_message", "memo_read"],
        "视为上下文工具的工具名列表", label="上下文工具列表",
    )
    sentinel_enabled: bool = _f(False, "LLM 哨兵层开关(默认关)", label="LLM 哨兵层开关")
    sentinel_model: str = _f(
        "planner",
        "哨兵旁路模型:填主程序 task 名(默认 planner;可自定义)",
        label="哨兵模型(task 名)",
    )
    sentinel_timeout_ms: int | None = _f(
        None,
        "哨兵判定 LLM 调用超时(毫秒);留空=主程序默认(30s)",
        label="哨兵调用超时(毫秒)",
    )


class ImageRelookSection(PluginConfigBase):
    __ui_label__ = "图片重看"
    __ui_order__ = 8

    enabled: bool = _f(True, "图片重看工具开关", label="图片重看工具开关")
    llm_model: str = _f(
        "utils",
        "旁路模型:填主程序 model_task_config 的 task 名(默认 utils 轻量任务;可自定义填 planner/memory 等任意已配置 task)",
        label="重看模型(task 名)",
    )
    llm_timeout_ms: int | None = _f(
        None,
        "图片重看 LLM 调用超时(毫秒);留空=主程序默认(30s);VLM 较慢建议 120000",
        label="重看调用超时(毫秒)",
    )


class CatsitateConfig(PluginConfigBase):
    """Catsitate 插件顶层配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    inject: InjectSection = Field(default_factory=InjectSection)
    time_aware: TimeAwareSection = Field(default_factory=TimeAwareSection)
    favorability: FavorabilitySection = Field(default_factory=FavorabilitySection)
    memo: MemoSection = Field(default_factory=MemoSection)
    msg_react: MsgReactSection = Field(default_factory=MsgReactSection)
    poke: PokeSection = Field(default_factory=PokeSection)
    reply_guard: ReplyGuardSection = Field(default_factory=ReplyGuardSection)
    image_relook: ImageRelookSection = Field(default_factory=ImageRelookSection)
