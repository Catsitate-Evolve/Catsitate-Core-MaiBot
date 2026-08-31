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
    level_rule_enabled: bool = _f(True, "好感度块内按等级注入规则开关", label="等级规则注入")
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

    def level_rules_list(self) -> list[str]:
        """5 级规则按等级序列化为「等级名(规则)」行列表(结算判定全表用)。"""

        return [
            f"陌生({self.level_rule_stranger})",
            f"熟悉({self.level_rule_familiar})",
            f"亲近({self.level_rule_close})",
            f"挚友({self.level_rule_best_friend})",
            f"特别({self.level_rule_special})",
        ]

    def level_rule_by_name(self, name: str) -> str:
        """按等级名取单条规则(注入用)。"""

        return {
            "陌生": self.level_rule_stranger,
            "熟悉": self.level_rule_familiar,
            "亲近": self.level_rule_close,
            "挚友": self.level_rule_best_friend,
            "特别": self.level_rule_special,
        }.get(name, "")


    enabled: bool = _f(True, "好感度模块开关", label="好感度模块开关")
    window_hours: int = _f(24, "日终结算周期(小时)", label="日终结算周期(小时)")
    early_settle_threshold: int = _f(20, "提前结算消息数阈值", label="提前结算消息数阈值")
    daily_max_early_settle: int = _f(3, "每用户每日提前结算上限", label="每日提前结算上限")
    daily_settle_min: int = _f(3, "日终结算最小消息数(不足顺延)", label="日终结算最小消息数")
    delta_max: int = _f(5, "单次结算好感度变化上限(±,判定结果钳制在此范围内)", label="单次变化上限")
    decay_enabled: bool = _f(True, "好感度自然衰减开关", label="自然衰减开关")
    decay_after_days: int = _f(7, "未互动 N 天后开始衰减", label="衰减触发天数")
    decay_max: int = _f(3, "单次衰减幅度上限(-decay_max 到 0)", label="单次衰减上限")
    decay_llm_model: str = _f("memory", "衰减判定模型:填主程序 task 名", label="衰减模型(task 名)")
    decay_llm_timeout_ms: int | None = _f(0, "衰减判定 LLM 超时(毫秒);0=主程序默认(30s)", label="衰减调用超时(毫秒)")
    level_rule_stranger: str = _f("仅按普通网友对待,保持礼貌与距离", "陌生级行为准则", label="陌生级规则")
    level_rule_familiar: str = _f("认识一段时间,可自然闲聊", "熟悉级行为准则", label="熟悉级规则")
    level_rule_close: str = _f("关系较好,可主动关心", "亲近级行为准则", label="亲近级规则")
    level_rule_best_friend: str = _f("非常信任,可分享心事", "挚友级行为准则", label="挚友级规则")
    level_rule_special: str = _f("最重要的人,格外在意其感受", "特别级行为准则", label="特别级规则")
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
        0,
        "好感度判定 LLM 调用超时(毫秒);0=主程序默认(30s);慢模型建议 120000",
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
        0,
        "选表情 LLM 调用超时(毫秒);0=主程序默认(30s)",
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
    sentinel_enabled: bool = _f(False, "LLM 哨兵层开关(默认关)", label="LLM 哨兵层开关")
    sentinel_model: str = _f(
        "planner",
        "哨兵旁路模型:填主程序 task 名(默认 planner;可自定义)",
        label="哨兵模型(task 名)",
    )
    sentinel_timeout_ms: int | None = _f(
        0,
        "哨兵判定 LLM 调用超时(毫秒);0=主程序默认(30s)",
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
        0,
        "图片重看 LLM 调用超时(毫秒);0=主程序默认(30s);VLM 较慢建议 120000",
        label="重看调用超时(毫秒)",
    )


