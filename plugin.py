"""Catsitate 核心插件入口:薄接线层,业务逻辑全部在 catsitate_core 包内。

规格:docs/superpowers/specs/2026-08-14-catsitate-core-maibot-design.md
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncio
import hashlib
import json
import logging
import os
import sys
import time

import httpx
from maibot_sdk import Command, HookHandler, MaiBotPlugin, MessageGateway, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo

# spike ① 实测结论:加载器仅将 plugins 父目录临时加入 sys.path,插件目录本身不在,
# 绝对导入 catsitate_core.* 会失败。在此自行注册插件目录(sys.path 修改限于插件进程内)。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from catsitate_core.config import CatsitateConfig
from catsitate_core.favorability import LEVELS, LEVEL_INDEX, EXCLUSIVE_LEVEL, BatchEngine, SettleExecutor, build_favorability_block
from catsitate_core.image_relook import build_relook_prompt, find_image_segment
from catsitate_core.inject import InjectAssembler, InjectionBlock
from catsitate_core.llm_provider import build_side_prompt
from catsitate_core.memo import MemoService, validate_remind_at
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.poke import PokeEngine
from catsitate_core.prompt_deploy import sync_prompt_templates
from catsitate_core.qzone import QZONE_GATEWAY_NAME, QZONE_PLATFORM
from catsitate_core.qzone.protocol import FeedItem, parse_friend_list
from catsitate_core.qzone.client import CookieManager, QzoneAuthError, QzoneClient
from catsitate_core.qzone.comment_seen import CommentSeenStore
from catsitate_core.qzone.discovery import FeedDiscovery
from catsitate_core.qzone.injector import FeedInjector
from catsitate_core.qzone.messages import (
    build_feed_message,
    build_notify_message,
    fit_images_to_rpc_budget,
    format_comment_param_line,
)
from catsitate_core.qzone.registry import FeedContext, FeedContextRegistry
from catsitate_core.qzone.scene import (
    SCENE_EMPTY_CONFIG_WARNING, SCENE_MISS_WARNING,
    apply_scene_surgery, filter_qzone_tools_for_stream, is_qzone_message,
)
from catsitate_core.qzone.seen_store import SeenStore
from catsitate_core.qzone.wire import parse_feed_replies, parse_qzone_mentions
from catsitate_core.reply_guard import (
    CONTEXT_TOOLS,
    backfill_reply_items,
    build_sentinel_prompt,
    parse_sentinel_response,
)
from catsitate_core.decay import last_bot_interaction_time
from catsitate_core.schedule import (
    DEFAULT_TEMPLATE_SCHEDULE,
    _materialize_template,
    apply_schedule_add,
    apply_schedule_edit,
    apply_schedule_move,
    build_proactive_intent,
    current_window,
    next_window,
    schedule_overview_text,
    sort_windows,
    threshold_met,
)
from catsitate_core.services.scheduler import Scheduler
from catsitate_core.sleep import is_goodnight_utterance, parse_sleep_confirm_response
from catsitate_core.storage import JsonSnapshot, SQLiteStore
from catsitate_core.time_aware import (
    build_environment_text,
    dedup_festival_names,
    holiday_chain,
    lunar_festivals_near,
    lunar_festivals_upcoming,
    parse_holiday_cn,
    solar_terms_near,
)

logger = logging.getLogger("catsitate.core")

SNAPSHOT_CACHE_MAX = 256  # 快照项缓存条数上限(背包 M-1,超限 LRU 逐最旧)

# 空间互动事件 kind → 结算素材标签(spec §3.9;未知 kind 兜底「空间互动」)
QZONE_FAV_EVENT_LABELS = {
    "COMMENT": "评论了你的说说",
    "OUT_COMMENT": "你评论了TA",
    "OUT_LIKE": "你点赞了TA",
}

# 通知注入重试上限(深度审查 A-N1):同一通知被宿主拒绝/注入异常后经软回退
# 重发现的次数上限,超过则保留登记放弃——防宿主持续拒绝时每轮询周期无限重注入
QZONE_NOTIFY_MAX_RETRIES = 3


class _ModuleLogForwarder(logging.Handler):
    """把 catsitate_core.* 模块日志转发到插件 ctx logger(联调缺陷#10)。

    插件 runner 只路由插件自身 logger,模块级 getLogger(__name__) 的告警
    (如 qzone client 的下载失败)原本不可见——违反错误显式暴露。
    """

    def __init__(self, sink) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败不得打断日志链
            message = record.msg
        if record.levelno >= logging.ERROR:
            self._sink.error("%s: %s", record.name, message)
        elif record.levelno >= logging.WARNING:
            self._sink.warning("%s: %s", record.name, message)
        else:
            self._sink.info("%s: %s", record.name, message)


class CatsitatePlugin(MaiBotPlugin):
    """Catsitate 猫耳少女核心插件。"""

    config_model = CatsitateConfig
    config_reload_subscriptions = ("bot",)

    _persona_cache: str | None = None  # bot 人设缓存(config.get 一次,bot 配置变更时失效)
    _style_cache: str | None = None  # bot 行为风格缓存(同上)
    _debug_handler: logging.Handler | None = None  # debug 日志文件 handler(配置开关控制)
    _debug_prev_level: int = logging.NOTSET  # 开启前 logger 级别(关闭时恢复)
    _module_log_forwarder: logging.Handler | None = None  # 模块日志转发(on_load 挂载,unload 清理)
    _llm_warned_day: str = ""  # 旁路 LLM 用量告警的已告警日期(跨越当天只告警一次)
    _qzone_session_ids: set[str] = set()  # 虚拟流 session(运行时收集;豁免判定用)
    _last_speaker_map: dict[str, str] = {}  # 流→最近真实说话人(§3.10 群聊 memo 归属兜底;内存映射,重启丢失可接受)
    _qzone_warned: set[str] = set()
    _qzone_group_prompt_value: str = ""
    _qzone_group_prompt_at: float = 0.0
    _qzone_available: bool = False  # 启动自检+网关就绪后置 True(Task 12)
    _qzone_seq: int = 0  # message_id 序号(on_load 以当前秒播种,防跨重启撞车触发宿主去重)
    _qzone_registry: FeedContextRegistry = FeedContextRegistry()  # 注入上下文追踪(工具目标解析;on_load 实例级重置)
    _qzone_comment_counts: dict[str, int] = {}  # 同说说评论频控计数(上限 3;窗口边界重置,on_load 实例级重置)
    _qzone_notify_task_armed: bool = False  # 统一通知轮询调度任务已注册标记(热重载重注册防重)
    _qzone_poll_running: bool = False  # 浏览轮询后台拉取进行中(深度审查 A-2:tick 防重入标记)
    _qzone_notify_running: bool = False  # 通知轮询后台扫描进行中(同上,通知 tick 独立标记)

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        # 旁路模板自动部署到主程序 prompts/zh-CN/(主程序 load_prompts 在插件启动后
        # 调用,同次启动即生效,无需重启);结构不符时告警跳过,不阻断加载
        written, skipped = sync_prompt_templates()
        if written or skipped:
            logger.info("旁路模板自动部署完成:写入 %d 个、内容一致跳过 %d 个", written, skipped)
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteStore(data_dir / "catsitate.db")
        self.memo = MemoService(self.store, self.config.memo)
        self.react = MsgReactEngine(JsonSnapshot(data_dir / "msg_react_cooldown.json"), self.config.msg_react)
        self.poke = PokeEngine(JsonSnapshot(data_dir / "poke_cooldown.json"), self.config.poke)
        self.fav_engine = BatchEngine(self.store, self.config.favorability)
        self.fav_executor = SettleExecutor(
            self.fav_engine,
            lambda messages, model="": self._side_llm_call(
                messages, self.config.favorability.llm_model, "favorability", self.config.favorability.llm_timeout_ms
            ),
        )
        self.assembler = InjectAssembler()
        from catsitate_core.decay import DecayExecutor
        from catsitate_core.sleep import SleepManager
        from catsitate_core.schedule import ScheduleGenerator

        # llm_call 为 2 参调用契约,经 lambda 包装 _side_llm_call(与一期 SettleExecutor 装配同模式)
        self.decay = DecayExecutor(
            self.store, self.config.favorability,
            lambda messages, model="": self._side_llm_call(
                messages, self.config.favorability.decay_llm_model, "decay", self.config.favorability.decay_llm_timeout_ms
            ),
        )
        self.sleep = SleepManager(JsonSnapshot(data_dir / "sleep_state.json"), self.config.sleep)
        self.schedule_gen = ScheduleGenerator(
            lambda messages, model="": self._side_llm_call(
                messages, model or self.config.schedule.schedule_llm_model, "schedule_generate", self.config.schedule.schedule_llm_timeout_ms
            ),
            self.config.schedule, self.config.sleep,
        )
        self._schedule_data: dict = {}
        self._schedule_edit_history: list[dict] = []
        self._speak_counts: dict[str, int] = {}  # date -> 已发言次数
        self._last_activity_ts: float = 0.0  # 静默入睡计时(入站/出站活动刷新)
        self._sleep_window_settled: str = ""  # 已处理(入睡/补生成)过的睡眠窗口 end 标记(Q1 防重复)
        # 睡眠期拦截消息缓冲(回顾报告素材);持久化防重启丢失(联调发现)
        self._sleep_review_buffer_snapshot = JsonSnapshot(data_dir / "sleep_review_buffer.json")
        # 入睡任务发布的日记正文(醒来回注虚拟流用);持久化防重启丢失
        self._pending_diary_snapshot = JsonSnapshot(data_dir / "qzone_pending_diary.json")
        _loaded_buffer = self._sleep_review_buffer_snapshot.load()
        # JsonSnapshot.load 仅接受 dict(非 dict 一律返回 {}):缓冲以 {"messages": [...]} 包装存储
        self._sleep_review_buffer: list[dict] = (
            _loaded_buffer.get("messages", []) if isinstance(_loaded_buffer, dict) else []
        )
        self.ctx.logger.info("回顾缓冲加载: %d 条", len(self._sleep_review_buffer))
        for service in (self.memo, self.fav_engine):
            service.ensure_schema()
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_usage (
                day TEXT NOT NULL,
                module TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, module)
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_snapshot (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                city TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        self._snapshot_cache: OrderedDict[str, dict] = OrderedDict()  # 注入块文本 -> 快照 UserMessageItem(LRU 上限 SNAPSHOT_CACHE_MAX)
        self._env_cache: dict[str, str] = {}  # content_key -> 环境块文本
        self._env_fetched_at: datetime | None = None
        self._stream_cache: dict[str, dict] = {}  # session_id -> 流信息(说话人解析,10 分钟 TTL)
        self._stream_cache_at: float = 0.0
        self._settling: set[str] = set()  # 结算并发防护键(按人,user_id;最终审查 Important#1/M2)
        self._background_tasks: set[asyncio.Task] = set()  # 后台任务引用(最终审查 Important#2)
        # 三期 M1:QQ空间感知模块(网关/客户端/注入泵;启动自检失败则保持停用并已告警)
        self.qzone_seen = SeenStore(self.store)
        self.qzone_seen.ensure_schema()
        # M2:评论去重与好感度显式事件表(窗口外评论轮询/点赞/出站评论的数据源)
        self.qzone_comment_seen = CommentSeenStore(self.store)
        self.qzone_comment_seen.ensure_schema()
        self.qzone_cookie = CookieManager(
            JsonSnapshot(data_dir / "qzone_cookies.json"),
            api_call=self.ctx.api.call,
            refresh_minutes=self.config.qzone.cookie_refresh_minutes,
        )
        # 深度审查 F3:cookie 含登录凭据,文件存在则收紧为属主可读(比照 SQLiteStore
        # 的 0600 纪律);JsonSnapshot.save 经 mkstemp+rename 本就 0600,此处兜住旧
        # 版本遗留/外部复制产生的宽权限存量文件
        cookie_file = data_dir / "qzone_cookies.json"
        if cookie_file.exists():
            try:
                os.chmod(cookie_file, 0o600)
            except OSError:
                self.ctx.logger.warning("qzone_cookies.json 权限设置失败(凭据文件可能非属主可读,建议人工检查)")
        self.qzone_client = QzoneClient(
            cookie_provider=self.qzone_cookie.get,
            fetch=self._qzone_http_fetch,
            timeout_ms=self.config.qzone.request_timeout_ms,
            max_retries=self.config.qzone.max_retries,
            # 写路径身份参数(opuin/qzreferrer/topicId.uin);为空时自检已停用模块,不会走到写路径
            bot_uin=str(self.config.favorability.bot_user_id or "").strip(),
        )
        if self.config.qzone.max_retries != 0:
            self.ctx.logger.warning(
                "qzone.max_retries=%s 当前版本不消费(动作 API 固定不重试,读路径固定单次),配置仅预留",
                self.config.qzone.max_retries,
            )
        # 工具驱动旧配置兼容:持久化的白名单缺空间工具时告警(不静默改配置)——
        # 旧配置含已废弃的 reply(receive 网关下无害但无效);qzone_comment/
        # qzone_reply 缺席会让 bot 在虚拟流里无法互动,qzone_post 缺席则无法发说说
        missing_qzone_tools = [
            t for t in ("qzone_comment", "qzone_reply", "qzone_post")
            if t not in self.config.qzone.tool_whitelist
        ]
        if missing_qzone_tools:
            self.ctx.logger.warning(
                "qzone.tool_whitelist 缺少 %s(旧配置残留),"
                "虚拟流将无法使用对应空间工具——请在配置中补入",
                "/".join(missing_qzone_tools),
            )
        if "reply" in self.config.qzone.tool_whitelist:
            self.ctx.logger.warning(
                "qzone.tool_whitelist 含已废弃的 reply(v0.7 工具驱动:receive 网关无出站路径),该项无效可移除"
            )
        self.qzone_injector = FeedInjector(decision_window_s=self.config.qzone.decision_window_seconds)
        # seq 以当前秒播种:重启归零会让 qzone_{tid}_{seq} 与上一轮运行撞车,
        # 被宿主 driver_id:message_id 去重拒绝(联调缺陷#11,静默丢注入)
        self._qzone_seq = int(time.time())
        # 工具驱动架构:注入上下文登记表实例级重置(类属性为共享可变态,按次加载初始化)
        self._qzone_registry = FeedContextRegistry()
        # 同说说评论频控计数实例级重置(窗口边界亦重置,防跨加载残留)
        self._qzone_comment_counts = {}
        # 轮询后台任务防重入标记实例级重置(类属性共享可变态,卸载取消任务后不得残留 True)
        self._qzone_poll_running = False
        self._qzone_notify_running = False
        # 说话人映射实例级重置(类属性为共享可变态,按次加载初始化;§3.10)
        self._last_speaker_map = {}
        # 泵并发锁:_qzone_pump 两个入口(调度 tick/轮完成信号)整体互斥,防弹出-置位间隙双弹
        self._qzone_pump_lock = asyncio.Lock()
        # 模块日志转发(联调缺陷#10):catsitate_core.* 的告警路由到插件 ctx logger,否则不可见
        self._module_log_forwarder = _ModuleLogForwarder(self.ctx.logger)
        _module_root = logging.getLogger("catsitate_core")
        _module_root.addHandler(self._module_log_forwarder)
        _module_root.setLevel(logging.INFO)
        self._qzone_available = await self._qzone_selfcheck()
        if self._qzone_available:
            await self._qzone_gateway_ready()
        self._scheduler = Scheduler(tick_seconds=60)
        self._scheduler.register("qzone_poll", max(self.config.qzone.poll_interval_minutes, 1) * 60, self._qzone_poll_tick)
        # M2.1 统一通知轮询(替代旧评论轮询):高频短间隔模拟推送通知,始终运行醒着即可;
        # 注册下限 30s 防风控,tick 内自检开关/睡眠/awaiting 占用/可用性
        self._scheduler.register(
            "qzone_notify_poll", max(self.config.qzone.notification_interval_seconds, 30), self._qzone_notify_poll_tick
        )
        self._qzone_notify_task_armed = True
        self._scheduler.register("weather", max(self.config.time_aware.weather_refresh_minutes, 1) * 60, self._refresh_environment)
        self._scheduler.register("holiday", 24 * 3600, self._refresh_environment)
        # qzone 数据保留期清理(深度审查 D-1):评论去重 30 天+seen 表 7 天,每日一次
        self._scheduler.register("qzone_data_prune", 24 * 3600, self._qzone_data_prune)
        self._scheduler.register("memo_cleanup", 3600, self._cleanup_memos)
        self._scheduler.register("daily_settle", max(self.config.favorability.window_hours, 1) * 3600, self._daily_settle)
        self._scheduler.register("daily_decay", 24 * 3600, self._daily_decay)  # 每日一次(与日终结算同 tick)
        self._scheduler.register("sleep_tick", 60, self._sleep_tick)
        self._schedule_generated: bool = False  # 当天日程是否为 LLM 生成(模板撑场为 False)
        self._schedule_tick_fired: dict[str, str] = {}  # day -> 已触发窗口 mark(day|start)
        # 触发去重持久化(联调发现:内存态重启后旧备忘重复注入)
        self._remind_fired_snapshot = JsonSnapshot(data_dir / "remind_fired.json")
        self._remind_fired: dict[str, str] = self._remind_fired_snapshot.load()  # remind:<id> -> 触发时刻
        self._scheduler.register("schedule_tick", 60, self._schedule_tick)
        self._scheduler.register("remind_fallback", 300, self._remind_fallback_tick)
        self._restore_schedule()  # 重启恢复当日日程与编辑历史(审查 I-4)
        self._setup_debug_logging()
        self._scheduler.start()
        # 首次环境数据立即刷新一次,避免环境块空缺到首个定时点(45 分钟)
        self._spawn_background_task(self._refresh_environment())
        try:
            from lunar_python import Solar as _solar_probe  # noqa: F401
        except ImportError:
            self.ctx.logger.warning("lunar-python 未安装:农历节日/节气不可用(公历回退链不受影响)")
        self.ctx.logger.info("catsitate_core 已加载:注入/备忘录/好感度/贴表情/戳一戳/reply补传/图片重看")

    async def on_unload(self) -> None:
        self._teardown_debug_logging()  # 卸载清理:debug handler 移除并 close、logger 级别恢复(审查 I5)
        if self._module_log_forwarder is not None:
            logging.getLogger("catsitate_core").removeHandler(self._module_log_forwarder)  # 模块日志转发清理
        await self._scheduler.stop()
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self.store.close()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data, version  # 新配置已由 Runner 注入 self.config,这里只刷新派生缓存
        if scope == "self":
            self.assembler.reset()
            self._env_cache.clear()
            self._env_fetched_at = None
            self._snapshot_cache.clear()
            # 调度周期随配置热重载(审查 Minor#5):weather/daily_settle 间隔取新值重注册
            self._scheduler.unregister("weather")
            self._scheduler.register("weather", max(self.config.time_aware.weather_refresh_minutes, 1) * 60, self._refresh_environment)
            self._scheduler.unregister("daily_settle")
            self._scheduler.register("daily_settle", max(self.config.favorability.window_hours, 1) * 3600, self._daily_settle)
            # qzone 热重载比照 weather 模式:自检通过还需网关就绪上报,否则每次注入都会被宿主拒绝;
            # 拉取间隔取新值重注册(FeedInjector 的 decision_window 热刷新留 M2)
            self._qzone_available = await self._qzone_selfcheck() if self.config.qzone.enabled else False
            if self._qzone_available:
                await self._qzone_gateway_ready()
                self._scheduler.unregister("qzone_poll")
                self._scheduler.register("qzone_poll", max(self.config.qzone.poll_interval_minutes, 1) * 60, self._qzone_poll_tick)
                # 统一通知轮询间隔热重载(比照 qzone_poll;开关热生效由 tick 首行自检承担)
                if self._qzone_notify_task_armed:
                    self._scheduler.unregister("qzone_notify_poll")
                    self._scheduler.register(
                        "qzone_notify_poll", max(self.config.qzone.notification_interval_seconds, 30), self._qzone_notify_poll_tick
                    )
            self._setup_debug_logging()  # debug 开关随配置热生效
            self.ctx.logger.info("catsitate_core 配置已刷新,派生缓存已重置")
        elif scope == "bot":
            # personality 变化影响等级规则块注入与哨兵人设(下次渲染自动生效)
            self.assembler.reset()
            self._snapshot_cache.clear()
            self._persona_cache = None
            self._style_cache = None

    # ---------- 工具 ----------

    @Tool(
        "memo_write",
        description="为当前用户或聊天流记一条短时备忘。内容需简明(≤80 字符);ttl_hours 为单条有效期(小时,≤168),缺省用默认 24 小时;按内容需要可延长(如『周四交作业』可设到周四)。",
        brief_description="记短时备忘",
        detailed_description="写入后按人跨流注入(备忘对主QQ与附带QQ在任何聊天流均可见,条目所在流也可见),过期自动清理。",
        parameters=[
            ToolParameterInfo(name="content", param_type="string", description="备忘内容,≤80 字符", required=True),
            ToolParameterInfo(name="stream_id", param_type="string", description="关联聊天流,默认当前流", required=False),
            ToolParameterInfo(name="user_id", param_type="string", description="关联用户(主QQ),默认当前说话人", required=False),
            ToolParameterInfo(name="ttl_hours", param_type="number", description="单条有效期小时数,缺省用默认", required=False),
            ToolParameterInfo(name="remind_at", param_type="string",
                              description="可选提醒时刻,ISO 格式如 2026-08-16T19:00。重要:备忘内容含时间要求时(如「5分钟后」「今晚8点」)必须换算为绝对时间传入本参数,否则到期不会提醒", required=False),
            ToolParameterInfo(name="related_user_ids", param_type="string",
                              description="可附带相关人的QQ号列表(逗号分隔,≤5个,超出截断),备忘对这些人同样可见(跨流)", required=False),
        ],
        visibility="visible",
    )
    async def memo_write(
        self,
        content: str = "",
        stream_id: str = "",
        user_id: str = "",
        ttl_hours: float | None = None,
        related_user_ids: str = "",
        **kwargs: Any,
    ) -> str:
        if not self.config.plugin.enabled or not self.config.memo.tool_enabled:
            return "备忘录工具未启用。"
        remind_at = str(kwargs.get("remind_at") or "")
        if err := validate_remind_at(remind_at):
            return err  # 非法提醒时间显式返回给 LLM(审查 M-10)
        sid = stream_id or str(kwargs.get("stream_id") or "")
        # 私聊流官方 kwargs 自动注入可靠;群聊 user_id 常为空——以 fav_count 维护的
        # 最近说话人映射兜底(§3.10 取数点;重启丢失可接受,_resolve_speaker 回退仍在)
        uid = (
            user_id
            or str(kwargs.get("user_id") or "")
            or self._last_speaker_map.get(sid, "")
        )
        # 附带 QQ:逗号分隔字符串(兼容中文逗号/顿号)→ 列表;清洗去重交 MemoService.write
        related_raw = related_user_ids or str(kwargs.get("related_user_ids") or "")
        extra: list[str] | None = None
        if related_raw.strip():
            extra = [
                part.strip()
                for part in related_raw.replace("，", ",").replace("、", ",").split(",")
                if part.strip()
            ]
        ok, msg = self.memo.write(content, sid, uid, ttl_hours, remind_at=remind_at, extra_user_ids=extra)
        return msg if ok else f"备忘写入失败:{msg}"

    @Tool(
        "memo_read",
        description="读取当前流与当前说话人相关的未过期备忘,含各自剩余有效时间。",
        brief_description="读短时备忘",
        parameters=[],
        visibility="visible",
    )
    async def memo_read(self, **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.memo.tool_enabled:
            return "备忘录工具未启用。"
        entries = self.memo.read(str(kwargs.get("stream_id") or ""), str(kwargs.get("user_id") or ""), limit=self.config.memo.inject_max)
        if not entries:
            return "当前没有未过期的备忘。"
        lines = [f"- {e['content']}(剩余 {e['remaining_hours']:.1f} 小时)" for e in entries]
        return "\n".join(lines)

    @Tool(
        "update_schedule",
        description="增/删/改 bot 自己今天的日程安排(活动窗口)。活动最多 8 个;睡眠窗口不可删除、时间修改受最短/最长睡眠约束。",
        brief_description="修改今日日程",
        parameters=[
            ToolParameterInfo(name="action", param_type="string", description="view(查看当前日程)/move(把某窗口挪到新时段)/add(新增活动)/delete(删除活动窗口)。日程按时间顺序排列,窗口序号以 view 输出为准。建议流程:编辑前先 view 看当前日程与窗口序号,编辑后再次 view 确认结果。常用示例:把睡眠窗口改成11:45到16:00 → action=move, window_index=view 中睡眠窗口的序号, start=11:45, end=16:00;新增下午听歌 → action=add, start=16:00, end=18:00, activity=和Hesitate_P一起听歌", required=True),
            ToolParameterInfo(name="window_index", param_type="integer", description="move/delete 时的窗口序号(view 结果每行开头数字)", required=False),
            ToolParameterInfo(name="start", param_type="string", description="move/add 的新开始时刻,HH:MM 格式如 11:45(自动按当天日期)", required=False),
            ToolParameterInfo(name="end", param_type="string", description="move/add 的新结束时刻,HH:MM 格式如 16:00(跨午夜自动次日)", required=False),
            ToolParameterInfo(name="activity", param_type="string", description="add 时的活动描述(如 和Hesitate_P一起听歌);move 时留空保持原活动", required=False),
        ],
        visibility="visible",
    )
    async def update_schedule(self, action: str = "", window_index: int = 0, start: str = "", end: str = "", activity: str = "", **kwargs: Any) -> str:
        del kwargs
        if not self.config.plugin.enabled or not self.config.schedule.enabled:
            return "日程模块未启用。"
        if not self._schedule_data:
            return "今天还没有日程,等睡前一并安排吧。"
        if action == "view":
            return "当前日程(每行开头是窗口序号):\n" + schedule_overview_text(self._schedule_data)
        day = self._schedule_data.get("date") or datetime.now().strftime("%Y-%m-%d")
        min_sleep, max_sleep = self.config.sleep.min_sleep_minutes, self.config.sleep.max_sleep_minutes
        adjustments: list[str] = []
        if action == "move":
            data, err, history, adjustments = apply_schedule_move(
                self._schedule_data, window_index, start, end, day,
                min_sleep=min_sleep, max_sleep=max_sleep, history=self._schedule_edit_history,
            )
        elif action == "add":
            data, err, history, adjustments = apply_schedule_add(
                self._schedule_data, start, end, activity, day,
                min_sleep=min_sleep, max_sleep=max_sleep, history=self._schedule_edit_history,
            )
        elif action == "delete":
            data, err, history = apply_schedule_edit(
                self._schedule_data, "delete", window_index, None, self._schedule_edit_history,
                min_sleep=min_sleep, max_sleep=max_sleep,
            )
        else:
            return f"未知操作: {action}(可选 view/move/add/delete)"
        if err:
            return f"{err}\n当前日程:\n" + schedule_overview_text(self._schedule_data)
        self._schedule_data = data
        self._schedule_edit_history = history
        self._persist_schedule()
        if adjustments:
            # 重叠警告(联调对齐):编辑发生压缩时返回明细,bot 可再 view 确认
            return "日程已更新。注意:与已有安排重叠,已自动调整:" + ";".join(adjustments)
        return "日程已更新。"

    @Tool(
        "msg_react",
        description="给目标消息贴一个表情回应(从配置白名单中选择最合适的)。",
        brief_description="贴表情",
        detailed_description="仅表情白名单内的 emoji_id 可贴;同一聊天流有冷却(工程护栏)。",
        parameters=[
            ToolParameterInfo(name="message_id", param_type="string", description="目标消息 ID", required=True),
            ToolParameterInfo(name="intent", param_type="string", description="贴表情意图(可选文字)", required=False),
        ],
        visibility="visible",
    )
    async def msg_react(self, message_id: str = "", intent: str = "", **kwargs: Any) -> str:
        if str(kwargs.get("platform") or "") == QZONE_PLATFORM:
            return "当前是QQ空间动态流,这个动作用不上哦。"
        if not self.config.plugin.enabled or not self.config.msg_react.enabled:
            return "贴表情工具未启用。"
        if not str(kwargs.get("group_id") or ""):
            return "贴表情仅限群聊(QQ 私聊不支持贴表情)。"
        stream_id = str(kwargs.get("stream_id") or "")
        ok, reason = self.react.check_cooldown(stream_id)
        if not ok:
            return reason
        target_text = await self._fetch_message_text(stream_id, message_id)
        messages, _ = self.react.build_choose_prompt(target_text or f"消息 {message_id}", intent)
        result = await self._side_llm_call(messages, self.config.msg_react.llm_model, "msg_react", self.config.msg_react.llm_timeout_ms)
        if not isinstance(result, dict) or not result.get("success"):
            # 不落响应原文(安全复审):仅记失败形态
            detail = f"success={result.get('success')}" if isinstance(result, dict) else f"结果类型={type(result).__name__}"
            return f"选表情 LLM 调用失败({detail})"
        emoji, err = parse_choice_resp(str(result.get("response") or ""))
        if emoji is None:
            return f"选表情失败:{err}"
        api_result = await self.ctx.api.call("adapter.napcat.message.set_msg_emoji_like", message_id=message_id, emoji_id=emoji)
        if not self._api_ok(api_result):
            return f"贴表情 API 失败:{api_result}"
        self.react.mark_used(stream_id)
        return f"已贴表情 {emoji}"

    @Tool(
        "poke_user",
        description="主动戳一戳目标用户(仅冷却限制)。",
        brief_description="主动戳一戳",
        parameters=[
            ToolParameterInfo(name="user_id", param_type="string", description="目标用户 ID", required=True),
            ToolParameterInfo(name="group_id", param_type="string", description="群号(群聊必填,私聊留空)", required=False),
        ],
        visibility="visible",
    )
    async def poke_user(self, user_id: str = "", group_id: str = "", **kwargs: Any) -> str:
        if str(kwargs.get("platform") or "") == QZONE_PLATFORM:
            return "当前是QQ空间动态流,这个动作用不上哦。"
        del kwargs
        if not self.config.plugin.enabled or not self.config.poke.poke_tool_enabled:
            return "主动戳工具未启用。"
        ok, reason = self.poke.can_poke(user_id)
        if not ok:
            return reason
        # 实测 API 签名:send_poke(user_id, group_id, target_id, qq_id)
        api_result = await self.ctx.api.call(
            "adapter.napcat.message.send_poke",
            user_id=user_id,
            group_id=group_id or None,
            target_id=user_id,
        )
        if not self._api_ok(api_result):
            return f"戳一戳 API 失败:{api_result}"
        self.poke.mark_poked(user_id)
        return "已戳。"

    @Tool(
        "inspect_image",
        description="重看聊天里的某张图片,针对图片回答具体问题(如『图片里写了什么』)。",
        brief_description="图片重看",
        detailed_description="目标消息太旧、get_recent 取不到时返回错误并记录日志。",
        parameters=[
            ToolParameterInfo(name="message_id", param_type="string", description="目标消息 ID(可选,缺省按 image_index 取)", required=False),
            ToolParameterInfo(name="image_index", param_type="integer", description="倒数第几张含图消息(默认 1)", required=False),
            ToolParameterInfo(name="question", param_type="string", description="针对图片的具体问题", required=True),
        ],
        visibility="visible",
    )
    async def inspect_image(self, message_id: str = "", image_index: int = 1, question: str = "", **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.image_relook.enabled:
            return "图片重看工具未启用。"
        stream_id = str(kwargs.get("stream_id") or "")
        # 方案 B(2026-08-31):注入消息 timestamp=阅读时刻,天然落在宿主 24h 默认窗内,
        # 无需放宽取数窗(原缺陷#14 的 hours 放宽随之移除)
        recent = await self._fetch_recent(stream_id, limit=50)
        seg, err = find_image_segment(recent, message_id or None, image_index)
        if seg is None:
            self.ctx.logger.warning("inspect_image 失败:%s(stream=%s,message_id=%s)", err, stream_id, message_id)
            return f"取图失败:{err}"
        # spike ④ 实测:image 段仅 hash 无 data;SDK 无 ctx.database 属性,直调 database.get 能力
        # 拿 full_path(相对主程序根目录 /MaiMBot)后读文件补 base64(插件与主程序同容器共享文件系统)
        db_result = await self.ctx.call_capability(
            "database.get",
            model_name="Images",
            filters={"image_hash": str(seg.get("hash"))},
            single_result=True,
        )
        if not isinstance(db_result, dict) or not db_result.get("full_path"):
            msg = f"图片 {seg.get('file_name') or seg.get('hash')} 数据库补读失败:{db_result}"
            self.ctx.logger.error(msg)
            return msg
        image_path = Path("/MaiMBot") / str(db_result["full_path"])
        try:
            image_bytes = image_path.read_bytes()
        except OSError as exc:
            msg = f"图片文件读取失败 {image_path}: {exc}"
            self.ctx.logger.error(msg)
            return msg
        import base64

        seg = {**seg, "data": base64.b64encode(image_bytes).decode("ascii")}
        messages, _ = build_relook_prompt(question, seg)
        try:
            result = await self._side_llm_call(messages, self.config.image_relook.llm_model, "image_relook", self.config.image_relook.llm_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            # 失败显式日志并返回失败(与哨兵层同款纪律,审查 M9);仅记异常类型防 PII
            msg = f"图片重看 LLM 调用异常({type(exc).__name__})"
            self.ctx.logger.warning(msg)
            return msg
        if not isinstance(result, dict) or not result.get("success"):
            # 不落响应原文(安全复审):仅记失败形态
            detail = f"success={result.get('success')}" if isinstance(result, dict) else f"结果类型={type(result).__name__}"
            msg = f"图片重看 LLM 调用失败:{detail}"
            self.ctx.logger.warning(msg)
            return msg
        return str(result.get("response") or "")

    def _qzone_resolve_feed(self, feed_id: str) -> tuple[str, str, FeedContext | None]:
        """工具目标解析(工具驱动 2026-09-01):registry(精确/前缀)→ seen_store
        (7 天浏览窗前缀)→ awaiting(当前浏览项)→ 显式失败。

        返回 (全量 tid, 说说主人 uin, registry 上下文);tid 空表示解析失败。
        全量 tid 必须回填——消息尾部锚只展示 tid 前 12 位,直接拿锚值发 API
        会构造畸形 unikey/topicId;owner 与泵登记同公式(通知源A=bot 自己)。
        """

        key = str(feed_id or "").strip()
        if not key:
            return "", "", None
        ctx = self._qzone_registry.resolve(key)
        if ctx is not None:
            return ctx.tid, ctx.owner_uin, ctx
        for r in self.qzone_seen.recent_seen(limit=200, days=7, now=datetime.now()):
            if r["tid"].startswith(key):
                return r["tid"], r["author_uin"], None
        awaiting = self.qzone_injector.awaiting_feed
        if awaiting is not None:
            # 通知项取真实说说 tid;无 origin_tid 的畸形通知不可解析(合成 tid 发 API 必畸形)
            real = awaiting.origin_tid or ("" if awaiting.source == "notify" else awaiting.tid)
            if real and (real == key or real.startswith(key)):
                bot_uin = str(self.config.favorability.bot_user_id or "").strip()
                owner = (awaiting.friend_uin or bot_uin) if awaiting.source == "notify" else awaiting.uin
                return real, owner, None
        return "", "", None

    @Tool(
        "qzone_like",
        description="给当前正在看的好友说说点赞(QQ空间)。仅在浏览动态时可用;可传 feed_id 精确指定(照抄消息末尾「说说 xxx」括号里的字符),缺省对当前说说点赞。",
        brief_description="给当前说说点赞",
        parameters=[ToolParameterInfo(name="feed_id", param_type="string", description="目标说说ID(照抄消息尾部「说说 xxx」;可选,缺省当前说说)", required=False)],
        visibility="visible",
    )
    async def qzone_like(self, feed_id: str = "", **kwargs: Any) -> str:
        # 硬门控(SDK @Tool 无类级 allowed_session 通道,实测结论见任务报告):
        # 仅虚拟流会话可用,真实聊天流调用直接拒绝
        if str(kwargs.get("stream_id") or "") not in self._qzone_session_id_set():
            return "该工具仅在浏览QQ空间动态时可用。"
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        target = str(feed_id or "").strip()
        if not target:
            awaiting = self.qzone_injector.awaiting_feed
            if awaiting is None:
                return "当前没有正在浏览的说说,想点赞请带上消息尾部的说说ID。"
            # 通知项取真实说说 tid(可点其原说说);无 origin_tid 的畸形通知显式拒绝
            target = awaiting.origin_tid or ("" if awaiting.source == "notify" else awaiting.tid)
            if not target:
                return "当前是互动通知且缺少原说说信息,无法点赞。"
        # 目标解析:registry → seen_store → awaiting(通知项不再拒赞——锚指向真实说说)
        fid, owner_uin, ctx = self._qzone_resolve_feed(target)
        if not fid:
            return f"未找到说说 {target[:12]},可能已过期,请核对消息尾部的说说ID。"
        try:
            await self.qzone_client.do_like(fid=fid, target_qq=owner_uin)
        except QzoneAuthError:
            # 与轮询同款自愈链:作废 cookie 下轮重取,点赞留给下次浏览
            self.qzone_cookie.invalidate()
            self.ctx.logger.warning("QQ空间点赞遇登录态失效,cookie 已作废,下轮重取")
            return "点赞失败:登录态失效已重置,稍后再试。"
        except Exception:
            self.ctx.logger.exception("QQ空间点赞失败(tid=%s)", fid)
            return "点赞失败,已记录日志。"
        self.qzone_seen.mark_interacted(fid)
        nickname = ctx.owner_nickname if ctx else owner_uin
        self.qzone_comment_seen.fav_event(owner_uin, "OUT_LIKE", f"你点赞了 {owner_uin} 的说说")
        return f"已点赞 {nickname} 的说说。"

    @Tool(
        "qzone_comment",
        description="评论当前看到的好友说说(QQ空间)。feed_id 填消息末尾「说说 xxx」括号里的那串字符;要@谁就把TA的QQ号填 at_user_id。",
        brief_description="评论说说",
        parameters=[
            ToolParameterInfo(name="feed_id", param_type="string", description="目标说说ID(照抄消息尾部的说说ID)", required=True),
            ToolParameterInfo(name="content", param_type="string", description="评论内容(≤200字)", required=True),
            ToolParameterInfo(name="at_user_id", param_type="string", description="要@的人的QQ号(回应评论时填消息尾部标注的QQ号)", required=False),
        ],
        visibility="visible",
    )
    async def qzone_comment(self, feed_id: str = "", content: str = "", at_user_id: str = "", **kwargs: Any) -> str:
        """评论说说——bot 自主决定是否/如何评论(工具驱动,替代意图路由)。"""
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        stream_id = str(kwargs.get("stream_id") or "")
        if stream_id not in self._qzone_session_id_set():
            return "这个工具只能在QQ空间动态流里使用。"
        if not feed_id.strip():
            return "缺少说说ID,请照抄消息末尾「说说 xxx」括号里的字符。"
        content = content.strip()
        if not content:
            return "评论内容不能为空。"
        if len(content) > 200:
            return f"评论太长了({len(content)} 字,上限 200),请精简。"
        # 目标解析:registry → seen_store → awaiting → 显式失败(fid 回填全量 tid)
        fid, owner_uin, ctx = self._qzone_resolve_feed(feed_id)
        if not fid:
            return f"未找到说说 {feed_id[:12]},可能已过期,请核对消息尾部的说说ID。"
        # 频控:同说说评论计数上限 3(窗口边界重置,防对同一条说说刷屏)
        count = self._qzone_comment_counts.get(fid, 0)
        if count >= 3:
            return "这条说说你已经评论过 3 次了,适可而止～"
        # @ 前缀(napcat 适配器同格式,QQ 空间原生支持):nick 默认 QQ 号,
        # registry 有该评论者昵称则用昵称(通知场景回应评论最自然)
        at_nick = ""
        if at_user_id.strip():
            at_uin = at_user_id.strip()
            at_nick = at_uin
            if ctx and ctx.commenter_uin == at_uin:
                at_nick = ctx.commenter_nickname or at_uin
            content = f"@{{uin:{at_uin},nick:{at_nick},auto:1}}{content}"
        try:
            await self.qzone_client.do_comment(fid=fid, target_qq=owner_uin, content=content)
        except QzoneAuthError:
            self.qzone_cookie.invalidate()
            self.ctx.logger.warning("QQ空间评论遇登录态失效,cookie 已作废,下轮重取")
            return "登录态失效已重置,请稍后再试。"
        except Exception:
            self.ctx.logger.exception("QQ空间评论失败(feed_id=%s)", fid[:16])
            return "评论失败,已记录日志。"
        # 记账(远端已成功,记账失败仅告警——错误显式暴露但不误报失败)
        try:
            self.qzone_seen.mark_interacted(fid)
            self.qzone_comment_seen.note_bot_comment(fid, owner_uin, content, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
            fav_target = at_user_id.strip() or owner_uin
            fav_kind = "COMMENT" if at_user_id.strip() else "OUT_COMMENT"
            fav_text = f"你在 {owner_uin} 的说说下评论" + (f"并@了 {at_nick}" if at_user_id.strip() else "")
            self.qzone_comment_seen.fav_event(fav_target, fav_kind, fav_text)
        except Exception:
            self.ctx.logger.exception("QQ空间评论记账失败(远端已成功,仅告警)")
        self._qzone_comment_counts[fid] = count + 1
        return f"已评论{'并@了 ' + at_nick if at_user_id.strip() else ''}。"

    @Tool(
        "qzone_reply",
        description="回复好友在你说说下的评论或对你评论的回复(QQ空间楼中楼)。feed_id 和 comment_id 都照抄消息尾部括号里的标注。",
        brief_description="回复评论(楼中楼)",
        parameters=[
            ToolParameterInfo(name="feed_id", param_type="string", description="目标说说ID(照抄消息尾部「说说 xxx」)", required=True),
            ToolParameterInfo(name="comment_id", param_type="string", description="要回复的主评论ID(照抄消息尾部「评论 xxx」)", required=True),
            ToolParameterInfo(name="content", param_type="string", description="回复内容(≤200字)", required=True),
        ],
        visibility="visible",
    )
    async def qzone_reply(self, feed_id: str = "", comment_id: str = "", content: str = "", **kwargs: Any) -> str:
        """楼中楼回复——commentId+commentUin 二元组精确匹配主评论(联调实证)。"""
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        stream_id = str(kwargs.get("stream_id") or "")
        if stream_id not in self._qzone_session_id_set():
            return "这个工具只能在QQ空间动态流里使用。"
        if not (feed_id.strip() and comment_id.strip() and content.strip()):
            return "说说ID、评论ID和回复内容都不能为空,请照抄消息尾部的标注。"
        content = content.strip()
        if len(content) > 200:
            return f"回复太长了({len(content)} 字,上限 200)。"
        fid, target_qq, ctx = self._qzone_resolve_feed(feed_id)
        if not fid:
            return f"未找到说说 {feed_id[:12]},可能已过期。"
        # 楼中楼二元组:commentId=主评论 tid(消息锚「评论 xxx」),commentUin=主评论
        # 作者(通知场景经 FeedItem.comment_uin 传递:源A=评论好友/源B=bot 自己;
        # 无上下文回退 bot——bot 只在自己的评论线程里收到回复通知)
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        comment_uin = ctx.comment_uin if ctx and ctx.comment_uin else bot_uin
        # @ 目标=正在对话的评论者/回复者(与二元组解耦:源B 回复线程头=bot 自己
        # 的评论,但 @ 的是回复者;前缀由 wire.build_reply_form 统一拼装)
        at_uin = ctx.commenter_uin if ctx and ctx.commenter_uin else comment_uin
        at_nick = (ctx.commenter_nickname if ctx else "") or at_uin or "好友"
        try:
            await self.qzone_client.do_reply(fid=fid, target_qq=target_qq,
                                             comment_tid=comment_id.strip(), comment_uin=comment_uin,
                                             comment_nick=at_nick, content=content,
                                             at_uin=at_uin, at_nick=at_nick)
        except QzoneAuthError:
            self.qzone_cookie.invalidate()
            self.ctx.logger.warning("QQ空间楼中楼回复遇登录态失效,cookie 已作废,下轮重取")
            return "登录态失效已重置,请稍后再试。"
        except Exception:
            self.ctx.logger.exception("QQ空间楼中楼回复失败(feed=%s,comment=%s)", fid[:12], comment_id)
            return "回复失败,已记录日志。"
        try:
            self.qzone_comment_seen.fav_event(at_uin, "COMMENT", f"你回复了 {at_nick} 的评论")
        except Exception:
            self.ctx.logger.exception("QQ空间楼中楼记账失败(仅告警)")
        return f"已回复 {at_nick} 的评论。"

    @Tool(
        "qzone_post",
        description="发布一条自己的说说(QQ空间)。想说点什么、分享心情或见闻时使用。内容自然即可,不要刻意。",
        brief_description="发说说",
        parameters=[
            ToolParameterInfo(name="content", param_type="string", description="说说内容(≤500字,自然表达)", required=True),
        ],
        visibility="visible",
    )
    async def qzone_post(self, content: str = "", **kwargs: Any) -> str:
        """发布说说——bot 自主决定是否/何时发,发布后自动注入虚拟流供后续互动引用。"""
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        stream_id = str(kwargs.get("stream_id") or "")
        if stream_id not in self._qzone_session_id_set():
            return "这个工具只能在QQ空间动态流里使用。"
        content = content.strip()
        if not content:
            return "说说内容不能为空。"
        if len(content) > 500:
            return f"内容太长了({len(content)} 字,上限 500)。"
        try:
            await self.qzone_client.do_publish(content=content)
        except QzoneAuthError:
            # 登录态失效自愈链(与评论/点赞同款):作废 cookie 下轮重取,本轮发布放弃
            self.qzone_cookie.invalidate()
            self.ctx.logger.warning("QQ空间说说发布遇登录态失效,cookie 已作废,下轮重取")
            return "登录态失效已重置,请稍后再试。"
        except Exception:
            self.ctx.logger.exception("QQ空间说说发布失败")
            return "发布失败,已记录日志。"
        # 回注:发布成功的说说以 self 消息注入虚拟流(不触发 planner 决策轮,仅入历史)。
        # 原因:后续好友评论此说说时通知轮询只带说说ID,bot 需要这段历史才知道自己发过什么;
        # 正文只带前 60 字预览——全文已真实发布在空间,回注只是上下文锚,超长会挤占虚拟流。
        # 回注失败不影响回执:说说已远端发布成功,谎报失败会诱导重复发布。
        self._qzone_seq += 1
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        echo_msg = {
            "message_id": f"qzone_self_{int(time.time())}_{self._qzone_seq}",
            "platform": QZONE_PLATFORM,
            "timestamp": str(int(time.time())),
            "message_info": {
                "user_info": {"user_id": bot_uin, "user_nickname": "我"},
                "group_info": {
                    "group_id": self.config.qzone.virtual_group_id,
                    "group_name": self.config.qzone.virtual_group_name,
                },
                # 不设 is_mentioned:这是 bot 自己发的,不需要触发 planner 决策轮
            },
            "raw_message": [{"type": "text", "data": f"我发布了一条说说:{content[:60]}"}],
        }
        try:
            await self.ctx.gateway.route_message(QZONE_GATEWAY_NAME, echo_msg)
        except Exception:
            self.ctx.logger.exception("QQ空间说说回注失败(发布已成功,仅上下文注入失败)")
        self.ctx.logger.info("QQ空间说说发布成功: %s", content[:30])
        return "发布成功。"

    # ---------- QQ空间(M1 感知 / M2 互动) ----------

    async def _qzone_http_fetch(self, method: str, url: str, *, params: dict, headers: dict, timeout_ms: int, data: dict | None = None) -> tuple[int, bytes]:
        """httpx 薄封装(client.py 的 fetch 注入点;超时与异常上抛由调用方告警)。

        统一返回 **bytes**:二进制图片经 resp.text 的 UTF-8 解码会失真,
        再 encode('latin-1') 必炸(联调缺陷#13);文本/JSON 由 client 侧显式
        utf-8 解码。params 为空时必须传 None:httpx 的 params={} 会把 URL
        既有 query 整体清空(联调缺陷#9 根因)——签名 URL 由此被剥签名致 404。
        data 为 M2 写路径表单(dict 时 httpx 自动 form-encode,
        Content-Type 由调用方 headers 指定);读路径保持 None。
        """

        async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
            resp = await client.request(method, url, params=params or None, headers=headers, data=data)
            return resp.status_code, resp.content

    async def _qzone_selfcheck(self) -> bool:
        """启动自检:开关 → person 别名折叠(qzone-qq 与 qq 同命名空间) → focus_mode/talk_value=0 前置。"""

        if not (self.config.plugin.enabled and self.config.qzone.enabled):
            return False
        try:
            a = await self.ctx.person.get_id(platform=QZONE_PLATFORM, user_id="1")
            b = await self.ctx.person.get_id(platform="qq", user_id="1")
            # 折叠失效=人物分裂不可接受(用户裁定 2026-08-30):硬停用,不做降级分裂模式;
            # 同时防假阴性——两侧同形失败(非 str 返回)不得误判为折叠正常
            if not isinstance(a, str) or not isinstance(b, str) or not a or not b or a != b:
                self.ctx.logger.warning(
                    "QQ空间模块停用:person 别名折叠自检失败(qzone-qq 与 qq 未折叠到同一命名空间,"
                    "或自检调用返回异常形态 a=%s b=%s),主程序 get_person_id 折叠机制可能已改版",
                    type(a).__name__, type(b).__name__,
                )
                return False
        except Exception:
            self.ctx.logger.exception("person 别名自检调用失败,QQ空间模块停用")
            return False
        try:
            result = await self.ctx.config.get(key="experimental.focus_mode", default=False)
            value = result.get("value") if isinstance(result, dict) else result
            if value:
                self.ctx.logger.warning("QQ空间模块停用:主程序 experimental.focus_mode 开启(focus 槽会吞掉注入的强制触发,spec §2.18)")
                return False
        except Exception:
            self.ctx.logger.exception("focus_mode 前置检测失败,QQ空间模块停用")
            return False
        try:
            # 键路径核对(official_configs.py):talk_value 归属 ChatConfig.reply_timing(ChatReplyTimingConfig),
            # 对应 bot_config.toml [chat.reply_timing];_cap_config_get 返回 {"success":..,"value":..} 兼容裸值
            result = await self.ctx.config.get(key="chat.reply_timing.talk_value", default=1.0)
            value = result.get("value") if isinstance(result, dict) else result
            if value is None:
                raise TypeError(f"talk_value 返回空值(形态={type(result).__name__})")
            # float("0")/0/0.0 均视为 0;非数值形态经 float() 抛错走异常路径(读取失败→告警停用)
            if float(value) == 0:
                self.ctx.logger.warning("QQ空间模块停用:群聊回复频率 talk_value=0(注入消息会被主程序静默消费)")
                return False
        except Exception:
            self.ctx.logger.exception("talk_value 前置检测失败,QQ空间模块停用")
            return False
        return True

    async def _qzone_gateway_ready(self) -> None:
        """网关就绪上报(account 用真实 bot QQ:经别名折叠统一 bot 自身 person,spec §2.1)。"""

        account = str(self.config.favorability.bot_user_id or "").strip()
        if not account:
            self.ctx.logger.warning("QQ空间模块停用:favorability.bot_user_id 为空,虚拟平台无法注册 bot 账号")
            self._qzone_available = False
            return
        try:
            # SDK update_state 返回 bool(不抛异常):宿主拒绝(如网关未注册)返回 False
            accepted = await self.ctx.gateway.update_state(QZONE_GATEWAY_NAME, ready=True, platform=QZONE_PLATFORM, account_id=account)
        except Exception:
            self.ctx.logger.exception("QQ空间网关就绪上报失败,模块停用(重载插件重试)")
            self._qzone_available = False
            return
        if not accepted:
            self.ctx.logger.warning("QQ空间网关就绪上报被拒绝,模块停用")
            self._qzone_available = False
            return
        self.ctx.logger.info("QQ空间虚拟平台就绪(platform=%s,伪群=%s)", QZONE_PLATFORM, self.config.qzone.virtual_group_id)

    @MessageGateway("receive", name=QZONE_GATEWAY_NAME, description="QQ空间虚拟聊天平台(动态流入;互动经工具发出)", platform=QZONE_PLATFORM)
    async def qzone_gateway(self, *, message: dict, route: dict, metadata: dict) -> dict:
        """receive 网关声明(工具驱动 2026-09-01):只进不出。

        出站意图路由已随意图系统删除——bot 对说说的动作(评论/回复/点赞)一律经
        qzone_comment/qzone_reply/qzone_like 工具发出,直接打字发不出去(receive
        网关宿主侧本就不回调出站);本方法仅承载网关组件声明,receive 模式下宿主
        不会调用,防御性保留显式拒发分支(错误显式暴露,不静默吞)。
        """

        del route, metadata
        self.ctx.logger.warning("QQ空间网关收到意外出站回调(receive 模式无出站路径,文本预览=%.30s)", str(message)[:64])
        return {"success": False, "error": "QQ空间出站已改经工具发出(receive 网关无出站路径)"}

    async def _qzone_poll_tick(self) -> None:
        """空间浏览轮询触发器(深度审查 A-2):防重入后派发后台拉取,立即返回。

        调度器串行 await 各任务(60s tick),逐好友 2s sleep+HTTP 的长 IO 若在
        tick 内执行会拖住 sleep_tick/schedule_tick 等全部任务——长 IO 移入
        _qzone_poll_feeds 后台任务,守卫与窗口逻辑也随迁(单一事实源)。
        """

        if self._qzone_poll_running:
            return  # 上一轮后台拉取还在跑,跳过(防重入)
        self._qzone_poll_running = True
        self._spawn_background_task(self._qzone_poll_feeds())

    async def _qzone_poll_feeds(self) -> None:
        """空间窗口内周期拉取(M3 统一时间线架构);窗口切换时收泵并回退未读。

        浏览流三段式:①发现层 get_unified_timeline(1 次调用覆盖全好友)→
        ②过滤(说说 appid=311 且 seen 未登记的新 tid,is_new_candidate 纯查)→
        ③充实层按作者 uin 分组、每组 1 次 get_user_feeds 只拉有新动态的好友
        (1+N 次调用,N=有新动态的作者数,与好友总数无关)。
        发现层非登录态失败回退 _qzone_poll_feeds_legacy 旧逐好友路径。
        """

        try:
            if not self._qzone_available:
                return
            if self.config.sleep.enabled and self.sleep.is_sleeping():
                return  # 睡眠绝对静默(spec §2.6)
            if not self._schedule_data:
                return  # 日程未生成/未恢复(日程节禁用等):无窗口可言,按窗口外处理
            win = current_window(self._schedule_data, datetime.now().strftime("%Y-%m-%dT%H:%M"))
            in_qzone_window = bool(win and win.get("kind") == "daily" and win.get("read_qzone"))
            if not in_qzone_window:
                if self.qzone_injector.window_active:
                    self.qzone_injector.window_ended()
                    reverted = self.qzone_seen.revert_pending()
                    self.ctx.logger.info(
                        "QQ空间浏览窗口结束,浏览队列回退未读(%d 条);通知队列保留等待注入", reverted
                    )
                return
            if not self.qzone_injector.window_active:
                self.qzone_injector.window_started()
                # 评论频控计数随窗口边界重置(工具驱动 2026-09-01):新窗口重新计数,
                # 防跨窗口累计误伤(上限语义=「本轮逛空间期间」对同说说最多 3 条)
                self._qzone_comment_counts.clear()
                # 回收跨窗口/跨启动的 queued 残留:注入泵队列在内存,重启即丢,
                # 而 seen 的 queued 行会让新轮拉取全部判重跳过(联调缺陷#12)
                stale = self.qzone_seen.revert_pending()
                if stale:
                    self.ctx.logger.info("QQ空间窗口开始,注入泵激活;回收跨启动 queued 残留 %d 条(重新拉取)", stale)
                else:
                    self.ctx.logger.info("QQ空间窗口开始,注入泵激活")
            # ① 发现层:统一时间线 1 次调用(feeds3_html_more,全好友聚合端点)
            try:
                discoveries = await self.qzone_client.get_unified_timeline(count=20)
            except QzoneAuthError:
                # 登录态失效自愈链(联调缺陷#7):作废 cookie 下轮重取;不回退
                # legacy——cookie 失效对两路径同源,回退只会重复失败多打一轮 API
                self.qzone_cookie.invalidate()
                self.ctx.logger.warning("QQ空间登录态失效(统一时间线),cookie 已作废,下轮重取")
                return
            except Exception:
                self.ctx.logger.exception("QQ空间统一时间线拉取失败,回退逐好友旧路径")
                await self._qzone_poll_feeds_legacy()
                return
            if not discoveries:
                await self._qzone_pump()  # 空发现也泵——超时推进兜底(旧路径每轮必泵语义)
                return
            # ② 过滤:说说(appid=311)且 seen 未登记——is_new_candidate 纯查不登记
            # (发现≠注入,登记留给充实层 mark_queued,防预占主键判重跳过);排除 bot
            # 自己(自己发的说说不当新动态围观,终审 I1 与源B侧交叉同款语义)
            bot_uin = str(self.config.favorability.bot_user_id or "").strip()
            new_items = [
                d for d in discoveries
                if d.appid == 311 and d.uin != bot_uin and self.qzone_seen.is_new_candidate(d.tid)
            ]
            if not new_items:
                await self._qzone_pump()  # 无新动态也泵——超时推进兜底(旧路径每轮必泵语义)
                return
            # ③ 充实层:按作者分组(保发现顺序),每组 1 次 get_user_feeds 拉完整实体
            by_uin: dict[str, list[FeedDiscovery]] = {}
            for d in new_items:
                by_uin.setdefault(d.uin, []).append(d)
            added_total = 0
            for uin, group in by_uin.items():
                try:
                    feeds = await self.qzone_client.get_user_feeds(
                        target_uin=uin, nickname=group[0].nickname, num=len(group) + 2
                    )
                except QzoneAuthError:
                    # 登录态失效:立即作废 cookie 缓存(下轮重取),本轮终止(联调缺陷#7 自愈链)
                    self.qzone_cookie.invalidate()
                    self.ctx.logger.warning("QQ空间登录态失效(充实层 uin=%s),cookie 已作废,本轮终止", uin)
                    return
                except Exception:
                    # 单个好友失败不中止整轮(逐人隔离,显式告警)
                    self.ctx.logger.exception("QQ空间充实层拉取失败(uin=%s),该好友本轮跳过", uin)
                    continue
                # 匹配发现层 tid → 只注入发现层认定的这条新动态(充实页含同好友旧动态)
                discovered_tids = {d.tid for d in group}
                matched_tids: set[str] = set()
                for f in feeds:
                    if f.tid not in discovered_tids:
                        continue
                    matched_tids.add(f.tid)
                    if self.qzone_seen.mark_queued(
                        f.tid, abstime=f.abstime, author_uin=f.uin, summary=f.content[:60],
                        author_nickname=f.nickname,
                    ):
                        self.qzone_injector.enqueue([f])
                        added_total += 1
                unmatched_tids = discovered_tids - matched_tids
                if unmatched_tids:
                    # 发现层认定的 tid 在充实页消失(已被删除/超出 num 窗口):debug 留痕
                    self.ctx.logger.debug(
                        "QQ空间充实层 tid 未匹配(uin=%s,tid=%s),该条本轮跳过",
                        uin, ",".join(sorted(unmatched_tids)),
                    )
                await asyncio.sleep(2.0)  # 好友间请求间隔(防风控,Maizone 保守默认同款)
            if added_total:
                self.ctx.logger.info("QQ空间新动态入队 %d 条(统一时间线发现 %d 条)", added_total, len(new_items))
            await self._qzone_pump()
        finally:
            self._qzone_poll_running = False

    async def _qzone_poll_feeds_legacy(self) -> None:
        """旧逐好友浏览路径(M3 前架构):好友列表→每人 get_user_feeds(num=3)。

        仅作发现层失败的回退路径(窗口守卫已由 _qzone_poll_feeds 完成);保留
        OneBot 好友列表通道——统一时间线端点不可用时仍可逛空间。
        """

        # 拉取架构(联调修正 2026-08-30):好友列表走 adapter OneBot API,
        # 逐好友拉最近说说(msglist_v6 为指定用户接口);好友间固定间隔防风控
        friends = await self._qzone_friend_list()
        if not friends:
            self.ctx.logger.warning("QQ空间好友列表为空或获取失败,本轮跳过")
            return
        added_total = 0
        for friend in friends:
            try:
                feeds = await self.qzone_client.get_user_feeds(
                    target_uin=friend["user_id"], nickname=friend["nickname"], num=3
                )
            except QzoneAuthError:
                # 登录态失效:立即作废 cookie 缓存(下轮重取),本轮终止(联调缺陷#7 自愈链)
                self.qzone_cookie.invalidate()
                self.ctx.logger.warning("QQ空间登录态失效(code=-3000/-10005),cookie 已作废,下轮重取")
                return
            except Exception:
                # 单个好友失败不中止整轮(逐人隔离,显式告警)
                self.ctx.logger.exception("QQ空间说说拉取失败(uin=%s),该好友本轮跳过", friend["user_id"])
                continue
            added = [
                f for f in feeds
                if self.qzone_seen.mark_queued(
                    f.tid, abstime=f.abstime, author_uin=f.uin, summary=f.content[:60],
                    author_nickname=friend["nickname"],
                )
            ]
            if added:
                self.qzone_injector.enqueue(added)
                added_total += len(added)
            await asyncio.sleep(2.0)  # 好友间请求间隔(防风控,Maizone 保守默认同款)
        if added_total:
            self.ctx.logger.info("QQ空间新动态入队 %d 条(好友 %d 人)", added_total, len(friends))
        await self._qzone_pump()

    async def _qzone_friend_list(self) -> list[dict]:
        """好友列表(adapter OneBot 通道,信封容忍解析;失败告警返回空)。"""

        try:
            result = await self.ctx.api.call("adapter.napcat.account.get_friend_list", no_cache=False)
        except Exception:
            self.ctx.logger.exception("QQ空间好友列表获取失败(adapter.napcat.account.get_friend_list)")
            return []
        return parse_friend_list(result)

    async def _qzone_data_prune(self) -> None:
        """qzone 数据保留期清理(深度审查 D-1):comment_seen 30 天+seen 表 7 天。

        qzone_comments/qzone_feeds 无限增长会拖慢每轮判重与通知轮询反查;
        seen 保留 7 天(recent_seen 只需 summary_days≤3,7 天留余量);
        queued 行不动——回退未读语义由窗口收泵的 revert_pending 负责,prune 不越权。
        """

        now = datetime.now()
        pruned_comments = self.qzone_comment_seen.prune(30, now)
        try:
            self.store.execute(
                "DELETE FROM qzone_feeds WHERE state = 'seen' AND injected_at < ?",
                ((now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"),),
            )
        except Exception:
            self.ctx.logger.exception("qzone_feeds 清理失败")
            return
        if pruned_comments:
            self.ctx.logger.info("QQ空间数据清理:评论去重 %d 行,seen 保留 7 天", pruned_comments)

    async def _qzone_pump(self) -> None:
        """串行注入:超时兜底 → 取队首 → 构造(通知=build_notify_message 带 reply 段
        /浏览=下载图片+build_feed_message)→ route_message → mark_seen。

        泵有两个入口(调度 tick 直接 await 与轮完成信号后台任务),整体持锁串行:
        next_to_inject(弹出)到 mark_injected(置 awaiting)之间有图片下载/route_message
        等 await 点,无锁时重叠入口会同时弹出第二条,破坏「一动态一轮」。
        """

        async with self._qzone_pump_lock:
            # 泵门控不卡浏览窗口(M3-r2):通知(P1)推送语义任何时刻可注入,
            # 窗口外浏览动态由 next_to_inject 的窗口判定拦截
            if not self._qzone_available:
                return
            if self.config.sleep.enabled and self.sleep.is_sleeping():
                return  # 深度审查 C-1:入睡后在途泵静默退出——注入的消息会被睡眠拦截链拦进回顾缓冲(白注入)
            now = time.monotonic()
            if self.qzone_injector.awaiting_timed_out(now):
                self.ctx.logger.warning("QQ空间注入等待轮完成超时(tid=%s),强制推进", self.qzone_injector.awaiting_tid)
                self.qzone_injector.force_release(now)
            feed = self.qzone_injector.next_to_inject(now)
            if feed is None:
                return
            if feed.source == "notify":
                # 通知走专用构造(联调修正):带 reply 段引用**原说说**的注入消息
                # (napcat quote 式上下文关联)——引用目标经 seen_store.get_message_id
                # 查原说说注入时记录的 message_id;原说说未注入过(窗口外通知/已被
                # 7 天清理/旧库未记录)时查无 id → reply 段省略,回退纯文本不静默臆造
                reply_target_id = self.qzone_seen.get_message_id(feed.origin_tid)
                if feed.origin_tid and not reply_target_id:
                    self.ctx.logger.debug(
                        "QQ空间通知原说说无注入记录(origin_tid=%s),reply 段省略", feed.origin_tid
                    )
                msg = build_notify_message(
                    feed, group_id=self.config.qzone.virtual_group_id,
                    group_name=self.config.qzone.virtual_group_name, now_epoch=time.time(),
                    reply_target_id=reply_target_id, reply_target_sender=feed.origin_sender,
                )
            else:
                images: list[tuple[str, bytes]] = []
                for url in feed.image_urls:
                    try:
                        data = await self.qzone_client.download_image(url)
                    except Exception:
                        # 单图异常降级为占位,不中止该条动态的注入(网络抖动等瞬态)
                        self.ctx.logger.exception("QQ空间图片下载异常(%s),以占位注入", url)
                        data = None
                    if data is not None:
                        images.append((url, data))
                # RPC 帧预算(用户裁定 2026-08-31):体积治理=压缩而非拒收,压到帧限内
                images = fit_images_to_rpc_budget(
                    images,
                    on_drop=lambda u: self.ctx.logger.warning("QQ空间图片压缩后仍超 RPC 帧预算,丢弃保帧: %s", u),
                )
                self._qzone_seq += 1
                msg = build_feed_message(
                    feed, seq=self._qzone_seq, group_id=self.config.qzone.virtual_group_id,
                    group_name=self.config.qzone.virtual_group_name, images=images,
                    now_epoch=time.time(),
                )
            try:
                # SDK route_message 返回 bool accepted(不抛异常):宿主 adapter policy
                # 或网关状态拒绝时返回 False,不得当成功继续标记
                accepted = await self.ctx.gateway.route_message(QZONE_GATEWAY_NAME, msg)
            except Exception:
                self.ctx.logger.exception("QQ空间动态注入失败(tid=%s),本轮跳过", feed.tid)
                # 深度审查 B-4+A-N1:通知项注入未成功不永久丢失,但重试有上限(见
                # _qzone_notify_retry_backoff);浏览动态无此问题(queued 行由窗口
                # 尾 revert_pending 回退未读)
                self._qzone_notify_retry_backoff(feed)
                return
            if not accepted:
                # 拒绝时不 mark_injected/mark_seen:feed 已从内存队列弹出,但 DB 仍
                # queued,窗口尾 revert_pending 会回退未读——不丢数据,下窗口可重试
                self.ctx.logger.warning("QQ空间动态注入被宿主拒绝(tid=%s,adapter policy 或网关状态),跳过且不标记已见", feed.tid)
                # 深度审查 B-4+A-N1:通知项被拒不永久丢失,重试上限内回退待重检
                self._qzone_notify_retry_backoff(feed)
                return
            self.qzone_injector.mark_injected(feed.tid, time.monotonic())
            # message_id 随 mark_seen 落库:后续通知的 reply 段据此引用本条注入消息
            self.qzone_seen.mark_seen(feed.tid, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), msg["message_id"])
            # 工具驱动架构:登记 FeedContext(替代意图绑定,工具按 tid 解析目标)。
            # 键=真实说说 tid(通知项用 origin_tid——消息尾部锚展示真实 tid,合成 tid
            # 模型不可见);owner=说说主人(浏览=作者;通知源B=好友;源A=bot 自己);
            # commenter/comment_tid/comment_uin=通知场景的评论者与主评论二元组素材
            bot_uin = str(self.config.favorability.bot_user_id or "").strip()
            self._qzone_registry.register(FeedContext(
                tid=feed.origin_tid or feed.tid,
                owner_uin=(feed.friend_uin or bot_uin) if feed.source == "notify" else feed.uin,
                owner_nickname=feed.nickname,
                commenter_uin=feed.uin if feed.source == "notify" else "",
                commenter_nickname=feed.nickname if feed.source == "notify" else "",
                comment_tid=feed.comment_tid,
                comment_uin=feed.comment_uin,
                kind=feed.source,
            ))
            self.ctx.logger.info("QQ空间动态已注入(tid=%s,作者=%s)", feed.tid, feed.nickname)

    def _qzone_notify_retry_backoff(self, feed: FeedItem) -> None:
        """通知项注入失败(被拒/异常)的回退决策(深度审查 B-4 + A-N1)。

        B-4:回退去重键令下轮通知轮询重新发现,通知不因一次拒绝永久丢失。
        A-N1:重试有上限——revert 为软回退(qzone_comments 行保留),note_retry
        的累计跨「回退→重发现」循环存活;满 QZONE_NOTIFY_MAX_RETRIES 次仍失败
        则保留登记放弃(is_new 恒 False 跳过),防宿主持续拒绝时同一通知每
        轮询周期(120s)无限重注入。浏览动态(source=feed)不走本路径。"""

        if feed.source != "notify" or not feed.dedup_key:
            return
        retries = self.qzone_comment_seen.note_retry(feed.dedup_key)
        if retries >= QZONE_NOTIFY_MAX_RETRIES:
            self.ctx.logger.warning(
                "QQ空间通知重试 %d 次仍被拒(dedup_key=%.40s),放弃不再重试", retries, feed.dedup_key
            )
            return  # 不 revert:保留登记,下轮 is_new 判 False 跳过
        self.ctx.logger.info("QQ空间通知被拒,回退去重键待下轮重试(第 %d 次)", retries)
        self.qzone_comment_seen.revert(feed.dedup_key)

    async def _qzone_notify_poll_tick(self) -> None:
        """统一通知轮询触发器(深度审查 A-2):防重入后派发后台扫描,立即返回。

        源A/B 逐好友 2s sleep+HTTP 与泵注入都是长 IO,移入 _qzone_notify_scan
        后台任务执行,不阻塞调度器 60s tick;守卫随迁(单一事实源)。
        """

        if self._qzone_notify_running:
            return  # 上一轮后台扫描还在跑,跳过(防重入)
        self._qzone_notify_running = True
        self._spawn_background_task(self._qzone_notify_scan())

    async def _qzone_notify_scan(self) -> None:
        """统一通知扫描(原 _qzone_notify_poll_tick 主体,始终运行,醒着即可;M2.1 替代旧评论轮询)。

        双源检测:源A=自己说说下的新评论;源B=自己在他人说说下的评论收到的
        新楼中楼回复(list_3)。通知构造为 FeedItem(source="notify")走 P1
        优先级队列(插队于浏览动态之前),泵注入成功后登记 FeedContext 供
        qzone_comment/qzone_reply 解析目标(工具驱动,替代意图路由)。
        通知注入走 build_notify_message:reply 段引用原说说注入消息承载上下文
        (target_message_content=原说说正文前 60 字),正文自然可读并带参数独立
        尾行(可读性优化 2026-09-01)——源A「评论了你的说说:…」换行
        「〔说说ID=xx 评论ID=xx 评论者QQ=xx 评论于(今天HH:MM)〕」、源B
        「回复了你的评论「{bot原评论前20字}」:…」换行同款参数行(action=回复),
        评论内 @{uin,nick} 解析为 @昵称;参数行供模型照抄调用工具(映射由场景
        prompt 解释),不重复引用原文,正文不带发布时间前缀——互动新旧由参数行
        动作时间承载(create_time 缺失则省略该段,不编造时间)。
        """

        try:
            if not self._qzone_available:
                self.ctx.logger.debug("通知轮询跳过:QQ空间模块不可用(自检未过/热重载停用)")
                return
            if not self.config.qzone.comment_poll_enabled:
                self.ctx.logger.debug("通知轮询跳过:comment_poll_enabled 关闭")
                return
            if self.config.sleep.enabled and self.sleep.is_sleeping():
                self.ctx.logger.debug("通知轮询跳过:睡眠中(绝对静默,spec §2.6)")
                return
            if self.qzone_injector.awaiting_feed is not None:
                self.ctx.logger.debug("通知轮询跳过:上一条还在等回复(awaiting 占用,不叠加)")
                return  # 上一条通知/动态还在等 bot 回复,不叠加(下轮再取)
            bot_uin = str(self.config.favorability.bot_user_id or "").strip()
            if not bot_uin:
                return  # 写路径身份缺失(on_load 自检已停用模块,防御性再判)
            now_epoch = time.time()
            # 新鲜度截断(终审 I2 同款语义):早于 summary_days 的过旧通知不注入;
            # create_time 为 epoch 秒字符串按数值比较,不可解析不截断(保守注入)
            stale_before = now_epoch - max(self.config.qzone.summary_days, 1) * 86400
            notifications: list[FeedItem] = []

            # ---- 源A:自己说说下的新评论 ----
            try:
                comments, ctx = await self.qzone_client.get_own_feed_comments(bot_uin=bot_uin, num=10)
            except QzoneAuthError:
                # 与浏览轮询同款自愈链(联调缺陷#7):作废 cookie,下轮重取
                self.qzone_cookie.invalidate()
                self.ctx.logger.warning("QQ空间登录态失效(通知轮询源A),cookie 已作废,下轮重取")
                return
            except Exception:
                self.ctx.logger.exception("QQ空间通知轮询源A失败,本轮跳过")
                return
            for feed_tid, items in comments.items():
                for c in items:
                    if not c.comment_tid:
                        continue  # 空 comment_tid 的畸形条目:跳过(防空 tid 畸形请求,T11 审查遗留)
                    if str(c.uin) == bot_uin:
                        # 自己发出的评论:重见即登记(幂等,note_bot_comment 独立键空间;
                        # 自己说说的主人即 bot,friend_uin 记 bot_uin——源B 反查会因此
                        # 跳过自己,自己说说已在源A覆盖),不注入
                        self.qzone_comment_seen.note_bot_comment(
                            feed_tid, bot_uin, c.content, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        )
                        continue
                    # 先 is_new 判新(发现即登记,store 契约)再判旧跳过——已登记则下轮
                    # 判重不再重扫
                    dedup_key = f"{feed_tid}:{c.comment_tid}:{c.uin}"
                    if not self.qzone_comment_seen.is_new(dedup_key):
                        continue
                    try:
                        comment_epoch = float(str(c.create_time or "").strip())
                    except ValueError:
                        comment_epoch = 0.0
                    if comment_epoch > 0 and comment_epoch < stale_before:
                        self.ctx.logger.info(
                            "QQ空间评论过旧跳过(create_time=%s,昵称=%s)", c.create_time, c.nickname
                        )
                        continue
                    feed_summary = (ctx.get(feed_tid) or "(无文字)")[:30]
                    notifications.append(FeedItem(
                        tid=f"notify_comment_{feed_tid}_{c.comment_tid}",
                        abstime=c.create_time, uin=str(c.uin), nickname=c.nickname,
                        # 正文=自然可读+参数独立尾行(可读性优化 2026-09-01):评论内
                        # @{uin,nick} 解析为 @昵称;〔〕参数行(说说ID/评论ID/评论者QQ/
                        # 评论于时间)供模型照抄调用 qzone_comment/qzone_reply,与工具参数名
                        # (feed_id/comment_id/at_user_id)的映射由场景 prompt 解释;
                        # 动作时间让 bot 分得清互动新旧(Task 3,缺失省略不编造)
                        content=(
                            f"评论了你的说说:{parse_qzone_mentions(c.content, bot_uin=bot_uin)}\n"
                            + format_comment_param_line(
                                feed_tid=feed_tid, comment_tid=c.comment_tid, commenter_uin=str(c.uin),
                                action="评论", create_time=str(c.create_time or ""), now_epoch=now_epoch,
                            )
                        ),
                        source="notify", dedup_key=dedup_key,
                        # reply 段关联原说说(联调修正):origin_* 供泵构造引用段,
                        # 引用内容=原说说正文前 60 字(截断统一在 messages 构造层,
                        # 源A:原说说作者=bot 自己)
                        origin_tid=feed_tid, origin_content=ctx.get(feed_tid) or "(无文字)",
                        origin_sender=bot_uin,
                        # 楼中楼二元组素材(qzone_reply):主评论 tid+主评论作者
                        # (源A=评论好友的评论,作者=好友;源B=bot 的评论,作者=bot)
                        comment_tid=c.comment_tid, comment_uin=str(c.uin),
                    ))
                    self.qzone_comment_seen.fav_event(
                        str(c.uin), "COMMENT", f"{c.nickname} 评论了你的说说「{feed_summary}」: {c.content[:40]}"
                    )
                    if len(notifications) >= 3:  # 单轮上限,防通知风暴
                        break
                if len(notifications) >= 3:
                    break

            # ---- 源B:自己在他人说说下的评论收到的新回复(list_3) ----
            # M3 重构:搭统一时间线便车——只对「发现层显示有新活动+bot 评论过该好友」
            # 的说说拉评论,不再逐好友全量轮询(发现层无交集时零源B拉取)
            if len(notifications) < 3:
                # 名单先行(M3 终审优化):本地反查零 HTTP,名单空则跳过发现层调用省 API
                try:
                    commented_friends = set(self.qzone_comment_seen.bot_commented_friends(days=30))
                except Exception:
                    # 反查失败显式告警后按空处理(源B 仅是增量来源,不阻断源A 已得通知)
                    self.ctx.logger.exception("QQ空间通知轮询源B好友反查失败,本轮跳过源B")
                    commented_friends = set()
                discoveries_b: list[FeedDiscovery] = []
                if commented_friends:
                    try:
                        discoveries_b = await self.qzone_client.get_unified_timeline(count=20)
                    except QzoneAuthError:
                        # 登录态失效自愈链(与浏览流同款):作废 cookie 下轮重取;不 return
                        # ——源B仅是增量来源,终止本轮源B即可,源A已得通知照常入队
                        self.qzone_cookie.invalidate()
                        self.ctx.logger.warning("QQ空间通知源B登录态失效,cookie 已作废")
                    except Exception:
                        # 发现层失败不阻断源A已得通知(源B仅是增量来源),显式告警
                        self.ctx.logger.exception("QQ空间通知源B发现层失败,本轮跳过源B")
                # 交叉:发现层作者 ∈ 被评论好友 → 该好友有新活动(时间线序去重,
                # 排除 bot 自己——自己说说已在源A覆盖)
                active_commented: dict[str, None] = {}
                for d in discoveries_b:
                    if d.uin in commented_friends and d.uin != bot_uin:
                        active_commented.setdefault(d.uin, None)
                for friend_uin in active_commented:
                    if len(notifications) >= 3:
                        break
                    await asyncio.sleep(2.0)  # 好友前固定间隔(终审 I2 防风控,与浏览流同款 2 秒)
                    try:
                        raw = await self.qzone_client.get_user_feeds_raw(target_uin=friend_uin, num=10)
                    except QzoneAuthError:
                        # 登录态失效对源B所有好友同源:作废 cookie 并终止源B(下轮重取)
                        self.qzone_cookie.invalidate()
                        self.ctx.logger.warning(
                            "QQ空间登录态失效(通知轮询源B,好友 %s),cookie 已作废,本轮终止源B", friend_uin
                        )
                        break
                    except Exception:
                        self.ctx.logger.exception("QQ空间通知轮询源B拉取失败(好友 %s),该好友跳过", friend_uin)
                        continue
                    for r in parse_feed_replies(raw, bot_uin=bot_uin):
                        if not r.feed_tid:
                            continue  # 批①遗留:空 feed_tid 的 ReplyItem 过滤
                        if not r.reply_tid:
                            continue  # 空 reply_tid 的畸形回复:跳过(防空 tid 畸形请求,T11 审查遗留)
                        key = f"{r.feed_tid}:{r.parent_comment_tid}:reply:{r.reply_tid}"
                        if not self.qzone_comment_seen.is_new(key):
                            continue
                        try:
                            reply_epoch = float(str(r.create_time or "").strip())
                        except ValueError:
                            reply_epoch = 0.0
                        if reply_epoch > 0 and reply_epoch < stale_before:
                            self.ctx.logger.info(
                                "QQ空间楼中楼回复过旧跳过(create_time=%s,昵称=%s)", r.create_time, r.nickname
                            )
                            continue
                        # 正文=自然可读+楼中楼上下文+参数独立尾行(可读性优化
                        # 2026-09-01):引用 bot 原评论前 20 字(缺内容回退
                        # 「你之前的评论」),@{uin,nick} 解析为 @昵称;〔〕参数行
                        # 供模型照抄调用 qzone_reply(说说ID/主评论ID/回复者QQ/
                        # 回复于时间,create_time 缺失省略不编造);
                        # 楼中楼二元组的 commentUin=bot 自己(主评论作者是 bot)
                        bot_ctx = r.parent_comment_content[:20] if r.parent_comment_content else "你之前的评论"
                        notifications.append(FeedItem(
                            tid=f"notify_reply_{r.feed_tid}_{r.reply_tid}",
                            abstime=r.create_time, uin=str(r.uin), nickname=r.nickname,
                            content=(
                                f"回复了你的评论「{bot_ctx}」:"
                                f"{parse_qzone_mentions(r.content, bot_uin=bot_uin)}\n"
                                + format_comment_param_line(
                                    feed_tid=r.feed_tid, comment_tid=r.parent_comment_tid, commenter_uin=str(r.uin),
                                    action="回复", create_time=str(r.create_time or ""), now_epoch=now_epoch,
                                )
                            ),
                            source="notify", friend_uin=friend_uin, dedup_key=key,
                            # reply 段关联原说说(源B:原说说作者=好友/说说主人);
                            # 引用内容=原说说正文前 60 字(截断统一在 messages 构造层)
                            origin_tid=r.feed_tid, origin_content=r.feed_content,
                            origin_sender=friend_uin,
                            # 楼中楼二元组素材(qzone_reply):主评论 tid+主评论作者
                            # (源B=被回复的 bot 评论,作者=bot;@ 目标另由 commenter 承载)
                            comment_tid=r.parent_comment_tid, comment_uin=bot_uin,
                        ))
                        self.qzone_comment_seen.fav_event(
                            str(r.uin), "COMMENT", f"{r.nickname} 回复了你的评论: {r.content[:40]}"
                        )
                        if len(notifications) >= 3:
                            break

            if notifications:
                added = self.qzone_injector.enqueue_priority(notifications)
                self.ctx.logger.info("QQ空间通知入队 %d 条(源A+B,P1 插队)", added)
                # 泵优先取 P1(推送语义,任何时刻可注入);浏览动态仅窗口内注入
                await self._qzone_pump()
        finally:
            self._qzone_notify_running = False

    # ---------- 命令 ----------

    @Command("记一下", description="记一条短时备忘", pattern=r"^/记一下\s+(?P<content>.+)$", aliases=["/备忘"])
    async def cmd_memo(self, content: str = "", stream_id: str = "", user_id: str = "", **kwargs: Any) -> str:
        del kwargs
        if not self.config.plugin.enabled or not self.config.memo.command_enabled:
            return "备忘命令未启用。"
        if len(content.strip()) > self.config.memo.entry_max_chars:
            return f"备忘太长啦(>{self.config.memo.entry_max_chars} 字符),请精简后再发～"
        ok, msg = self.memo.write(content, stream_id, user_id, None)
        return msg if ok else f"备忘写入失败:{msg}"

    # ---------- Hook:主链路注入 ----------

    @HookHandler("maisaka.planner.before_request", name="catsitate_inject", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def inject_blocks(self, **kwargs: Any) -> dict[str, Any]:
        """注入块前插 system 之后、历史之前(规格 §4.1);失败仅记录日志不阻塞。"""

        if not self.config.plugin.enabled or not self.config.inject.enabled:
            self.ctx.logger.debug("注入 hook 跳过:开关关闭")
            return {"action": "continue", "modified_kwargs": kwargs}
        self.ctx.logger.debug("注入 hook 触发: keys=%s", sorted(str(k) for k in kwargs.keys()))
        try:
            blocks = await self._build_inject_blocks(kwargs)
            self.ctx.logger.debug("注入块构造: %s", [b.module for b in blocks])
            messages = self._messages_from_kwargs(kwargs)
            if messages is None:
                self.ctx.logger.debug("注入跳过: messages 取不到")
                return {"action": "continue", "modified_kwargs": kwargs}
            stream_id = str(kwargs.get("session_id") or "")
            is_qzone_session = stream_id in self._qzone_session_id_set() and self._qzone_available
            new_kwargs = {**kwargs}
            if is_qzone_session:
                messages, scene_status = apply_scene_surgery(messages, await self._qzone_group_prompt())
                if scene_status == "empty_config":
                    self._qzone_warn_once("scene_empty", SCENE_EMPTY_CONFIG_WARNING)
                elif scene_status == "miss":
                    self._qzone_warn_once("scene_miss", SCENE_MISS_WARNING)
                new_kwargs = {**new_kwargs, self._MESSAGES_KEY: messages}
            defs = kwargs.get("tool_definitions")
            if isinstance(defs, list):
                # 双向隔离(T11/终审 I4):qzone 流走白名单(硬门控不随配置放松);
                # 非 qzone 流剥离 qzone_* 工具(qzone_like 等),防模型误调
                filtered = filter_qzone_tools_for_stream(
                    [d for d in defs if isinstance(d, dict)],
                    is_qzone=is_qzone_session, whitelist=self.config.qzone.tool_whitelist,
                )
                new_kwargs = {**new_kwargs, "tool_definitions": filtered}
            rendered = self.assembler.render(blocks)
            if not rendered:
                self.ctx.logger.debug("注入跳过: render 结果为空(blocks=%d)", len(blocks))
                return {"action": "continue", "modified_kwargs": kwargs}
            # spike ② 实测:items 须为合法快照格式,朴素 dict 会被主程序拒绝——render 输出转快照后插入
            rendered = [self._to_snapshot_item(m["content"]) for m in rendered]
            insert_at = self._system_tail_index(messages)
            new_messages = messages[:insert_at] + rendered + messages[insert_at:]
            new_kwargs = {**new_kwargs, self._MESSAGES_KEY: new_messages}
            self.ctx.logger.debug("注入完成: 插入 %d 条(system 尾 %d/总 %d)", len(rendered), insert_at, len(messages))
            return {"action": "continue", "modified_kwargs": new_kwargs}
        except Exception:
            self.ctx.logger.exception("注入块构造失败,本轮跳过注入")
            return {"action": "continue", "modified_kwargs": kwargs}

    # ---------- Hook:入站(好感度计数) ----------

    @HookHandler("chat.receive.after_process", name="catsitate_fav_count", mode=HookMode.OBSERVE)
    async def fav_count(self, **kwargs: Any) -> None:
        if self.config.sleep.enabled and self.sleep.is_sleeping():
            return  # 睡眠期消息不得计数(绝对静默;睡眠模块关闭时计数不暂停,审查 M14)
        if not self.config.plugin.enabled or not self.config.favorability.enabled:
            return
        msg = kwargs.get("message")
        if not isinstance(msg, dict):
            return
        # 通知类消息(戳一戳等)不参与好感度计数(审查 Minor#7)
        if msg.get("is_notify"):
            return
        # 三期豁免(spec §2.19②):虚拟流注入消息不计好感度(好友发说说≠与 bot 互动;
        # 空间互动走 M2 显式事件路径);顺带收集虚拟流 session 供晚安/白名单豁免
        if is_qzone_message(msg):
            sid = str(msg.get("session_id") or "")
            if sid:
                self._qzone_session_ids.add(sid)
            return
        # spike ③ 实测:user/stream 在 message 内(user_info 与 session_id;user_id 字段名以实机联调为准)
        msg_info = msg.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        user_id = str(user_info.get("user_id") or user_info.get("sender_id") or "")
        stream_id = str(msg.get("session_id") or "")
        if not user_id or not stream_id:
            return
        # §3.10 取数点:记录流→最近真实说话人(群聊 memo_write 工具 user_id 空时兜底;
        # 纯内存映射,重启丢失可接受——_resolve_speaker 的 get_recent 回退仍在)
        self._last_speaker_map[stream_id] = user_id
        # 计数与触发判定按人:先 bump 活跃账本,再判定(check_trigger 不再内部计数,审查 ⚠️ 裁决)
        self.fav_engine.count_message(user_id, stream_id)
        trigger = self.fav_engine.check_trigger(user_id)
        if trigger == "early":
            self._spawn_background_task(self._settle_and_log(user_id, kind="early"))

    # ---------- Hook:睡眠拦截与晚安判定 ----------

    @HookHandler("chat.receive.before_process", name="catsitate_sleep_gate", mode=HookMode.BLOCKING, order=HookOrder.EARLY)
    async def sleep_gate(self, **kwargs: Any) -> dict[str, Any]:
        """睡眠绝对静默:拦截一切入站消息(含命令),记录进回顾缓冲。"""

        if not self.config.plugin.enabled or not self.config.sleep.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        if not self.sleep.is_sleeping():
            self._last_activity_ts = datetime.now().timestamp()
            return {"action": "continue", "modified_kwargs": kwargs}
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            msg_info = msg.get("message_info") or {}
            ui = msg_info.get("user_info") or {}
            stream_id = str(msg.get("session_id") or "")
            logger.debug("睡眠拦截:消息入回顾缓冲(stream=%s,当前共 %d 条)", stream_id, len(self._sleep_review_buffer) + 1)
            self._sleep_review_buffer.append({
                "stream_id": stream_id,
                "user_id": str(ui.get("user_id") or ""),
                "nickname": str(ui.get("user_nickname") or ""),
                "text": str(msg.get("processed_plain_text") or ""),
                "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            })
            self._sleep_review_buffer_snapshot.save({"messages": self._sleep_review_buffer})
        return {"action": "abort", "modified_kwargs": kwargs}

    @HookHandler("maisaka.replyer.after_response", name="catsitate_goodnight", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def goodnight_check(self, **kwargs: Any) -> dict[str, Any]:
        """晚安短句判定(可入睡时间内):SLEEP → 入睡并触发生成次日日程。"""

        if not self.config.plugin.enabled or not self.config.sleep.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        self._last_activity_ts = datetime.now().timestamp()  # 任何出站回复都算活动(含空间评论,与一二期语义一致)
        # 三期豁免(spec §2.19①):虚拟流的评论文本不进晚安判定(防深夜短评论触发全局入睡)
        # 注意:活动计时刷新在豁免之前——空间活动同样延迟静默入睡(用户裁定 2026-08-31)
        if str(kwargs.get("session_id") or "") in self._qzone_session_id_set():
            return {"action": "continue", "modified_kwargs": kwargs}
        if self.sleep.is_sleeping():
            return {"action": "continue", "modified_kwargs": kwargs}
        # 晚安判定仅在睡眠窗口内有效(可入睡时间,联调裁定 Q2),与静默开关无关
        _win = current_window(self._schedule_data, datetime.now().strftime("%Y-%m-%dT%H:%M"))
        if not _win or _win.get("kind") != "sleep":
            return {"action": "continue", "modified_kwargs": kwargs}
        text = str(kwargs.get("response") or "")
        if not is_goodnight_utterance(text):
            return {"action": "continue", "modified_kwargs": kwargs}
        # 短句为变量尾(stable_ctx 纪律);判定器为轻量任务,固定 memory(与一期 msg_react 默认一致)
        messages, _ = build_side_prompt(
            "sleep_confirm", [], [f"待判定晚安短句:{text}"]
        )
        try:
            result = await self._side_llm_call(messages, "memory", "sleep_confirm")
        except Exception as exc:  # noqa: BLE001
            # 失败显式日志并跳过本轮(与哨兵层同款纪律,审查 M9);仅记异常类型防 PII
            self.ctx.logger.warning("晚安判定 LLM 调用异常(%s),本轮不入睡", type(exc).__name__)
            return {"action": "continue", "modified_kwargs": kwargs}
        if not isinstance(result, dict) or not result.get("success"):
            # 不落响应原文(安全复审):仅记失败形态
            detail = f"success={result.get('success')}" if isinstance(result, dict) else f"结果类型={type(result).__name__}"
            self.ctx.logger.warning("晚安判定 LLM 失败,本轮不入睡:%s", detail)
            return {"action": "continue", "modified_kwargs": kwargs}
        verdict, _ = parse_sleep_confirm_response(str(result.get("response") or ""))
        if verdict == "SLEEP":
            await self._enter_sleep()
        return {"action": "continue", "modified_kwargs": kwargs}

    async def _enter_sleep(self) -> None:
        """入睡:计算 clamp 醒来时刻,状态落盘,触发入睡任务(次日日程生成+日记生成)。"""

        if self.sleep.is_sleeping():
            return  # 已睡幂等:晚安判定 await 交错期间不得二次入睡/二次生成(审查 I-3)
        now = datetime.now()
        # 计划醒来时刻 = 日程中睡眠窗口的 end(与当前所处窗口无关)
        sleep_win = next(
            (w for w in (self._schedule_data.get("windows") or []) if w.get("kind") == "sleep"), None
        )
        planned_wake = sleep_win.get("end") if sleep_win else (now + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M")
        if len(planned_wake) == 16:
            planned_wake += ":00"  # SleepManager 统一秒格式 %Y-%m-%dT%H:%M:%S
        wake_at = self.sleep.clamp_wake_time(now.strftime("%Y-%m-%dT%H:%M:%S"), planned_wake)
        self.sleep.enter_sleep(now=lambda: now, wake_at=wake_at)
        self._sleep_window_settled = str(sleep_win.get("end") or "") if sleep_win else ""  # 入睡已执行入睡任务,窗口终点不再补执行(Q1)
        self.ctx.logger.info("已入睡:醒来 %s", wake_at)
        self._spawn_background_task(self._generate_tomorrow_schedule())
        # 日记与日程同属入睡任务:旁路 LLM 与发布 API 均不经消息链,睡眠期可执行
        self._spawn_background_task(self._generate_and_publish_diary())

    # ---------- Hook:reply 补传与哨兵 ----------

    @HookHandler("maisaka.planner.after_response", name="catsitate_reply_backfill", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def reply_backfill(self, **kwargs: Any) -> dict[str, Any]:
        """规则层补传(规格 §4.7):三条件触发,零成本,不改动其它工具调用。"""

        cfg = self.config.reply_guard
        if not self.config.plugin.enabled or not cfg.enabled or not cfg.context_backfill_enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        output_items = self._output_items(kwargs)
        if not output_items:
            return {"action": "continue", "modified_kwargs": kwargs}
        called_tools = self._called_tools(kwargs)
        reasoning = self._reasoning_from_items(output_items)
        tool_results = self._context_tool_results(output_items, called_tools)
        if not tool_results:
            logger.debug("reply 补传跳过:本轮无上下文工具结果(called=%s)", called_tools)
            return {"action": "continue", "modified_kwargs": kwargs}
        new_items = backfill_reply_items(output_items, tool_results, called_tools, reasoning)
        if new_items is output_items:
            logger.debug("reply 补传跳过:三条件不齐(reasoning 非空=%s)", bool(reasoning.strip()))
            return {"action": "continue", "modified_kwargs": kwargs}
        new_kwargs = {**kwargs, self._OUTPUT_ITEMS_KEY: new_items}
        self.ctx.logger.info("reply 补传:%s", [t.get("tool_name") for t in new_items if t.get("tool_name") == "reply"])
        return {"action": "continue", "modified_kwargs": new_kwargs}

    @HookHandler("maisaka.replyer.after_response", name="catsitate_sentinel", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def sentinel_check(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 哨兵层(默认关,规格 §4.7);撤回能力以 spike ④ 结论为准,不能则仅日志。"""

        cfg = self.config.reply_guard
        if not self.config.plugin.enabled or not cfg.enabled or not cfg.sentinel_enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        reply_text = str(kwargs.get("response") or "")
        if not reply_text.strip():
            return {"action": "continue", "modified_kwargs": kwargs}
        persona = await self._persona()
        chat_context = await self._recent_context_text(str(kwargs.get("session_id") or ""), limit=10)
        messages, _ = build_sentinel_prompt(persona, reply_text, chat_context)
        result = await self._side_llm_call(messages, cfg.sentinel_model, "sentinel", cfg.sentinel_timeout_ms)
        if not result.get("success"):
            self.ctx.logger.warning("哨兵层 LLM 调用失败,放行回复:%s", result.get("response", "")[:200])
            return {"action": "continue", "modified_kwargs": kwargs}
        should_send, reason = parse_sentinel_response(str(result.get("response") or ""))
        if should_send is None or should_send:
            self.ctx.logger.info("哨兵判定:放行回复")
            return {"action": "continue", "modified_kwargs": kwargs}
        self.ctx.logger.warning("哨兵判定:撤回回复:%s", reason)
        # 撤回动作(spike ④ 验证后实现:删除待发送项或调用撤回 API);当前先日志
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler("maisaka.replyer.before_model_request", name="catsitate_qzone_replyer_scene", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def qzone_replyer_scene(self, **kwargs: Any) -> dict[str, Any]:
        """replyer 侧场景替换(spec §2.11):before_request 不带 items,必须挂 before_model_request。"""

        if not self._qzone_available or str(kwargs.get("session_id") or "") not in self._qzone_session_id_set():
            return {"action": "continue", "modified_kwargs": kwargs}
        items = kwargs.get(self._MESSAGES_KEY)
        if not isinstance(items, list):
            return {"action": "continue", "modified_kwargs": kwargs}
        new_items, status = apply_scene_surgery(items, await self._qzone_group_prompt())
        if status == "empty_config":
            self._qzone_warn_once("scene_empty", SCENE_EMPTY_CONFIG_WARNING)
        elif status == "miss":
            self._qzone_warn_once("scene_miss", SCENE_MISS_WARNING)
        if new_items is items:
            return {"action": "continue", "modified_kwargs": kwargs}
        # item_schema_version 等原键必须展平保留(丢失=修改被主程序静默丢弃,plugin.py:472 已实测此坑)
        return {"action": "continue", "modified_kwargs": {**kwargs, self._MESSAGES_KEY: new_items}}

    @HookHandler("maisaka.planner.after_response", name="catsitate_qzone_turn", mode=HookMode.OBSERVE, order=HookOrder.LATE)
    async def qzone_turn_signal(self, **kwargs: Any) -> None:
        """轮完成信号(spec §2.4):无 tool_calls 的 planner 响应=本轮不再有出站,释放注入泵。"""

        if not self._qzone_available or str(kwargs.get("session_id") or "") not in self._qzone_session_id_set():
            return
        called = self._called_tools(kwargs)
        if called:
            if "wait" in called:
                self.qzone_injector.on_wait_state(time.monotonic())
            return
        if self.config.sleep.enabled and self.sleep.is_sleeping():
            return  # 深度审查 C-1:睡眠期不推进泵——在途消息已被拦,推进会注入下一条进拦截链
        self.qzone_injector.on_turn_complete(time.monotonic())
        self._spawn_background_task(self._qzone_pump())

    # ---------- 内部辅助 ----------

    # spike ②/③/④ 结论的字段名集中于此,不符仅改此处
    _MESSAGES_KEY = "items"  # 主程序 planner.before_request payload 键(spike ② 快照格式)
    _OUTPUT_ITEMS_KEY = "output_items"

    def _messages_from_kwargs(self, kwargs: dict[str, Any]) -> list[dict] | None:
        return kwargs.get(self._MESSAGES_KEY)

    def _system_tail_index(self, messages: list[dict]) -> int:
        """注入点 = system 消息之后(spike ②:快照 item 无 role 字段,按 item_type 定位)。"""

        for i, m in enumerate(messages):
            if m.get("item_type") == "SystemMessageItem" or m.get("role") == "system":
                return i + 1
        self.ctx.logger.warning("注入定位失败:items 中无 SystemMessageItem,已回退追加尾部(缓存纪律受损)")
        return len(messages)  # 无 system 时追加尾部(spike ② 回退语义)

    def _spawn_background_task(self, coro: Any) -> asyncio.Task:
        """启动后台任务并持有引用:done 回调里 discard;异常时经 ctx.logger.exception 上报。"""

        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # exc_info 显式传异常对象:done 回调不在 except 上下文,裸 exception()
            # 不会附栈回溯,只有一行类型名(深度审查 F5 可观测性)
            self.ctx.logger.exception("后台任务异常:%s", exc, exc_info=exc)

    def _to_snapshot_item(self, text: str) -> dict:
        """渲染块 → 合法快照 UserMessageItem(spike ②:朴素 dict 被主程序拒绝)。

        同文本返回同一对象(前缀缓存纪律,缓存键=文本);item_id 用 sha256 前缀,
        勿用内置 hash()(进程内哈希随机化);timestamp 随对象绑定(创建时确定)。
        """

        cached = self._snapshot_cache.get(text)
        if cached is not None:
            self._snapshot_cache.move_to_end(text)  # 命中刷新新近度(LRU,背包 M-1)
            return cached
        item_id = "catsitate-inject-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        item = {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": item_id,
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": text}],
        }
        self._snapshot_cache[text] = item
        if len(self._snapshot_cache) > SNAPSHOT_CACHE_MAX:
            self._snapshot_cache.popitem(last=False)  # 逐最旧(背包 M-1:文本键无界增长)
        return item

    async def _build_inject_blocks(self, kwargs: dict[str, Any]) -> list[InjectionBlock]:
        cfg = self.config
        # planner.before_request payload 无 user_id/stream_id 键(实机确认):
        # 流 = session_id;说话人 = 私聊流对端 / 群聊最近非 bot 消息发送者
        stream_id = str(kwargs.get("session_id") or "")
        # 虚拟流说话人 = 注入泵当前动态作者——注入 timestamp=阅读时刻(方案 B),
        # 宿主 24h 窗天然可靠;说话人仍取注入泵当前作者(spec §2.16 交叉校验)
        if stream_id and stream_id in self._qzone_session_id_set() and self._qzone_available:
            speaker = self.qzone_injector.awaiting_author
        else:
            speaker = await self._resolve_speaker(stream_id) if stream_id else ""
        blocks: list[InjectionBlock] = []
        if cfg.inject.environment_enabled and cfg.time_aware.enabled:
            env = self._environment_block(stream_id)
            if env:
                blocks.append(InjectionBlock("environment", env[0], env[1]))
        if cfg.schedule.enabled and cfg.time_aware.enabled and cfg.memo.enabled:
            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")
            win = current_window(self._schedule_data, now_iso)
            if win and win.get("kind") != "sleep":
                line = f"[日程] {win.get('activity') or '自由时间'}"
                day = datetime.now().strftime("%Y-%m-%d")
                mark = f"{day}|{win.get('start')}"
                fired = self._schedule_tick_fired.get(day) == mark
                if fired:
                    line += "(该窗口已过)"
                nxt = next_window(self._schedule_data, now_iso)
                if nxt:
                    label = "睡觉" if nxt.get("kind") == "sleep" else (nxt.get("activity") or "自由时间")
                    line += f";接下来:{label}"
                due_today = [
                    e for e in self.memo.due_on(datetime.now().strftime("%Y-%m-%d"))
                    if str(e.get("user_id") or "") == speaker
                    or (stream_id and str(e.get("stream_id") or "") == stream_id)
                ]
                if due_today:
                    line += ";" + ";".join(f"备忘:{e['content']}" for e in due_today[:3])
                blocks.append(InjectionBlock("schedule", f"sch:{win.get('start')}|{'fired' if fired else ''}", line))
        if cfg.qzone.enabled and self._qzone_available:
            qz = self._qzone_block(stream_id)
            if qz:
                blocks.append(InjectionBlock("qzone", qz[0], qz[1]))
        if cfg.inject.memo_enabled and cfg.memo.enabled:
            # §3.10 read 为 OR 语义(流 ∪ 主QQ ∪ 附带QQ),一次查询即含原「流+说话人」两维度;
            # 单查询无重复,按 id 去重保留为防御;取数与截断均按 inject_max(规格 §4.4,批③遗留修正)
            merged = self.memo.read(stream_id, speaker, limit=cfg.memo.inject_max)
            seen: set[int] = set()
            entries = []
            for entry in merged:
                if entry["id"] not in seen:
                    seen.add(entry["id"])
                    entries.append(entry)
            entries = entries[: cfg.memo.inject_max]
            if entries:
                text = "[备忘] " + ";".join(e["content"] for e in entries)
                key = "|".join(sorted(f"{e['id']}" for e in entries))
                blocks.append(InjectionBlock("memo", f"memo:{key}", text))
        if cfg.inject.favorability_enabled and cfg.favorability.enabled:
            target = speaker or str(kwargs.get("peer_id") or "")
            if target:
                blocks.append(
                    InjectionBlock(
                        "favorability",
                        f"fav:{target}",
                        # 联调决定:5 级规则全量注入改为按等级单条注入(等级规则块并入好感度块)
                        build_favorability_block(
                            self.fav_engine, target,
                            include_rule=cfg.inject.level_rule_enabled,
                        ),
                    )
                )
        return blocks

    async def _refresh_stream_cache(self) -> None:
        """流信息缓存刷新(10 分钟 TTL);失败保持旧缓存并显式告警。"""

        now = datetime.now().timestamp()
        if self._stream_cache and now - self._stream_cache_at <= 600:
            return
        try:
            raw: list[dict] = []
            for platform in ("qq", QZONE_PLATFORM):
                result = await self.ctx.chat.get_all_streams(platform=platform)
                if isinstance(result, list):
                    raw.extend(result)
                elif isinstance(result, dict) and result.get("success"):
                    raw.extend(result.get("streams") or [])
                else:
                    self.ctx.logger.warning("聊天流列表形状异常(platform=%s,%s),该平台沿用旧缓存", platform, type(result).__name__)
            self._stream_cache = {
                str(st.get("session_id") or ""): st
                for st in raw if isinstance(st, dict) and st.get("session_id")
            }
            self._stream_cache_at = now
        except Exception:
            self.ctx.logger.exception("聊天流列表获取失败,沿用旧缓存")

    async def _resolve_speaker(self, stream_id: str) -> str:
        """注入目标说话人:私聊=流对端用户;群聊=最近非 bot 消息发送者(无则回退流 user_id)。

        流信息经 chat.get_all_streams 建缓存(10 分钟 TTL);群聊说话人每轮变化属设计预期。
        """

        await self._refresh_stream_cache()
        streams = self._stream_cache
        info = streams.get(stream_id) or {}
        if not str(info.get("is_group_session") or "").lower().startswith(("true", "1")):
            uid = str(info.get("user_id") or "")
            if uid:
                return uid
        bot_id = str(self.config.favorability.bot_user_id or "").strip()
        try:
            recent = await self._fetch_recent(stream_id, 3)
        except Exception:
            self.ctx.logger.exception("最近消息获取失败,说话人回退流信息")
            recent = []
        for m in recent:
            if not isinstance(m, dict):
                continue
            ui = (m.get("message_info") or {}).get("user_info") or {}
            uid = str(ui.get("user_id") or "")
            if uid and uid != bot_id:
                return uid
        return str(info.get("user_id") or "")

    def _qzone_session_id_set(self) -> set[str]:
        """虚拟流 session 集合 = 运行时收集 ∪ 本地按公式计算的预期值。"""

        ids = set(self._qzone_session_ids)
        expected = self._qzone_expected_session_id()
        if expected:
            ids.add(expected)
        return ids

    def _qzone_block(self, stream_id: str) -> tuple[str, str] | None:
        """qzone 注入块(spec §3.4):虚拟流=当前浏览动态状态;真实聊天=近期见闻摘要。

        注入块去重(可读性优化 2026-09-01):场景全文已由 apply_scene_surgery
        原位替换进 system 段,虚拟流分支只保留动态状态——不再拼场景常量,
        免同轮双份场景说明互相漂移。
        """

        if stream_id in self._qzone_session_id_set():
            state = self.qzone_injector.describe_current()
            return f"qzone:v:{state}", f"[空间] {state}"
        entries = self.qzone_seen.recent_seen(
            limit=self.config.qzone.summary_count, days=self.config.qzone.summary_days, now=datetime.now()
        )
        if not entries:
            return None
        # 叙事格式(与浏览动态的自然文本一致):「昵称发了「摘要」」比键值对
        # 「昵称:摘要」更像转述见闻;摘要截 20 字,纯图说说以「图片」占位
        lines = [
            f"{e['author_nickname'] or e['author_uin']}发了「{(e['summary'] or '图片')[:20]}」"
            for e in entries
        ]
        text = "[空间] 近期刷到: " + ";".join(lines)
        key = "qzone:s:" + "|".join(e["tid"] for e in entries)
        return key, text

    async def _qzone_group_prompt(self) -> str:
        """主程序 group_chat_prompt 当前值(1 小时缓存;读失败返回空串并告警)。"""

        now = time.monotonic()
        if self._qzone_group_prompt_at and now - self._qzone_group_prompt_at < 3600:
            return self._qzone_group_prompt_value
        try:
            result = await self.ctx.config.get(key="chat.reply_style.group_chat_prompt", default="")
            value = result.get("value") if isinstance(result, dict) else result
            self._qzone_group_prompt_value = str(value or "")
            self._qzone_group_prompt_at = now
        except Exception:
            self.ctx.logger.exception("group_chat_prompt 读取失败,场景替换按空配置处理")
            self._qzone_group_prompt_value = ""
            self._qzone_group_prompt_at = now
        return self._qzone_group_prompt_value

    def _qzone_warn_once(self, key: str, message: str) -> None:
        """每进程每类告警一次(场景回退类)。"""

        if key not in self._qzone_warned:
            self._qzone_warned.add(key)
            self.ctx.logger.warning("QQ空间场景回退:%s", message)


    def _qzone_expected_session_id(self) -> str:
        """按主程序 session_id 公式本地计算虚拟流预期值(md5(platform[+account]+group_id))。"""

        account = str(self.config.favorability.bot_user_id or "").strip()
        parts = [QZONE_PLATFORM]
        if account:
            parts.append(f"account:{account}")
        parts.append(self.config.qzone.virtual_group_id)
        return hashlib.md5("_".join(parts).encode()).hexdigest()

    def _environment_block(self, stream_id: str) -> tuple[str, str] | None:
        """环境块:节日+天气;缓存 45 分钟(规格 §4.2)。"""

        del stream_id
        cfg = self.config.time_aware
        if self._env_fetched_at and (datetime.now() - self._env_fetched_at).total_seconds() < cfg.weather_refresh_minutes * 60:
            cached = self._env_cache.get("env")
            return ("env", cached) if cached else None
        return None  # 数据未就绪时跳过(首次由后台任务填充后自动出现)

    async def _refresh_environment(self) -> None:
        """后台任务:拉取节日(在线→库→内置)与天气(Open-Meteo),刷新环境块缓存。"""

        if self.sleep.is_sleeping():
            return  # 睡眠期禁网络调用,下一 tick 自然重试
        if not self.config.plugin.enabled or not self.config.time_aware.enabled:
            return
        cfg = self.config.time_aware
        today = date.today()
        online = None
        sources = [
            # npm 包内不含年份 JSON(npm 源必然 404),必须用 jsDelivr 的 GitHub 仓库形式;
            # gh 形式仅支持分支/commit/tag,不支持 @latest
            f"https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{today.year}.json",
            f"https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{today.year}.json",
        ]
        if not cfg.holiday_online:
            sources = []  # 在线刷新开关关闭,直接走库/内置回退链
        for url in sources:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    online = parse_holiday_cn(resp.json())
                break
            except Exception:
                self.ctx.logger.warning("holiday-cn 数据源 %s 获取失败,尝试下一个", url, exc_info=True)
        try:
            from holiday_calendar import get_holidays  # manifest 声明依赖(自动安装)

            lib_data = get_holidays(today.year) if online is None else None
        except Exception:
            lib_data = None
        holidays = holiday_chain(today, {**online, **lib_data} if online and lib_data else (online or lib_data), builtin_ok=True)
        weather = None
        try:
            lat, lon = cfg.city_lat, cfg.city_lon
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={"latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code", "daily": "temperature_2m_max,temperature_2m_min,weather_code", "forecast_days": 2},
                )
                resp.raise_for_status()
                data = resp.json()
                weather = {"temperature_2m": data["current"]["temperature_2m"], "weather_code": data["current"]["weather_code"]}
        except Exception:
            self.ctx.logger.warning("天气获取失败,本轮环境块省略天气", exc_info=True)
        # 天气快照落库供二期 2.1 联动(规格 §4.2)
        if weather is not None:
            self.store.execute(
                """
                INSERT INTO weather_snapshot (id, city, fetched_at, data) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET city = excluded.city, fetched_at = excluded.fetched_at, data = excluded.data
                """,
                (cfg.city, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), json.dumps(weather, ensure_ascii=False)),
            )
        # 当天 + 临近 3 天节日/节气(规格 §4.2 注入示例)
        # 公历节日走回退链结果;农历节日经 lunar-python 实算;双源重名按名去重
        today_holidays = dedup_festival_names(holidays.get(today.strftime("%m-%d"), []) + lunar_festivals_near(today, days=0))
        today_terms = solar_terms_near(today, days=0)
        # 临近节日/节气单独一段(带日期),不与「今天」混淆(联调 bug:3 天窗口混入今日)
        upcoming: list[str] = []
        for offset in range(1, 4):
            day = today + timedelta(days=offset)
            upcoming.extend(f"{day.month}月{day.day}日 {n}" for n in holidays.get(day.strftime("%m-%d"), []))
        upcoming += [n for n in lunar_festivals_upcoming(today, days=3) if "月" in n]
        upcoming += [n for n in solar_terms_near(today, days=3) if "月" in n]
        upcoming = dedup_festival_names(upcoming)
        text = build_environment_text(today, cfg.city, weather, today_holidays, today_terms, upcoming=upcoming)
        self._env_cache["env"] = text
        self._env_fetched_at = datetime.now()

    async def _cleanup_memos(self) -> None:
        removed = self.memo.cleanup()
        if removed:
            self.ctx.logger.info("备忘清理:%s 条过期", removed)

    async def _daily_settle(self) -> None:
        """日终兜底:对当日有消息且未日终结算的用户结算当前批次(不计提前上限,规格 §4.3)。"""

        if self.sleep.is_sleeping():
            return  # 睡眠期调度静默,醒来补跑
        await self._daily_decay()  # 先衰减后结算(同一 tick 调用顺序)
        if not self.config.plugin.enabled or not self.config.favorability.enabled:
            return
        # 按人语义:多流用户日终只结一次,结算聚合该人全部流素材(规格全局决策 #7)
        candidates = set(self.fav_engine.iter_today_active())
        # 深度审查 C-N1:纯空间互动好友并集——只有空间事件(评论/点赞/出站)而无
        # 当日聊天的人没有 batch 行,iter_today_active 只扫 batch_counter 扫不到,
        # 原实现下纯空间互动者永不结算;并集当日 qzone_fav_events 的 user_id 令其
        # 进入日终兜底(空间事件本身即结算素材,§3.9)。bot 自身排除:源A自评回复
        # 的 OUT_COMMENT 以 bot 为 target 落事件,但 bot 不是好感度结算对象。
        today = datetime.now().strftime("%Y-%m-%d")
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        try:
            event_users = {
                str(r[0]) for r in self.store.query(
                    "SELECT DISTINCT user_id FROM qzone_fav_events WHERE day = ?", (today,)
                )
            }
        except Exception:
            self.ctx.logger.exception("当日空间事件反查失败,日终候选仅用 batch 活跃(深度审查 C-N1)")
            event_users = set()
        candidates |= {u for u in event_users if u and u != bot_uin}
        for user_id in sorted(candidates):
            if self.fav_engine.has_daily_settle_today(user_id):
                continue
            try:
                await self._settle_and_log(user_id, kind="daily")
            except Exception:
                # 单用户结算失败隔离,不拖垮整轮(与衰减逐流隔离对齐,审查 I4)
                self.ctx.logger.exception("日终结算失败(user=%s),跳过该用户", user_id)
                continue

    async def _sleep_tick(self) -> None:
        if not self.config.plugin.enabled or not self.config.sleep.enabled:
            return
        now = datetime.now()
        now_iso = now.strftime("%Y-%m-%dT%H:%M")
        # 注意:不能经 is_sleeping() 判断睡眠中——其语义含 now < wake_at,
        # wake_at 过后直接返回 False 落入 else 分支,自然醒分支会成死代码(审查 Critical #1)
        if self.sleep.state.state == "sleep":
            if now.strftime("%Y-%m-%dT%H:%M:%S") >= (self.sleep.state.wake_at or "9999"):
                self.ctx.logger.info("自然醒来: %s", now.strftime("%Y-%m-%dT%H:%M:%S"))
                await self._wake_up()
                # 醒来首个动作:补注昨晚发布的日记(睡眠期注入会被 sleep_gate 拦进回顾缓冲)
                await self._echo_pending_diary()
            return
        # 醒态兜底补注:入睡任务发布的日记经此回注(含「窗口终点未入睡」路径——
        # bot 整夜醒着,日记发布后下个 tick 即补注);失败保留快照下个 tick 重试
        await self._echo_pending_diary()
        win = current_window(self._schedule_data, now_iso)
        if not win or win.get("kind") != "sleep":
            # 睡眠窗口已过而未入睡(Q1 裁定):不入睡,但补执行入睡时会做的任务(次日日程生成)
            await self._maybe_settle_passed_sleep_window(now)
            return
        # 可入睡时间(睡眠窗口语义,联调裁定 2026-08-17):
        # 静默睡眠关 = 窗口起点直接入睡;静默睡眠开 = 安静满 N 分钟入睡
        if not self.config.sleep.silent_sleep_enabled:
            self.ctx.logger.info("睡眠窗口起点已到(静默睡眠关闭),直接入睡")
            await self._enter_sleep()
            return
        if now.timestamp() - self._quiet_since(win) >= self.config.sleep.silent_sleep_minutes * 60:
            self.ctx.logger.info("静默入睡:安静 %d 分钟", self.config.sleep.silent_sleep_minutes)
            await self._enter_sleep()

    def _quiet_since(self, win: dict) -> float:
        """静默入睡计时基准:max(睡眠窗口起点, 最后活动时刻);无活动记录从窗口起点起算。"""

        try:
            win_start = datetime.strptime(str(win.get("start") or ""), "%Y-%m-%dT%H:%M").timestamp()
        except (ValueError, TypeError):
            win_start = 0.0
        return max(self._last_activity_ts or win_start, win_start)

    async def _maybe_settle_passed_sleep_window(self, now: datetime) -> None:
        """睡眠窗口已过而未入睡(Q1 裁定):不入睡,但执行入睡时会做的任务(次日日程生成+日记生成);每窗口一次。

        入睡过(入睡时已执行)则跳过;窗口结束前不触发。
        """

        sleep_win = next(
            (w for w in (self._schedule_data.get("windows") or []) if w.get("kind") == "sleep"), None
        )
        if not sleep_win:
            return
        end = str(sleep_win.get("end") or "")
        if not end or end > now.strftime("%Y-%m-%dT%H:%M"):
            return  # 窗口尚未结束
        if self._sleep_window_settled == end:
            return  # 本窗口已入睡(入睡时已生成)或已补执行过
        self._sleep_window_settled = end
        self.ctx.logger.info("睡眠窗口已过未入睡:补执行入睡任务(不入睡)")
        self._spawn_background_task(self._generate_tomorrow_schedule())
        self._spawn_background_task(self._generate_and_publish_diary())

    async def _wake_up(self) -> None:
        self.sleep.wake()
        if self.config.sleep.review_enabled:
            self._spawn_background_task(self._write_sleep_review())
        # 醒来补跑当日结算(内部已先衰减后结算;勿再单独 spawn 衰减,防并发双计,审查 Important #2)
        self._spawn_background_task(self._daily_settle())

    async def _daily_decay(self) -> None:
        """自然衰减(按人跨流):先衰减后结算(与 _daily_settle 同 tick 调用顺序)。

        计时基准(规格「判定后重置计时」):基准 = max(各活跃流内最近 bot 直接互动时间,
        最近一次 decay 判定时间)——衰减判定本身即一次「想起」,7 天内不重复衰减。
        群聊互动 = @ 或 quote(reply 段经 message.get_by_id 解析原发送者,规格 §3.1);
        quote 解析在本函数内预解析后注入 resolved_quote_user_id(decay.py 保持纯函数)。
        """

        if self.sleep.is_sleeping():
            return
        if not self.config.plugin.enabled or not self.config.favorability.decay_enabled:
            return
        try:
            decay_quote_warned = False  # 本轮 _daily_decay 至多一条 quote 解析失败告警(不静默)
            candidates = []
            # 注意:不能用 iter_today_active(只含今日活跃流)——衰减对象恰是长期未互动者,
            # 必须扫 favorability 全表 score>0 行(按人,行主键即 user_id)
            rows = self.store.query("SELECT DISTINCT user_id FROM favorability WHERE score > 0")
            for (user_id,) in rows:
                row = self.fav_engine.get_level(user_id)
                if row is None or row["score"] <= 0:
                    continue
                # 跨流取该人最近一次 bot 直接互动时间(经 batch_counter 活跃流账本定位流)
                best: str = ""
                stream_rows = self.store.query("SELECT DISTINCT stream_id FROM batch_counter WHERE user_id = ?", (user_id,))
                for (stream_id,) in stream_rows:
                    await self._refresh_stream_cache()
                    if stream_id not in self._stream_cache:
                        # 流已消亡(解散/移除):显式跳过该流,不误判为私聊,不拖垮整轮
                        self.ctx.logger.warning("衰减候选流不存在(user=%s,stream=%s),跳过该流", user_id, stream_id)
                        continue
                    try:
                        recent = await self._fetch_recent(stream_id, 50)
                    except Exception:
                        # 单流取消息失败(如 RPC 超时)只跳过该流,其余流继续
                        self.ctx.logger.warning("衰减候选取消息失败(user=%s,stream=%s),跳过该流", user_id, stream_id)
                        continue
                    # quote 语义(最终审查 I2 恢复):群聊 bot 消息 reply 段为纯消息 id,
                    # 经 message.get_by_id 解析原发送者后注入 resolved_quote_user_id
                    # (逐条浅拷贝,不就地修改 get_recent 返回值);解析失败不注入该字段,
                    # 该条按 at 判定,每轮至多一条告警
                    if self._stream_is_group(stream_id):
                        senders, first_err = await self._resolve_quote_senders(recent, stream_id)
                        if first_err and not decay_quote_warned:
                            decay_quote_warned = True
                            self.ctx.logger.warning("quote 发送者解析失败(stream=%s):%s", stream_id, first_err)
                        annotated: list[dict] = []
                        for m in recent:
                            if not isinstance(m, dict):
                                annotated.append(m)
                                continue
                            copy = dict(m)
                            rid = str(m.get("reply_to") or "")
                            if rid and senders.get(rid):
                                copy["resolved_quote_user_id"] = senders[rid]
                            annotated.append(copy)
                        recent = annotated
                    t = last_bot_interaction_time(
                        recent, user_id, str(self.config.favorability.bot_user_id or ""),
                        stream_is_group=self._stream_is_group(stream_id),
                    )
                    if t and (not best or t > best):
                        best = t
                # 空间互动事件参与衰减计时基准(spec §3.9:窗口外评论是真实双向互动)。
                # 事件时间取自事件表全局、不依赖流存在——流消亡(上文 continue)只影响
                # 消息素材基准不影响事件基准;读取失败告警后按空串(不参与 max)
                event_ts = ""
                try:
                    event_ts = self.qzone_comment_seen.last_fav_interaction(user_id)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("衰减取空间互动事件失败,该人事件基准按空处理(user=%s):%s", user_id, type(exc).__name__)
                # 最近一次衰减判定时间作为基准参与取 max(该人全局,跨流)
                decay_rows = self.store.query(
                    "SELECT judged_at FROM favorability_log WHERE user_id = ? AND judge_id LIKE 'decay-%' "
                    "ORDER BY judged_at DESC LIMIT 1",
                    (user_id,),
                )
                decay_ts = decay_rows[0][0] if decay_rows else ""
                candidates.append((user_id, max(best or "", decay_ts, event_ts or "")))
            persona, style = await self._persona_context()
            results = await self.decay.scan_and_apply(candidates, persona=persona, behavior_style=style)
            for r in results:
                self.ctx.logger.info("好感度衰减 %s:delta=%s", r["user_id"], r["delta"])
                if r.get("exclusive_clamped"):
                    self.ctx.logger.warning("衰减升特别被独占钳制(user=%s)", r["user_id"])
        except Exception:
            self.ctx.logger.exception("衰减扫描异常,本轮跳过")

    def _stream_is_group(self, stream_id: str) -> bool:
        info = self._stream_cache.get(stream_id) or {}
        return str(info.get("is_group_session") or "").lower().startswith(("true", "1"))

    async def _write_sleep_review(self) -> None:
        """睡醒回顾:拦截缓冲按流聚合,LLM 摘要,写单份聚合报告文件。"""

        buffer, self._sleep_review_buffer = self._sleep_review_buffer, []
        self._sleep_review_buffer_snapshot.save({"messages": self._sleep_review_buffer})
        if not buffer:
            return
        report_dir = self.ctx.paths.data_dir / "sleep_review" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        by_stream: dict[str, list[dict]] = {}
        for item in buffer:
            by_stream.setdefault(item["stream_id"], []).append(item)
        sections: list[str] = []
        for stream_id, msgs in by_stream.items():
            preview = " | ".join(f"{m['nickname'] or m['user_id']}:{m['text'][:50]}" for m in msgs[:20])
            messages, _ = build_side_prompt(
                "sleep_review", [], [f"睡眠期间 {stream_id} 的消息(共 {len(msgs)} 条):\n{preview}"]
            )
            try:
                result = await self._side_llm_call(messages, self.config.sleep.review_llm_model, "sleep_review", self.config.sleep.review_llm_timeout_ms)
                summary = str(result.get("response") or "")[:200] if isinstance(result, dict) else ""

            except Exception:
                self.ctx.logger.exception("回顾摘要失败(流 %s)", stream_id)
                summary = ""
            sections.append(f"## 流 {stream_id}({len(msgs)} 条)\n{summary or '摘要生成失败'}")
        # 睡眠期到期的备忘提醒静态附列(不占 LLM 额度,备忘不丢失原则)
        sleep_day_due = [
            e for e in self.memo.due_on(datetime.now().strftime("%Y-%m-%d"))
            if e["remind_at"][:16] < datetime.now().strftime("%Y-%m-%dT%H:%M")
        ]
        if sleep_day_due:
            sections.append("## 睡眠期到期的备忘提醒\n" + "\n".join(f"- {e['content']}({e['remind_at']})" for e in sleep_day_due))
        path = report_dir / f"sleep_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        path.write_text("\n\n".join(sections) or "(无内容)", encoding="utf-8")
        os.chmod(path, 0o600)  # 报告含消息文本/用户标识,仅属主可读(安全复审,审查 M15)
        self.ctx.logger.info("睡醒回顾已生成: %s", path)

    async def _generate_tomorrow_schedule(self) -> None:
        """入睡确认:生成次日日程(入睡任务的日程侧;旁路 LLM 不经消息链,睡眠期可执行);失败用默认模板并告警。"""

        now = datetime.now()
        target = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        due = [f"{e['content']}({e['remind_at'][11:16]})" for e in self.memo.due_on(target)]
        try:
            persona, style = await self._persona_context()
            data, err = await self.schedule_gen.generate(
                persona=persona, behavior_style=style, today_review=self._today_review_text(),
                weather_text=self._weather_text(), fav_summary=self._fav_summary_text(),
                due_memos=due, target_date=target,
            )
        except Exception:
            self.ctx.logger.exception("次日日程生成异常,使用默认作息模板")
            data, err = _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target), "异常"
        if err:
            self.ctx.logger.warning("次日日程生成:%s(模板兜底)", err)
        self._schedule_data = data
        self._schedule_generated = (not err)  # 模板兜底日不视为生成日程,备忘提醒兜底保持开启(审查 I-2)
        self._schedule_edit_history = []
        self._persist_schedule()
        self.ctx.logger.info("次日日程已生成:%s", json.dumps(data, ensure_ascii=False)[:200])

    async def _generate_and_publish_diary(self) -> None:
        """入睡任务的日记侧:睡前用当日素材生成日记并发布为空间说说。

        与日程生成同属入睡任务——旁路 LLM 与发布 API 均不经消息链,不受睡眠
        拦截(深夜直发)。素材只取当日真实数据(日程活动/备忘/空间见闻),模板
        明令不得编造,防日记虚构没发生的事。发布成功后正文存 pending 快照,
        回注延迟到醒来(睡眠期 route_message 会被 sleep_gate 拦进回顾缓冲,
        白注入)。
        """

        if not self.config.qzone.enabled or not self.config.qzone.diary_enabled:
            return
        if not self._qzone_available:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        # 日程素材只取活动窗口(睡眠窗口没有「做过什么」);与次日日程生成并发
        # 时本任务先读今日日程(派发序保证,LLM 调用前完成素材组装)
        schedule_summary = ";".join(
            str(w.get("activity") or "") for w in (self._schedule_data.get("windows") or [])
            if w.get("kind") != "sleep"
        )
        memos = ";".join(e["content"] for e in self.memo.due_on(today)[:3])
        seen_feeds = self.qzone_seen.recent_seen(limit=3, days=1, now=datetime.now())
        seen_summary = ";".join(e["summary"][:20] for e in seen_feeds)
        stable_ctx = (
            f"今天的日程:{schedule_summary or '自由活动'}\n"
            f"备忘:{memos or '无'}\n看到的好友动态:{seen_summary or '无'}"
        )
        messages, _ = build_side_prompt("qzone_diary", [stable_ctx], [])
        try:
            result = await self._side_llm_call(
                messages, self.config.qzone.diary_llm_model, "qzone_diary", self.config.qzone.diary_llm_timeout_ms
            )
        except Exception:
            self.ctx.logger.exception("QQ空间日记 LLM 生成失败,跳过本轮")
            return
        if not isinstance(result, dict) or not result.get("success"):
            # 不落响应原文(安全复审纪律):仅记失败形态
            detail = f"success={result.get('success')}" if isinstance(result, dict) else f"结果类型={type(result).__name__}"
            self.ctx.logger.warning("QQ空间日记 LLM 失败(%s),跳过", detail)
            return
        diary_text = str(result.get("response") or "").strip()
        # 模板要求 80~200 字,超 300 字视为模型输出异常(夹带解释/重复),不硬发
        if not diary_text or len(diary_text) > 300:
            self.ctx.logger.warning("QQ空间日记内容异常(长度=%d),跳过发布", len(diary_text))
            return
        try:
            await self.qzone_client.do_publish(content=diary_text)
        except Exception:
            self.ctx.logger.exception("QQ空间日记发布失败(内容已生成,发布跳过)")
            return
        self.ctx.logger.info("QQ空间日记发布成功: %s", diary_text[:30])
        # 回注延迟到醒来:存入 pending 快照,醒态 sleep_tick 补注
        self._pending_diary_snapshot.save({
            "text": diary_text,
            "published_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        })

    async def _echo_pending_diary(self) -> None:
        """醒来后补注昨晚发布的日记(self 消息,仅入历史)。

        回注是上下文锚:好友次日评论日记说说时,bot 需要这段历史才知道自己
        昨晚写过什么;正文只带前 60 字预览(全文已真实发布在空间,超长挤占
        虚拟流)。不设 is_mentioned——bot 自己的旧说说不需要触发决策轮。
        route_message 失败保留快照,醒态 sleep_tick 下轮重试。
        """

        data = self._pending_diary_snapshot.load()
        text = str(data.get("text") or "").strip()
        if not text:
            return
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        self._qzone_seq += 1
        msg = {
            "message_id": f"qzone_self_diary_{int(time.time())}_{self._qzone_seq}",
            "platform": QZONE_PLATFORM,
            "timestamp": str(int(time.time())),
            "message_info": {
                "user_info": {"user_id": bot_uin, "user_nickname": "我"},
                "group_info": {
                    "group_id": self.config.qzone.virtual_group_id,
                    "group_name": self.config.qzone.virtual_group_name,
                },
            },
            "raw_message": [{"type": "text", "data": f"我昨晚发布的日记:{text[:60]}"}],
        }
        try:
            await self.ctx.gateway.route_message(QZONE_GATEWAY_NAME, msg)
            self._pending_diary_snapshot.save({})
            self.ctx.logger.info("QQ空间日记醒来补注完成")
        except Exception:
            self.ctx.logger.exception("QQ空间日记补注失败(下个 tick 重试)")

    # ---------- 日程窗口 trigger(trigger 模式:插件不 send,只指示主程序) ----------

    async def _schedule_tick(self) -> None:
        if not self.config.plugin.enabled or not self.config.schedule.enabled:
            logger.debug("schedule_tick 跳过:模块未启用")
            return
        if self.sleep.is_sleeping():
            return  # 绝对静默,跳过窗口执行
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        self._prune_day_keys(day)
        if not self._schedule_data or self._schedule_data.get("date") != day:
            stale = self._schedule_data
            if stale and stale.get("date"):
                stale_win = current_window(stale, now.strftime("%Y-%m-%dT%H:%M"))
                if stale_win and stale_win.get("kind") == "sleep":
                    # 跨午夜:旧日程睡眠窗口仍在进行,保留旧日程交给 _sleep_tick 处理
                    # (静默关=直接睡/静默开=安静计时;窗口结束后再换新模板,公测发现)
                    logger.debug("schedule_tick 跳过换日:旧日程睡眠窗口仍在进行")
                    return
            self._schedule_data = _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, day)  # 首日模板撑场(非生成)
            self._schedule_generated = False
        win = current_window(self._schedule_data, now.strftime("%Y-%m-%dT%H:%M"))
        if not win or win.get("kind") == "sleep":
            return
        mark = f"{day}|{win.get('start')}"
        if self._schedule_tick_fired.get(day) == mark:
            logger.debug("schedule_tick 跳过:窗口已触发(mark=%s)", mark)
            return  # 同窗口只触发一次
        if self._speak_counts.get(day, 0) >= self.config.schedule.daily_speak_limit:
            logger.debug("schedule_tick 跳过:已达每日发言上限 %s", self.config.schedule.daily_speak_limit)
            return
        logger.debug("schedule_tick 进入窗口 kind=%s 活动=%s", win.get("kind"), win.get("activity"))
        if win.get("kind") == "greeting":
            await self._greet_exclusive(day, win)  # 主动问候:仅特别者+私聊通道,无 2.1 群流路径
            self._schedule_tick_fired[day] = mark
            return
        await self._window_trigger(day, win)
        self._schedule_tick_fired[day] = mark

    async def _window_trigger(self, day: str, win: dict) -> None:
        """2.1 窗口 trigger:门槛过滤 → 活跃流排序取前 n → 每流 trigger(计 1)。"""

        threshold = self.config.schedule.speak_threshold_level
        candidates = await self._active_streams_over(day)
        if not candidates:
            return
        overview = self._day_overview_text(win)
        limit = max(1, self.config.schedule.speak_max_streams_per_window)
        for target in candidates[:limit]:
            if self._speak_counts.get(day, 0) >= self.config.schedule.daily_speak_limit:
                return
            intent = build_proactive_intent(win, target, overview)
            try:
                await self.ctx.maisaka.proactive.trigger(
                    stream_id=target["stream_id"], intent=intent,
                    reason=f"日程窗口:{win.get('activity')}", priority="",
                )
            except Exception:
                self.ctx.logger.exception("主动任务触发失败(stream=%s)", target["stream_id"])
                continue
            self._speak_counts[day] = self._speak_counts.get(day, 0) + 1
            self.ctx.logger.info("主动触发[%s] -> %s:%s", day, target["stream_id"], (win.get("activity") or "")[:40])

    async def _active_streams_over(self, day: str) -> list[dict]:
        """候选流(2.1 日常发言):近 24h 活跃流按人取等级,门槛固定 speak_threshold_level,
        按(等级,最近活动)降序(daily 窗口专用)。

        私聊流 user_id 取流信息 user_id,群聊流取最近非 bot 说话人(复用 _resolve_speaker 近似);
        无当前说话人的流跳过(空 user_id 无法作 trigger 目标)。"""

        del day  # 活跃判定仅用 last_bump 近 24h(签名保持 brief 约定)
        cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        rows = self.store.query(
            "SELECT stream_id, MAX(last_bump) FROM batch_counter WHERE last_bump >= ? GROUP BY stream_id",
            (cutoff,),
        )
        candidates = []
        threshold = self.config.schedule.speak_threshold_level
        for stream_id, last_bump in rows:
            if stream_id in self._qzone_session_id_set():
                continue  # 虚拟流不做 daily 窗口主动发言目标(空间表达走 qzone 窗口触发)
            user_id = await self._resolve_speaker(stream_id)
            if not user_id:
                continue  # 无当前说话人则跳过
            row = self.fav_engine.get_level(user_id)
            level_name = LEVELS[row["level"]] if row else "陌生"
            if not threshold_met(level_name, threshold):
                continue
            candidates.append({
                "stream_id": stream_id,
                "user_id": user_id,
                "level_name": level_name,
                "note": row["note"] if row else "",
                "_level": LEVEL_INDEX[level_name],
                "_recent": last_bump or "",
            })
        candidates.sort(key=lambda c: (c["_level"], c["_recent"]), reverse=True)
        logger.debug("2.1 候选流: 活跃 %d -> 门槛过滤后 %d", len(rows), len(candidates))
        return [{k: c[k] for k in ("stream_id", "user_id", "level_name", "note")} for c in candidates]

    async def _greet_exclusive(self, day: str, win: dict) -> bool:
        """主动问候(规格 §3.5):仅「特别」等级者 + 必须存在私聊流;greeting 窗口起点触发,无每日一次限制。"""

        if self._speak_counts.get(day, 0) >= self.config.schedule.daily_speak_limit:
            logger.debug("主动问候跳过:已达每日发言上限")
            return False
        rows = self.store.query(
            "SELECT user_id, note FROM favorability WHERE level >= ? LIMIT 1", (EXCLUSIVE_LEVEL,)
        )
        if not rows:
            logger.debug("主动问候跳过:无特别等级者")
            return False  # 无特别者,不问候
        user_id, note = rows[0]
        await self._refresh_stream_cache()
        target_stream = None
        for stream_id, info in self._stream_cache.items():
            if str(info.get("is_group_session") or "").lower().startswith(("true", "1")):
                continue
            if str(info.get("user_id") or "") == user_id:
                target_stream = stream_id
                break
        if target_stream is None:
            self.ctx.logger.info("主动问候跳过:特别者(%s)无私聊流", user_id)
            return False
        intent = (
            f"现在是你的日程「{win.get('activity')}」时间,{user_id} 是你「特别」级的好友(注记:{note or '无'})。"
            "想问候就用自己的方式轻轻说一句,不想说就保持沉默。"
        )
        try:
            await self.ctx.maisaka.proactive.trigger(
                stream_id=target_stream, intent=intent,
                reason=f"日程问候窗口:{win.get('activity')}", priority="",
            )
        except Exception:
            self.ctx.logger.exception("主动问候触发失败(user=%s)", user_id)
            return False
        self._speak_counts[day] = self._speak_counts.get(day, 0) + 1
        self.ctx.logger.info("主动问候触发[%s] -> %s", day, user_id)
        return True

    async def _remind_fallback_tick(self) -> None:
        """备忘提醒独立兜底:无生成日程(模板撑场)时到点注入备忘归属流;睡眠中不执行。"""

        # 日键清理(schedule 关闭时 _schedule_tick 早退,兜底 tick 仍须清理,
        # 防 remind_fired.json 跨天无限增长,审查 M13)
        self._prune_day_keys(datetime.now().strftime("%Y-%m-%d"))
        if not self.config.plugin.enabled or self.config.memo.enabled is False:
            return
        if self.sleep.is_sleeping():
            return
        if self._schedule_data and self._schedule_generated and self._schedule_data.get("date") == datetime.now().strftime("%Y-%m-%d"):
            return  # 有当天生成的有效日程 → 提醒走日程收录,不重复兜底
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        due = [e for e in self.memo.due_on(datetime.now().strftime("%Y-%m-%d")) if e["remind_at"][:16] <= now and e["stream_id"]]
        for entry in due:
            key = f"remind:{entry['id']}"
            if key in self._remind_fired:
                continue
            try:
                await self.ctx.maisaka.context.append(
                    stream_id=entry["stream_id"],
                    segments=[{"type": "text", "text": f"[备忘提醒] {entry['content']}"}],
                )
                self.ctx.logger.info("备忘提醒兜底注入(stream=%s):%s", entry["stream_id"], entry["content"])
            except Exception:
                self.ctx.logger.exception("备忘提醒注入失败(stream=%s)", entry["stream_id"])
                continue  # 失败不标记:留重试机会(审查 M-9)
            self._remind_fired[key] = now
            self._remind_fired_snapshot.save(self._remind_fired)

    # ---------- 日程辅助 ----------

    def _day_overview_text(self, win: dict) -> str:
        """全天概览:『今天:活动1→活动2→…』(睡眠窗口记为 睡觉;win 为当前窗口,仅作上下文)。"""

        del win
        parts = []
        for w in (self._schedule_data.get("windows") or []):
            if w.get("kind") == "sleep":
                parts.append("睡觉")
            else:
                parts.append(w.get("activity") or "自由时间")
        return "今天:" + "→".join(parts)

    def _weather_text(self) -> str:
        """当前天气文本:读 weather_snapshot 快照(环境刷新落库);无则 '无数据'。"""

        rows = self.store.query(
            "SELECT data FROM weather_snapshot WHERE id = 1 ORDER BY fetched_at DESC LIMIT 1"
        )
        if not rows:
            return "无数据"
        try:
            data = json.loads(rows[0][0])
            return f"温度 {data.get('temperature_2m')}°C(天气码 {data.get('weather_code')})"
        except (json.JSONDecodeError, TypeError):
            return "无数据"

    def _fav_summary_text(self) -> str:
        """重要用户好感度汇总:按人单行『u:等级(分数)』,按等级降序。"""

        rows = self.store.query(
            "SELECT user_id, level, score, note FROM favorability ORDER BY level DESC, score DESC"
        )
        if not rows:
            return "无"
        return ";".join(f"{r[0]}:{LEVELS[r[1]]}({r[2]}分)" for r in rows)

    def _today_review_text(self) -> str:
        """今日回顾:睡醒回顾报告最新一篇的摘要(文件读取);无则 '无'。"""

        report_dir = self.ctx.paths.data_dir / "sleep_review" / "reports"
        try:
            files = sorted(report_dir.glob("sleep_review_*.md"))
        except OSError:
            return "无"
        if not files:
            return "无"
        try:
            return files[-1].read_text(encoding="utf-8")[:200]
        except OSError:
            return "无"

    def _persist_schedule(self) -> None:
        """日程落盘:data_dir/schedule.json(含修改历史与生成标记)。"""

        path = self.ctx.paths.data_dir / "schedule.json"
        try:
            path.write_text(json.dumps({
                "data": self._schedule_data,
                "edit_history": self._schedule_edit_history,
                "generated": self._schedule_generated,
                "saved_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.ctx.logger.exception("日程落盘失败")

    def _restore_schedule(self) -> None:
        """重启恢复:schedule.json 的 date 为今天或明天时恢复日程/编辑历史/生成标记(审查 I2)。

        入睡当晚生成的是次日日程,夜间重启不得误删——date ∈ {今天, 明天} 均恢复;
        早于今天的文件删除并告警,但**其睡眠窗口仍覆盖当前时刻**(跨午夜未睡完)时保留恢复,
        以便首个 sleep_tick 按静默开关入睡(公测发现:直接删除会导致当天无法入睡);
        损坏/结构非法文件告警并忽略(错误显式暴露,不静默)。
        """

        path = self.ctx.paths.data_dir / "schedule.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            self.ctx.logger.warning("schedule.json 读取失败,忽略恢复")
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.ctx.logger.warning("schedule.json 损坏,忽略恢复(新日程将在入睡时重新生成)")
            return
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            self.ctx.logger.warning("schedule.json 结构非法,忽略恢复")
            return
        now = datetime.now()
        saved_date = data["data"].get("date")
        keep_dates = (now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d"))
        stale_sleep_active = False
        if saved_date not in keep_dates:
            # 跨午夜边界:过期日程的睡眠窗口若仍覆盖当前时刻(如昨夜 23:00 入睡、今晨重启),
            # 保留恢复以便首个 sleep_tick 按静默开关入睡(关=直接入睡/开=安静计时)——直接删除会导致当天无法入睡(公测发现)
            stale_win = current_window(data["data"], now.strftime("%Y-%m-%dT%H:%M"))
            stale_sleep_active = bool(stale_win and stale_win.get("kind") == "sleep")
        if saved_date not in keep_dates and not stale_sleep_active:
            self.ctx.logger.warning("schedule.json 为过期日程(%s),删除并忽略恢复", saved_date)
            try:
                path.unlink()
            except OSError:
                self.ctx.logger.exception("过期 schedule.json 删除失败")
            return
        if stale_sleep_active:
            self.ctx.logger.warning("schedule.json 为过期日程(%s)但睡眠窗口仍在进行,恢复以便强制入睡", saved_date)
        self._schedule_data = data["data"]
        if isinstance(self._schedule_data.get("windows"), list):
            self._schedule_data["windows"] = sort_windows(self._schedule_data["windows"])  # 旧数据按时间顺序重排
        self._schedule_edit_history = data["edit_history"] if isinstance(data.get("edit_history"), list) else []
        self._schedule_generated = bool(data.get("generated"))
        self.ctx.logger.info("已从 schedule.json 恢复日程(%s)", saved_date)

    def _prune_day_keys(self, day: str) -> None:
        """日键清理:只保留当天的状态条目(缓存纪律,防跨天无限增长)。"""

        for key in list(self._schedule_tick_fired):
            if key != day:
                del self._schedule_tick_fired[key]
        for key in list(self._speak_counts):
            if key != day:
                del self._speak_counts[key]
        for key, value in list(self._remind_fired.items()):
            if not str(value).startswith(day):
                del self._remind_fired[key]

    async def _settle_and_log(self, user_id: str, kind: str) -> None:
        """按人结算(规格全局决策 #7):聚合该人所有流的消息,一次 LLM 判定。

        并发防护保留(最终审查 Important#1):fav_count 与 _daily_settle 可能并发发起
        同一用户结算(LLM 秒级延迟窗口内),该用户任一结算(kind 不限)已在飞即跳过,
        防 delta 双计。
        """

        key = user_id
        if key in self._settling:
            self.ctx.logger.info("好感度结算[%s] %s 已在结算中,跳过本轮", kind, user_id)
            return
        self._settling.add(key)
        try:
            streams = [r[0] for r in self.store.query("SELECT DISTINCT stream_id FROM batch_counter WHERE user_id = ?", (user_id,))]
            history: list[dict] = []
            for stream_id in streams:
                # 单流取消息失败只跳过该流(公测发现:大附件消息可撑爆 RPC 帧),不拖垮整次结算
                try:
                    history.extend(await self._fetch_recent_for_history(stream_id, 50, user_id))
                except Exception:
                    self.ctx.logger.warning("结算取消息失败(user=%s,stream=%s),跳过该流", user_id, stream_id)
            persona, style = await self._persona_context()
            # 空间互动事件并入结算素材(spec §3.9,LLM 计权无硬编码数值)。
            # 本方法素材形态是 history 列表(build_material 的输入 list[dict]),
            # 事件按合成 user 消息追加:role/user_id=目标保证进入素材锚点;
            # stream_id 用合成流隔离邻居,seq 取大且逐条唯一(build_material 以
            # (stream,seq) 去重)。ts 用事件原始时刻——同日多次结算(early→daily)时
            # 首次结算已把 window_start 前移,已判事件被窗口过滤排除(真实消息同机制),
            # 防同一事件反复并入素材重判(审查必修)
            today = datetime.now().strftime("%Y-%m-%d")
            try:
                events = self.qzone_comment_seen.fav_events_on(today, user_id)
            except Exception as exc:  # noqa: BLE001
                events = []
                self.ctx.logger.warning("空间互动事件读取失败,本次结算不含事件素材(user=%s):%s", user_id, type(exc).__name__)
            for i, e in enumerate(events[:5]):
                label = QZONE_FAV_EVENT_LABELS.get(e["kind"], "空间互动")
                history.append({
                    "role": "user",
                    "user_id": user_id,
                    "stream_id": "qzone-events",  # 合成流:不与真实流撞 id,事件互为邻居
                    "text": f"[空间互动] {label}: {e['text'][:60]}",
                    "seq": 10 ** 9 + i,  # 排序键取大且逐条唯一,保证全量保留
                    "ts": e["created_at"] or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "is_group": False,
                    "addressed": None,
                })
            result = await self.fav_executor.settle(  # 仓库现有属性名为 fav_executor(plugin.py:85)
                user_id, history, kind, model=self.config.favorability.llm_model,
                persona=persona, behavior_style=style,
            )
            if result.get("status") == "ok":
                self.ctx.logger.info("好感度结算 %s:%s delta=%s", user_id, kind, result.get("delta"))
                if result.get("exclusive_clamped"):
                    self.ctx.logger.warning("结算升特别被独占钳制(user=%s)", user_id)
            else:
                logger.debug("结算未落库 user=%s kind=%s status=%s 原因=%s", user_id, kind, result.get("status"), result.get("error") or result.get("reason"))
                self.ctx.logger.warning("好感度结算失败 %s:%s %s", user_id, kind, result.get("error") or result.get("reason"))
        finally:
            self._settling.discard(key)

    async def _side_llm_call(
        self, messages: list[dict], model: str, module: str, timeout_ms: int | None = None
    ) -> dict:
        """旁路 LLM 统一出口(规格 §4.10):model 填主程序 task 名;用量按模块记账。

        经 call_capability 直调,超时由各能力配置节传入(0/None=主程序默认 30s;
        配置默认值用 0 而非 None——主机配置回写经 tomlkit 序列化,None 会致激活失败)。
        联调实测:utils 模型 31-53s 会触发默认超时,慢模型建议配置 120000。
        """

        result = await self.ctx.call_capability(
            "llm.generate", timeout_ms=timeout_ms or None, prompt=messages, model=model or ""
        )
        if isinstance(result, dict):
            if "model" not in result and result.get("model_name"):
                result = {**result, "model": result["model_name"]}
        self._record_llm_usage(module, result)
        return result

    def _record_llm_usage(self, module: str, result: dict) -> None:
        """旁路调用记账(规格 §4.10 可观测性):次数+token 按日/模块分列;超阈值告警。"""

        day = datetime.now().strftime("%Y-%m-%d")
        tokens = 0
        if not isinstance(result, dict):
            return
        usage = result.get("usage")
        if isinstance(usage, dict):
            tokens = int(usage.get("total_tokens") or 0)
        elif result.get("total_tokens"):
            # capability payload 的 token 统计在顶层(联调实测)
            tokens = int(result.get("total_tokens") or 0)
        self.store.execute(
            """
            INSERT INTO llm_usage (day, module, calls, tokens) VALUES (?, ?, 1, ?)
            ON CONFLICT(day, module) DO UPDATE SET calls = calls + 1, tokens = tokens + excluded.tokens
            """,
            (day, module, tokens),
        )
        rows = self.store.query(
            "SELECT SUM(calls) FROM llm_usage WHERE day = ?", (day,)
        )
        total = int(rows[0][0] or 0)
        if total >= self.config.plugin.llm_daily_call_warning_threshold and self._llm_warned_day != day:
            self._llm_warned_day = day  # 跨越阈值当天只告警一次(复核 Minor:防每次调用刷屏)
            self.ctx.logger.warning("旁路 LLM 当日调用次数已达或超过阈值 %s,请注意用量", total)

    async def _fetch_recent(self, stream_id: str, limit: int) -> list[dict]:
        """取近期消息。spike ④ 实测:返回 list;image 段仅 hash。

        公测发现:include_binary_data=True 时,含大附件(数十 MB)的消息会把 RPC
        响应帧撑爆(主机 16MB 上限,E_UNKNOWN)——插件消费方(衰减互动判定/说话人
        解析/结算素材)只用文本与元数据,二进制一律不取。
        方案 B(2026-08-31)后注入消息 timestamp=阅读时刻,宿主默认 24h 窗天然适用。
        """

        result = await self.ctx.call_capability("message.get_recent", chat_id=stream_id, limit=limit)
        return result if isinstance(result, list) else []

    async def _resolve_quote_sender(self, stream_id: str, reply_to_id: str) -> tuple[str | None, str]:
        """经主机能力 message.get_by_id 解析 reply 消息原发送者(实机:reply 段为纯消息 id)。

        能力签名:call_capability("message.get_by_id", message_id=<id>, chat_id=<stream_id>),
        返回序列化消息 dict(含 message_info.user_info.user_id),SDK 解包键 "message";
        失败返回 (None, 原因),不抛异常——原因供调用方每轮至多一条 warning。
        """

        try:
            result = await self.ctx.call_capability(
                "message.get_by_id", message_id=reply_to_id, chat_id=stream_id
            )
        except Exception as exc:  # noqa: BLE001
            # 仅记异常类型,不插值 exc 本体(安全复审纪律,同 decay.py)
            return None, f"能力调用异常({type(exc).__name__})"
        if result is None:
            return None, f"消息 {reply_to_id} 解析返回 None"
        if not isinstance(result, dict):
            return None, f"消息 {reply_to_id} 解析返回非 dict({type(result).__name__})"
        payload = result.get("message")
        if isinstance(payload, dict):
            result = payload  # SDK 解包键 "message"
        info = result.get("message_info") or {}
        user_info = info.get("user_info") or {}
        sender = str(user_info.get("user_id") or user_info.get("sender_id") or "")
        if not sender:
            return None, f"消息 {reply_to_id} 无发送者 user_id"
        return sender, ""

    async def _resolve_quote_senders(self, raw: list[dict], stream_id: str) -> tuple[dict[str, str | None], str]:
        """解析本流 bot 消息 reply 段(reply_to)的原发送者 user_id(同一流内批量先收集、逐条解析)。

        仅收集 bot 消息(消息归属按 message_info.user_info == bot_user_id)的 reply_to id,
        id 去重后逐个经 _resolve_quote_sender 解析;解析失败的 id 记 None(该条按未 quote 命中)。
        返回 (reply_to_id -> 发送者 user_id 或 None, 首个失败原因);失败原因空串 = 全部成功。
        告警由调用方打出(每轮至多一条),本函数不记日志。
        """

        bot_id = str(self.config.favorability.bot_user_id or "").strip()
        reply_ids: list[str] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            msg_info = m.get("message_info") or {}
            ui = msg_info.get("user_info") or {}
            uid = str(ui.get("user_id") or ui.get("sender_id") or "")
            rid = str(m.get("reply_to") or "")
            if bot_id and uid == bot_id and rid:
                reply_ids.append(rid)
        senders: dict[str, str | None] = {}
        first_err = ""
        for rid in dict.fromkeys(reply_ids):  # 同一 id 只解析一次
            sender, err = await self._resolve_quote_sender(stream_id, rid)
            senders[rid] = sender
            if err and not first_err:
                first_err = err
        if reply_ids:  # 解析观测日志(每次调用一条,低频:结算/衰减路径;仅计数,不落明文 QQ 号——安全复审)
            resolved = sum(1 for v in senders.values() if v)
            self.ctx.logger.info("quote 发送者解析: 成功 %d/%d(stream=%s)", resolved, len(senders), stream_id)
        return senders, first_err

    async def _fetch_recent_for_history(self, stream_id: str, limit: int, target_user_id: str = "") -> list[dict]:
        """取近期消息并归一化为 build_material 所需形状
        {role, user_id, stream_id, text, seq, ts, is_group, addressed}。

        spike ④ 实测:消息 dict 键含 message_id/timestamp/platform/message_info/raw_message/
        is_*/session_id/processed_plain_text;user 在 message_info.user_info。
        role:message_info.user_info.user_id == bot_user_id 判为 bot(配置留空则一律 user)。
        addressed 仅群聊 bot 消息有意义,两类命中(规格 §3.1 互动定义):
        raw_message 存在 type=at 且 target_user_id == 结算目标用户;或 bot 消息 reply 段
        (实机为纯消息 id,不含发送者)经主机能力 message.get_by_id(message_id=reply 段,
        chat_id=当前流)解析出的原发送者 == 结算目标用户(同一流内批量先收集 reply_to id、
        逐条解析,id 去重)。
        解析失败(能力调用异常/返回 None/缺 user_id)该条按未 quote 命中,每轮(每次调用
        本函数)至多打一条 warning「quote 发送者解析失败(stream=…):…」,不静默;
        私聊流 bot 消息不读 addressed(build_material 私聊全随附),设为 None。
        """

        await self._refresh_stream_cache()  # is_group 判定依赖流缓存,先刷新再归一化
        is_group = self._stream_is_group(stream_id)
        raw = await self._fetch_recent(stream_id, limit)
        bot_id = str(self.config.favorability.bot_user_id or "").strip()
        # quote 语义(最终审查 I2 恢复):群聊 bot 消息 reply 段先批量收集、再逐条解析原发送者
        senders: dict[str, str | None] = {}
        if is_group and target_user_id:
            senders, first_err = await self._resolve_quote_senders(raw, stream_id)
            # warning 粒度为每次调用本函数至多一条;衰减路径为整轮至多一条(decay_quote_warned),
            # 两处粒度差异系调用频率差异,勿视为不一致缺陷
            if first_err:
                self.ctx.logger.warning("quote 发送者解析失败(stream=%s):%s", stream_id, first_err)
        history: list[dict] = []
        for i, m in enumerate(raw):
            if not isinstance(m, dict):
                continue
            text = str(m.get("processed_plain_text") or "")
            if not text:
                # 兜底:从 raw_message 段拼文本(text 段 data 直接是字符串,spike ③)
                text = "".join(s.get("data", "") for s in (m.get("raw_message") or []) if isinstance(s, dict) and s.get("type") == "text")
            msg_info = m.get("message_info") or {}
            user_info = msg_info.get("user_info") or {}
            user_id = str(user_info.get("user_id") or user_info.get("sender_id") or "")
            role = "bot" if bot_id and user_id == bot_id else "user"
            addressed: bool | None = None
            if role == "bot" and is_group and target_user_id:
                at_hit = any(
                    isinstance(seg, dict) and seg.get("type") == "at"
                    and str((seg.get("data") or {}).get("target_user_id") or "") == target_user_id
                    for seg in (m.get("raw_message") or [])
                )
                # quote 命中:reply 段解析出的原发送者 == 结算目标(解析失败该 id 记 None,不命中)
                rid = str(m.get("reply_to") or "")
                quote_hit = bool(rid) and senders.get(rid) == target_user_id
                addressed = at_hit or quote_hit
            history.append({
                "role": role,
                "user_id": user_id,
                "stream_id": stream_id,
                "text": text,
                "seq": i,
                "ts": self._normalize_ts(m.get("timestamp")),
                "is_group": is_group,
                "addressed": addressed,
            })
        if bot_id:
            bot_n = sum(1 for h in history if h["role"] == "bot")
            self.ctx.logger.info("结算取数: 共 %d 条,其中 bot 发言 %d 条(bot_user_id=%s)", len(history), bot_n, bot_id)
        return history

    @staticmethod
    def _normalize_ts(raw_ts: Any) -> str:
        """消息时间戳归一化为 ISO(与 favorability.window_start 同格式,保证窗口过滤可比)。

        实机实测:主程序序列化的 timestamp 为 epoch 浮点(字符串);直接与 ISO window_start
        字符串比较恒 False,导致批次素材恒空(联调发现)。
        """

        if raw_ts is None:
            return ""
        try:
            return datetime.fromtimestamp(float(raw_ts)).strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError, OSError):
            self.ctx.logger.warning("消息时间戳归一化失败,该消息将被窗口过滤排除: %r", raw_ts)
            return str(raw_ts)

    async def _fetch_message_text(self, stream_id: str, message_id: str) -> str:
        raw = await self._fetch_recent(stream_id, 50)
        for m in raw:
            if isinstance(m, dict) and m.get("message_id") == message_id:
                return str(m.get("processed_plain_text") or "")
        return ""

    @staticmethod
    def _api_ok(result: Any) -> bool:
        """napcat API 返回格式实测为 {'status': 'ok', ...}(非 success 字段)。"""

        if isinstance(result, dict) and result.get("success"):
            return True
        return isinstance(result, dict) and str(result.get("status") or "").lower() in ("ok", "success")

    def _output_items(self, kwargs: dict[str, Any]) -> list[dict]:
        items = kwargs.get(self._OUTPUT_ITEMS_KEY)
        return items if isinstance(items, list) else []

    def _called_tools(self, kwargs: dict[str, Any]) -> list[str]:
        """本轮 planner 调用过的工具名:从 output_items 的 FunctionCallItem 提取(实机快照格式)。"""

        items = self._output_items(kwargs)
        names: list[str] = []
        for it in items:
            if isinstance(it, dict) and it.get("item_type") == "FunctionCallItem":
                tc = it.get("tool_call") or {}
                name = str(tc.get("func_name") or "") if isinstance(tc, dict) else ""
                if name:
                    names.append(name)
        return names

    def _context_tool_results(
        self, output_items: list[dict], called_tools: list[str]
    ) -> dict[str, str]:
        """本轮**被调用过的**上下文工具的结果:从 output_items 的 FunctionCallOutputItem 提取(实机快照格式)。"""

        wanted = set(CONTEXT_TOOLS) & set(called_tools)
        results: dict[str, str] = {}
        for it in output_items:
            if isinstance(it, dict) and it.get("item_type") == "FunctionCallOutputItem":
                name = str(it.get("tool_name") or "")
                if name in wanted:
                    results[name] = str(it.get("output") or "")
        return results

    @staticmethod
    def _reasoning_from_items(output_items: list[dict]) -> str:
        """本轮 planner 推理文本:从 output_items 的 ReasoningItem 提取(实机快照格式)。"""

        parts: list[str] = []
        for it in output_items:
            if isinstance(it, dict) and it.get("item_type") == "ReasoningItem":
                for p in it.get("text_parts") or []:
                    if isinstance(p, str):
                        parts.append(p)
        return "".join(parts)

    def _setup_debug_logging(self) -> None:
        """debug 日志开关(公测复审):开启时把 catsitate.core 的 debug 级日志落盘到数据目录 logs/ 当日文件。

        文件权限 0600(日志含 user_id/stream_id,仅属主可读——安全复审);关闭时移除并
        关闭 handler、恢复 logger 原级别;文件创建失败显式告警,不静默。
        """

        plugin_logger = logging.getLogger("catsitate.core")
        if not self.config.debug.enabled:
            self._teardown_debug_logging()
            return
        if self._debug_handler is not None:
            return  # 已挂载,不重复
        logs_dir = self.ctx.paths.data_dir / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            path = logs_dir / f"catsitate-{datetime.now().strftime('%Y%m%d')}.log"
            self._debug_prev_level = plugin_logger.level
            handler = logging.FileHandler(path, encoding="utf-8")
            os.chmod(path, 0o600)  # 仅属主可读(安全复审:日志含用户标识)
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            plugin_logger.addHandler(handler)
            plugin_logger.setLevel(logging.DEBUG)
            self._debug_handler = handler
            self.ctx.logger.info("debug 日志已开启: %s", path)
        except OSError:
            self.ctx.logger.exception("debug 日志文件创建失败,回退仅主日志")

    def _teardown_debug_logging(self) -> None:
        """关闭 debug 日志:移除并 close handler、恢复 logger 原级别。

        on_unload 卸载与 debug 开关关闭共用(关闭分支语义抽离,审查 I5);
        on_unload 阶段配置对象仍可读,但直接按已挂载的 _debug_handler 清理即可。
        """

        plugin_logger = logging.getLogger("catsitate.core")
        if self._debug_handler is not None:
            plugin_logger.removeHandler(self._debug_handler)
            self._debug_handler.close()
            self._debug_handler = None
            plugin_logger.setLevel(self._debug_prev_level)  # 恢复开启前级别

    async def _persona_context(self) -> tuple[str, str]:
        """bot 人设与行为风格(经 config.get 读主程序全局配置 personality.personality / personality.behavior_style,带缓存)。

        缓存保证旁路 prompt 稳定段字节不变(前缀缓存友好);bot 配置变更时经 on_config_update 失效。
        读取失败或为空时人设兜底"猫耳少女"并显式告警(不静默);行为风格可为空(直接省略该段)。
        """

        if self._persona_cache is not None and self._style_cache is not None:
            return self._persona_cache, self._style_cache
        try:
            value = await self.ctx.config.get("personality.personality", "")
        except Exception:
            self.ctx.logger.exception("读取 bot 人设失败,兜底默认人设")
            value = ""
        self._persona_cache = str(value or "").strip() or "猫耳少女"
        if self._persona_cache == "猫耳少女" and not str(value or "").strip():
            self.ctx.logger.warning("bot 人设配置为空,结算/衰减/日程回退默认人设「猫耳少女」")
        try:
            style = await self.ctx.config.get("personality.behavior_style", "")
        except Exception:
            self.ctx.logger.exception("读取 bot 行为风格失败,省略该段")
            style = ""
        self._style_cache = str(style or "").strip()
        return self._persona_cache, self._style_cache

    async def _persona(self) -> str:
        """bot 人设文本(见 _persona_context)。"""

        persona, _ = await self._persona_context()
        return persona

    async def _recent_context_text(self, stream_id: str, limit: int) -> str:
        raw = await self._fetch_recent(stream_id, limit)
        lines = []
        for m in raw:
            text = str(m.get("processed_plain_text") or "")
            if not text:
                # 兜底:raw_message 段(data 键,spike ③/④)
                text = "".join(s.get("data", "") for s in (m.get("raw_message") or []) if isinstance(s, dict) and s.get("type") == "text")
            lines.append(f"[{m.get('message_id')}] {text}")
        return "\n".join(lines)


def create_plugin() -> CatsitatePlugin:
    """插件工厂(入口约定)。"""

    return CatsitatePlugin()
