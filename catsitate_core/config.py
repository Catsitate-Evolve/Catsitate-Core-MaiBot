"""Catsitate 插件配置模型(与规格 §6 一致)。"""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="插件总开关")
    config_version: str = Field(default="1.0.0", description="配置版本")
    llm_daily_call_warning_threshold: int = Field(default=50, description="旁路 LLM 每日调用告警阈值")


class InjectSection(PluginConfigBase):
    __ui_label__ = "注入框架"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="注入管线总开关(无截断,长度在源头控制)")
    level_rule_enabled: bool = Field(default=True, description="等级规则块注入开关")
    environment_enabled: bool = Field(default=True, description="环境块(节日/天气)注入开关")
    memo_enabled: bool = Field(default=True, description="备忘块注入开关")
    favorability_enabled: bool = Field(default=True, description="好感度块注入开关")


class TimeAwareSection(PluginConfigBase):
    __ui_label__ = "时间感知"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="节日/天气感知开关")
    city: str = Field(default="北京", description="城市名")
    city_lat: float = Field(default=39.9042, description="城市纬度(Open-Meteo)")
    city_lon: float = Field(default=116.4074, description="城市经度(Open-Meteo)")
    weather_refresh_minutes: int = Field(default=45, description="天气后台刷新间隔(分钟)")
    holiday_online: bool = Field(default=True, description="节日数据在线刷新开关")


class FavorabilitySection(PluginConfigBase):
    __ui_label__ = "好感度"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="好感度模块开关")
    window_hours: int = Field(default=24, description="日终结算周期(小时)")
    early_settle_threshold: int = Field(default=20, description="提前结算消息数阈值")
    daily_max_early_settle: int = Field(default=3, description="每用户每日提前结算上限")
    daily_settle_min: int = Field(default=3, description="日终结算最小消息数(不足顺延)")
    level_rules: str = Field(
        default=(
            "与用户的关系分五级:陌生(仅按普通网友对待,保持礼貌与距离)、"
            "熟悉(认识一段时间,可自然闲聊)、亲近(关系较好,可主动关心)、"
            "挚友(非常信任,可分享心事)、特别(最重要的人,格外在意其感受)。"
        ),
        description="5 级行为准则文本(注入等级规则块)",
    )
    note_max_chars: int = Field(default=40, description="关系注记最大字符数(结算落库时强制)")
    material_max_messages: int = Field(default=30, description="结算素材锚定的用户消息条数")
    material_message_max_chars: int = Field(default=200, description="单条素材截断长度")
    llm_model: str = Field(default="", description="旁路模型:填主程序 model_task_config 的 task 名(推荐为插件配专用 task,如 catsitate);留空=主程序默认(首个可用 task)")


class MemoSection(PluginConfigBase):
    __ui_label__ = "备忘录"
    __ui_order__ = 4

    enabled: bool = Field(default=True, description="备忘录模块开关")
    tool_enabled: bool = Field(default=True, description="memo_write/memo_read 工具开关")
    command_enabled: bool = Field(default=True, description="/记一下 命令开关")
    default_ttl_hours: int = Field(default=24, description="单条备忘缺省有效期(小时)")
    max_ttl_hours: int = Field(default=168, description="单条备忘有效期上限(小时)")
    entry_max_chars: int = Field(default=80, description="备忘内容最大字符数(写入时强制)")
    inject_max: int = Field(default=5, description="备忘注入合计条数上限")


class MsgReactSection(PluginConfigBase):
    __ui_label__ = "贴表情"
    __ui_order__ = 5

    enabled: bool = Field(default=True, description="贴表情工具开关")
    emoji_whitelist: list[str] = Field(default_factory=list, description="表情包白名单(emoji_id)")
    per_stream_cooldown_seconds: int = Field(default=30, description="每流冷却秒数")
    llm_model: str = Field(default="", description="旁路模型:填主程序 model_task_config 的 task 名(推荐为插件配专用 task,如 catsitate);留空=主程序默认(首个可用 task)")


class PokeSection(PluginConfigBase):
    __ui_label__ = "戳一戳"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="戳一戳模块开关")
    enhance_notice_text: bool = Field(default=True, description="改写通知消息为拟人文本")
    inject_to_context: bool = Field(default=True, description="戳一戳事件注入当前流上下文")
    poke_tool_enabled: bool = Field(default=True, description="主动戳工具开关")
    min_level_for_poke: str = Field(default="熟悉", description="主动戳最低好感度等级")
    cooldown_seconds: int = Field(default=600, description="主动戳每用户冷却秒数")


class ReplyGuardSection(PluginConfigBase):
    __ui_label__ = "reply 补传"
    __ui_order__ = 7

    enabled: bool = Field(default=True, description="reply_guard 模块开关")
    context_backfill_enabled: bool = Field(default=True, description="上下文补传开关")
    context_tools: list[str] = Field(
        default_factory=lambda: ["query_memory", "query_person_profile", "fetch_history", "view_forward_message", "memo_read"],
        description="视为上下文工具的工具名列表",
    )
    sentinel_enabled: bool = Field(default=False, description="LLM 哨兵层开关(默认关)")
    sentinel_model: str = Field(default="", description="哨兵旁路模型:填主程序 task 名(推荐专用 catsitate task);留空=主程序默认")


class ImageRelookSection(PluginConfigBase):
    __ui_label__ = "图片重看"
    __ui_order__ = 8

    enabled: bool = Field(default=True, description="图片重看工具开关")
    llm_model: str = Field(default="", description="旁路模型:填主程序 model_task_config 的 task 名(推荐为插件配专用 task,如 catsitate);留空=主程序默认(首个可用 task)")


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