class SleepSection(PluginConfigBase):
    __ui_label__ = "睡眠"
    __ui_order__ = 9

    enabled: bool = _f(True, "睡眠模块开关", label="睡眠模块开关")
    min_sleep_minutes: int = _f(240, "最短睡眠分钟(不足顺延醒来)", label="最短睡眠分钟")
    max_sleep_minutes: int = _f(660, "最长睡眠分钟(超过提前醒)", label="最长睡眠分钟")
    silent_sleep_enabled: bool = _f(True, "静默入睡开关(睡眠窗口内生效:关=窗口起点直接入睡,开=安静满 N 分钟入睡)", label="静默入睡开关")
    silent_sleep_minutes: int = _f(60, "静默入睡:无消息满 N 分钟", label="静默入睡分钟")
    review_enabled: bool = _f(True, "睡醒回顾开关(醒来生成聚合报告文件)", label="睡醒回顾开关")
    review_llm_model: str = _f("memory", "回顾总结模型:填主程序 task 名", label="回顾模型(task 名)")
    review_llm_timeout_ms: int | None = _f(0, "回顾 LLM 调用超时(毫秒);0=主程序默认", label="回顾调用超时(毫秒)")


class ScheduleSection(PluginConfigBase):
    __ui_label__ = "日程"
    __ui_order__ = 10

    enabled: bool = _f(True, "日程模块开关", label="日程模块开关")
    max_regenerate: int = _f(1, "生成校验失败重生成次数", label="重生成次数")
    speak_threshold_level: str = _f("熟悉", "日常发言最低好感度等级(陌生/熟悉/亲近/挚友/特别)", label="日常发言等级门槛")
    speak_max_streams_per_window: int = _f(1, "每窗口最多主动触发流数(按等级+活跃度排序取前 n)", label="每窗口触发流数上限")
    schedule_llm_model: str = _f("memory", "日程生成模型:填主程序 task 名", label="日程生成模型(task 名)")
    schedule_llm_timeout_ms: int | None = _f(0, "日程生成 LLM 超时(毫秒);0=主程序默认", label="日程生成超时(毫秒)")
    daily_speak_limit: int = _f(5, "全天主动发言次数上限(每次发言计 1)", label="每日发言上限")


class QzoneSection(PluginConfigBase):
    __ui_label__ = "QQ空间"
    __ui_order__ = 11

    enabled: bool = _f(True, "QQ空间模块开关(M1:仅读动态,不发评论)", label="QQ空间模块开关")
    poll_interval_minutes: int = _f(15, "空间窗口内动态拉取间隔(分钟)", label="拉取间隔(分钟)")
    decision_window_seconds: int = _f(75, "注入后等待 planner 轮完成的超时兜底(秒;须大于最坏轮延迟)", label="决策窗口超时(秒)")
    tool_whitelist: list[str] = _f(
        ["wait", "reply", "query_memory", "query_person_profile", "memo_write", "memo_read", "inspect_image"],
        "虚拟流 planner 工具白名单(按名过滤;硬门控不随此配置放松)",
        label="虚拟流工具白名单",
    )
    virtual_group_id: str = _f("qzone_feed", "虚拟群聊流伪群号(勿与真实群号相同)", label="虚拟伪群号")
    virtual_group_name: str = _f("QQ空间", "虚拟群聊流显示名", label="虚拟流显示名")
    summary_count: int = _f(5, "真实聊天注入的近期已见动态条数", label="见闻摘要条数")
    summary_days: int = _f(3, "见闻摘要回溯天数", label="见闻回溯天数")
    request_timeout_ms: int = _f(10000, "空间 HTTP 请求超时(毫秒)", label="HTTP 超时(毫秒)")
    max_retries: int = _f(
        0,
        "空间动作 API(评论/点赞/发布,M2 生效)失败重试次数;0=失败即告警跳过。M1 读路径(图片下载)固定单次重试(联调实证 CDN 瞬态 404),不受此配置影响",
        label="失败重试次数",
    )
    cookie_refresh_minutes: int = _f(60, "cookie 刷新节流(分钟,间隔内跳过重取)", label="cookie 刷新节流(分钟)")


class DebugSection(PluginConfigBase):
    __ui_label__ = "调试"
    __ui_order__ = 99

    enabled: bool = _f(
        False,
        "debug 日志开关:开启后 debug 级日志写入插件数据目录 logs/ 下的当日文件(供公测复审)",
        label="debug 日志开关",
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
    sleep: SleepSection = Field(default_factory=SleepSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    qzone: QzoneSection = Field(default_factory=QzoneSection)
    debug: DebugSection = Field(default_factory=DebugSection)
