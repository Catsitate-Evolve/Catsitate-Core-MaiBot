"""Catsitate 核心插件入口:薄接线层,业务逻辑全部在 catsitate_core 包内。"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
import time

import httpx

# 空间风控实证(2026-09-05):httpx 的 Python TLS/HTTP 指纹同 cookie 同参数下,
# 滚动窗口内零星请求即被空间网关判 -10001(network busy),同刻 curl_cffi 的
# Chrome 指纹恒过(真浏览器同样恒过)。curl_cffi 为可选依赖(容器 venv 预装),
# 缺失时回退 httpx 原行为,不阻断插件加载
try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
except ImportError:  # pragma: no cover - 部署环境未装时走 httpx 回退
    _CurlAsyncSession = None
from maibot_sdk import Command, HookHandler, MaiBotPlugin, MessageGateway, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo

# 实测结论:加载器仅将 plugins 父目录临时加入 sys.path,插件目录本身不在,
# 绝对导入 catsitate_core.* 会失败。在此自行注册插件目录(sys.path 修改限于插件进程内)。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from catsitate_core.config import CatsitateConfig
from catsitate_core.favorability import LEVELS, LEVEL_INDEX, EXCLUSIVE_LEVEL, BatchEngine, SettleExecutor, build_favorability_block
from catsitate_core.guard import compile_guard, match_guard
from catsitate_core.image_relook import build_relook_prompt, find_image_segment
from catsitate_core.inject import InjectAssembler, InjectionBlock
from catsitate_core.llm_provider import build_side_prompt, rpc_error_brief
from catsitate_core.memo import MemoService, validate_remind_at
from catsitate_core.migrations import read_db_version, run_migrations
from catsitate_core.msg_react import MsgReactEngine, parse_choice_resp
from catsitate_core.poke import PokeEngine
from catsitate_core.prompt_deploy import sync_prompt_templates
from catsitate_core.qzone import (
    QZONE_GATEWAY_NAME,
    QZONE_PLATFORM,
    QZONE_VIRTUAL_GROUP_ID,
    QZONE_VIRTUAL_GROUP_NAME,
)
from catsitate_core.qzone.protocol import FEED_APPID_SHUOSHUO, FeedItem, parse_friend_list
from catsitate_core.qzone.client import (
    BIZ_CODE_SERVER_BUSY,
    BIZ_CODE_TOO_FREQUENT,
    CookieManager,
    QzoneAuthError,
    QzoneBizError,
    QzoneClient,
    QzoneRateLimitError,
)
from catsitate_core.qzone.comment_seen import CommentSeenStore
from catsitate_core.qzone.discovery import FeedDiscovery
from catsitate_core.qzone.imaging import FeedImagePack, run_feed_image_pipeline
from catsitate_core.qzone.injector import FeedInjector
from catsitate_core.qzone.like_seen import LikeSeenStore
from catsitate_core.qzone.messages import (
    build_feed_message,
    build_notify_message,
    clip_text,
    format_comment_block,
    comment_time_prefix,
    format_comment_param_line,
)
from catsitate_core.qzone.expression import polish_action_text
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
    apply_schedule_delete,
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
    solar_term_on,
    solar_terms_near,
)

logger = logging.getLogger("catsitate.core")

SNAPSHOT_CACHE_MAX = 256  # 快照项缓存条数上限(超限 LRU 逐最旧)

# 天气快照新鲜度上限(小时):环境刷新持续失败(网络断/接口故障)时,旧快照不得
# 被无限期当「当前天气」注入环境块/日程/日记素材。默认 30 分钟刷新一次,6 小时
# ≈连续 12 个刷新周期全失败,足以判定数据已不代表当前天气;刷新失败本身已有
# 告警,消费侧过期不再重复 warning 防刷屏
WEATHER_MAX_AGE_HOURS = 6

# 发现层共享缓存 TTL(秒):浏览层(15 分钟/次)与通知源B(120 秒/次)原本各自
# 直调统一时间线端点(同端点同口径、首页均无游标),合计约 860 次/天持续触发
# 服务端限流(-10001 network busy);两处合并走发现层统一入口后,缓存命中即免
# 请求,端点调用降到约 144 次/天(600 秒内至多 1 次真实拉取)
DISCOVERY_CACHE_TTL_SECONDS = 600

# 发现层限流退避(秒):该端点风控窗口为分钟~小时级,15 分钟浏览节奏重试即持续
# 撞墙(生产实机持续复现);30 分钟退避期内浏览与通知源B 共享零请求,期满自动
# 恢复探测(退避进入告警单次化,恢复成功再打 info)
DISCOVERY_RATE_LIMIT_BACKOFF_SECONDS = 1800

# 空间互动事件 kind → 结算素材标签(未知 kind 兜底「空间互动」)
QZONE_FAV_EVENT_LABELS = {
    "COMMENT": "评论了你的说说",
    "LIKE": "赞了你的说说",  # 源C:入站赞事件
    "OUT_COMMENT": "你评论了TA",
    "OUT_LIKE": "你点赞了TA",
}

# 通知注入重试上限:同一通知被宿主拒绝/注入异常后经软回退
# 重发现的次数上限,超过则保留登记放弃——防宿主持续拒绝时每轮询周期无限重注入
QZONE_NOTIFY_MAX_RETRIES = 3


class _ModuleLogForwarder(logging.Handler):
    """把 catsitate_core.* 模块日志转发到插件 ctx logger。

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
    _reply_style_cache: str | None = None  # bot 表达方式(personality.reply_style)缓存(表达润色用)
    _debug_handler: logging.Handler | None = None  # debug 日志文件 handler(配置开关控制)
    _debug_prev_level: int = logging.NOTSET  # 开启前 logger 级别(关闭时恢复)
    _module_log_forwarder: logging.Handler | None = None  # 模块日志转发(on_load 挂载,unload 清理)
    _llm_warned_day: str = ""  # 旁路 LLM 用量告警的已告警日期(跨越当天只告警一次)
    _qzone_session_ids: set[str] = set()  # 虚拟流 session(运行时收集;豁免判定用)
    _last_speaker_map: dict[str, str] = {}  # 流→最近真实说话人(群聊 memo 归属兜底;内存映射,重启丢失可接受)
    _qzone_warned: set[str] = set()
    _qzone_group_prompt_value: str = ""
    _qzone_group_prompt_at: float = 0.0
    _qzone_available: bool = False  # 启动自检+网关就绪后置 True
    _qzone_seq: int = 0  # message_id 序号(on_load 以当前秒播种,防跨重启撞车触发宿主去重)
    _qzone_registry: FeedContextRegistry = FeedContextRegistry()  # 注入上下文追踪(工具目标解析;on_load 实例级重置)
    _qzone_notify_task_armed: bool = False  # 统一通知轮询调度任务已注册标记(热重载重注册防重)
    _qzone_poll_running: bool = False  # 浏览轮询后台拉取进行中(tick 防重入标记)
    _qzone_last_fetch_at: float = 0.0  # 上次实际拉取的 monotonic 时刻(on_load 重置:monotonic 跨进程不可比)
    # 发现层统一入口共享态(浏览层与通知源B 共用一次请求源:单飞+缓存+限流退避)。
    # 缓存/游标/退避均为实例级单槽:类体只声明默认值(None/0.0),运行期实例赋值——
    # 双实例(多插件实例/测试)不得串扰,monotonic 跨进程不可比故 on_load 重置
    _qzone_discovery_cache: tuple[float, list] | None = None  # 共享缓存(monotonic 时刻, 首页列表)
    _qzone_discovery_home_cursor: str = ""  # 首页续页游标(与缓存列表同源;浏览层积压补全穿透翻页直发用)
    _qzone_discovery_backoff_until: float = 0.0  # 限流退避截止(monotonic;0=未在退避)
    _qzone_discovery_backoff_warned: bool = False  # 退避告警单次标记(退避期内只告警一次)
    # 单飞锁:on_load 实例创建。禁止类体 asyncio.Lock() 单例——跨实例串扰,
    # 且 Lock 绑定创建时的事件循环,热重载换循环后复用会报错
    _qzone_discovery_fetch_lock: asyncio.Lock | None = None
    _qzone_notify_running: bool = False  # 通知轮询后台扫描进行中(同上,通知 tick 独立标记)
    _decaying: bool = False  # 自然衰减进行中(醒后 spawn 与调度 tick 并发防重入,防 delta 双计)
    _daily_settle_running: bool = False  # 日终结算后台任务进行中(tick 防重入标记)
    _daily_decay_running: bool = False  # 自然衰减后台任务进行中(同上;与 _decaying 分层——本标记防重复派发,_decaying 防并发执行)
    _env_refresh_running: bool = False  # 环境刷新后台任务进行中(同上:weather/holiday 共用)
    _guard_compiled: list = []  # 内容护栏已编译正则(v1.0.0;on_load 按 guard.enabled 编译,编译失败整组置空并告警)
    _qzone_send_armed: str = ""        # 发布触发已武装的窗口标记 "{day}|{start}"
    _qzone_send_first_poll_done: bool = False  # 本窗口首轮拉取是否已完成
    _qzone_sourcec_empty_rounds: int = 0  # 源C 连续空解析轮数(锚点漂移观测线)
    _qzone_sourcec_drift_warned: bool = False  # 源C 锚点漂移告警已发标记(warn-once 去重;恢复有事件即复位)
    _qzone_curl_session: Any | None = None  # curl_cffi 持久会话(Chrome 指纹过空间风控;on_load 实例创建)

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

        # llm_call 为 2 参调用契约,经 lambda 包装 _side_llm_call(与 SettleExecutor 装配同模式)
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
        self._sleep_window_settled: str = ""  # 已处理(入睡/补生成)过的睡眠窗口 end 标记(防重复)
        # 睡眠期拦截消息缓冲(回顾报告素材);持久化防重启丢失(联调发现)
        self._sleep_review_buffer_snapshot = JsonSnapshot(data_dir / "sleep_review_buffer.json")
        # 入睡任务发布的日记正文(醒来回注虚拟流用);持久化防重启丢失
        self._pending_diary_snapshot = JsonSnapshot(data_dir / "qzone_pending_diary.json")
        # 空间见闻(read_qzone 窗口结束旁路 LLM 摘要的空间印象);持久化跨重启引用
        self._qzone_digest_snapshot = JsonSnapshot(data_dir / "qzone_digest.json")
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
        self._settling: set[str] = set()  # 结算并发防护键(按人,user_id)
        self._background_tasks: set[asyncio.Task] = set()  # 后台任务引用(卸载时统一取消)
        # QQ空间感知模块(网关/客户端/注入泵;启动自检失败则保持停用并已告警)
        self.qzone_seen = SeenStore(self.store)
        self.qzone_seen.ensure_schema()
        # 评论去重与好感度显式事件表(通知轮询/点赞/出站评论的数据源)
        self.qzone_comment_seen = CommentSeenStore(self.store)
        self.qzone_comment_seen.ensure_schema()
        # 源C:赞事件去重(「与我相关」流,同一人同一条说说只通知一次)
        self.qzone_like_seen = LikeSeenStore(self.store)
        # 数据迁移基线:ensure_schema 自愈(补缺表/列)完成后,按注册表推进
        # PRAGMA user_version;单步失败告警停止不阻断加载(版本停留,下次重试)
        _mig_from = read_db_version(self.store)
        _mig_steps = run_migrations(self.store, self.ctx.logger)
        if _mig_steps > 0:
            self.ctx.logger.info(
                "数据迁移完成: v%d→v%d, %d 步", _mig_from, read_db_version(self.store), _mig_steps
            )
        self.qzone_cookie = CookieManager(
            JsonSnapshot(data_dir / "qzone_cookies.json"),
            api_call=self.ctx.api.call,
            refresh_minutes=self.config.qzone.cookie_refresh_minutes,
        )
        # cookie 含登录凭据,文件存在则收紧为属主可读(比照 SQLiteStore
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
            # 写路径身份参数(opuin/qzreferrer/topicId.uin);为空时自检已停用模块,不会走到写路径
            bot_uin=str(self.config.favorability.bot_user_id or "").strip(),
        )
        # 工具驱动旧配置兼容:qzone_* 工具全域默认可用(不受白名单管理),
        # 白名单只管其余虚拟流工具;view_friend_feeds 缺席则虚拟流无法
        # 查看好友说说(仍检查);白名单里残留 qzone_*/reply 项提示可移除(不再消费)
        self._warn_qzone_tool_whitelist()
        self._validate_schedule_threshold()
        self.qzone_injector = FeedInjector(decision_window_s=self.config.qzone.decision_window_seconds)
        # seq 以当前秒播种:重启归零会让 qzone_{tid}_{seq} 与上一轮运行撞车,
        # 被宿主 driver_id:message_id 去重拒绝(静默丢注入)
        self._qzone_seq = int(time.time())
        # 工具驱动架构:注入上下文登记表实例级重置(类属性为共享可变态,按次加载初始化)
        self._qzone_registry = FeedContextRegistry()
        # 轮询后台任务防重入标记实例级重置(类属性共享可变态,卸载取消任务后不得残留 True)
        self._qzone_poll_running = False
        # 拉取间距时间戳重置:monotonic 基准随进程启动,残留旧值会错判「刚拉过」
        self._qzone_last_fetch_at = 0.0
        # 发现层统一入口共享态重置(同款理由:monotonic 跨进程不可比,残留缓存
        # 时刻/退避截止/告警标记会让新进程首轮错判「缓存新鲜」「仍在退避」)
        self._qzone_discovery_cache = None
        self._qzone_discovery_home_cursor = ""
        self._qzone_discovery_backoff_until = 0.0
        self._qzone_discovery_backoff_warned = False
        self._qzone_notify_running = False
        # curl_cffi 持久会话实例级重建(Chrome 指纹传输层,on_unload 关闭;缺依赖
        # 时保持 None,fetch 走 httpx 回退)。会话复用连接贴近真实浏览器,跨请求
        # 不再每轮新建 TLS 握手
        self._qzone_curl_session = _CurlAsyncSession(impersonate="chrome") if _CurlAsyncSession is not None else None
        # 衰减防重入标记实例级重置(同上;残留 True 会令醒后补跑衰减被永久跳过)
        self._decaying = False
        # 日终结算/衰减/环境刷新后台派发防重入标记实例级重置(同上;残留 True 永久跳过派发)
        self._daily_settle_running = False
        self._daily_decay_running = False
        self._env_refresh_running = False
        # 源C 解析观测线状态实例级重置(同上,类属性共享可变态)
        self._qzone_sourcec_empty_rounds = 0
        self._qzone_sourcec_drift_warned = False
        # 说话人映射实例级重置(类属性为共享可变态,按次加载初始化)
        self._last_speaker_map = {}
        # v1.0.0 内容护栏:on_load 装配编译正则(实例级重置后按 enabled 编译;
        # 未启用零编译;编译失败 warning 后整组置空——护栏失效但不阻断插件加载)
        self._assemble_guard()
        # 泵并发锁:_qzone_pump 两个入口(调度 tick/轮完成信号)整体互斥,防弹出-置位间隙双弹
        self._qzone_pump_lock = asyncio.Lock()
        # 发现层单飞锁:并发调用统一入口时只放行一次真实拉取(等待者共享缓存结果);
        # on_load 实例创建,防类体 asyncio.Lock() 单例的跨实例串扰与跨事件循环绑定
        self._qzone_discovery_fetch_lock = asyncio.Lock()
        # 模块日志转发:catsitate_core.* 的告警路由到插件 ctx logger,否则不可见
        self._module_log_forwarder = _ModuleLogForwarder(self.ctx.logger)
        _module_root = logging.getLogger("catsitate_core")
        _module_root.addHandler(self._module_log_forwarder)
        _module_root.setLevel(logging.INFO)
        self._qzone_available = await self._qzone_selfcheck()
        if self._qzone_available:
            await self._qzone_gateway_ready()
        self._scheduler = Scheduler(tick_seconds=60)
        self._scheduler.register("qzone_poll", max(self.config.qzone.poll_interval_minutes, 1) * 60, self._qzone_poll_tick)
        # 统一通知轮询(替代旧评论轮询):高频短间隔模拟推送通知,始终运行醒着即可;
        # 注册下限 30s 防风控,tick 内自检开关/睡眠/awaiting 占用/可用性
        self._scheduler.register(
            "qzone_notify_poll", max(self.config.qzone.notification_interval_seconds, 30), self._qzone_notify_poll_tick
        )
        self._qzone_notify_task_armed = True
        # 长 IO 任务(结算/衰减/环境刷新)注册 tick 派发入口,
        # tick 内只做防重入标记+_spawn_background_task 派发(比照 qzone_poll 模式),
        # 分钟级 LLM/HTTP 不再拖住同 tick 串行的 sleep_tick/schedule_tick 等任务
        self._scheduler.register("weather", max(self.config.time_aware.weather_refresh_minutes, 1) * 60, self._refresh_environment_tick)
        self._scheduler.register("holiday", 24 * 3600, self._refresh_environment_tick)
        # qzone 数据保留期清理:评论去重 30 天+seen 表 7 天,每日一次
        self._scheduler.register("qzone_data_prune", 24 * 3600, self._qzone_data_prune)
        self._scheduler.register("memo_cleanup", 3600, self._cleanup_memos)
        self._scheduler.register("daily_settle", max(self.config.favorability.window_hours, 1) * 3600, self._daily_settle_tick)
        self._scheduler.register("daily_decay", 24 * 3600, self._daily_decay_tick)  # 每日一次(与日终结算同 tick)
        self._scheduler.register("sleep_tick", 60, self._sleep_tick)
        self._schedule_generated: bool = False  # 当天日程是否为 LLM 生成(模板撑场为 False)
        self._schedule_tick_fired: dict[str, str] = {}  # day -> 已触发窗口 mark(day|start)
        # 触发去重持久化(联调发现:内存态重启后旧备忘重复注入)
        self._remind_fired_snapshot = JsonSnapshot(data_dir / "remind_fired.json")
        self._remind_fired: dict[str, str] = self._remind_fired_snapshot.load()  # remind:<id> -> 触发时刻
        self._scheduler.register("schedule_tick", 60, self._schedule_tick)
        self._scheduler.register("remind_fallback", 300, self._remind_fallback_tick)
        self._restore_schedule()  # 重启恢复当日日程与编辑历史
        self._setup_debug_logging()
        self._scheduler.start()
        # 首次环境数据立即刷新一次,避免环境块空缺到首个定时点(45 分钟);
        # 经 tick 派发入口走防重入守卫,与 weather 调度共用同一标记
        await self._refresh_environment_tick()
        try:
            from lunar_python import Solar as _solar_probe  # noqa: F401
        except ImportError:
            self.ctx.logger.warning("lunar-python 未安装:农历节日/节气不可用(公历回退链不受影响)")
        self.ctx.logger.info("catsitate_core 已加载:注入/备忘录/好感度/贴表情/戳一戳/reply补传/图片重看")

    def _validate_schedule_threshold(self) -> None:
        """schedule.speak_threshold_level 非法值显式告警并回退默认:threshold_met
        对未知等级取 99,非法配置会永久静默停用日程
        主动发言且无任何日志——违反错误显式暴露纪律。"""

        from catsitate_core.favorability import LEVEL_INDEX

        value = str(self.config.schedule.speak_threshold_level or "").strip()
        if value not in LEVEL_INDEX:
            self.ctx.logger.warning(
                "schedule.speak_threshold_level=%s 非法(合法值:%s),按默认「熟悉」处理——请修正配置",
                value, "/".join(LEVEL_INDEX),
            )
            self.config.schedule.speak_threshold_level = "熟悉"

    def _warn_qzone_tool_whitelist(self) -> None:
        """白名单语义告警(on_load 调用;不静默改配置,只提示)。

        2026-09-02 起 qzone_* 工具全域默认可用、不受 tool_whitelist 管理
        (不可剔除):白名单只管其余虚拟流工具;view_friend_feeds 缺席→虚拟流
        无法查看好友说说(仍检查);残留 qzone_*/已废弃 reply 项→提示可移除。"""

        if "view_friend_feeds" not in self.config.qzone.tool_whitelist:
            self.ctx.logger.warning(
                "qzone.tool_whitelist 缺少 view_friend_feeds(旧配置残留),"
                "虚拟流内将无法查看好友说说——请在配置中补入"
            )
        stale = [t for t in self.config.qzone.tool_whitelist
                 if t.startswith("qzone_") or t == "reply"]
        if stale:
            self.ctx.logger.warning(
                "qzone.tool_whitelist 含不再由白名单管理的项:%s"
                "(qzone_* 工具全域默认可用;reply 已废弃)——该项无效可移除",
                "/".join(stale),
            )

    async def on_unload(self) -> None:
        self._teardown_debug_logging()  # 卸载清理:debug handler 移除并 close、logger 级别恢复
        if self._module_log_forwarder is not None:
            logging.getLogger("catsitate_core").removeHandler(self._module_log_forwarder)  # 模块日志转发清理
        await self._scheduler.stop()
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._qzone_curl_session is not None:
            # 在后台任务取消之后再关:避免在途请求占用已关闭会话
            await self._qzone_curl_session.close()
            self._qzone_curl_session = None
        self.store.close()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载回调。

        scope="self" 刷新派生缓存、按新值重注册周期任务,并把各引擎的配置引用
        重指到新 config 节(SDK 热重载经 model_validate 重建整个 config 实例,
        on_load 时各引擎持有的旧节引用不重指会静默沿用旧值直到重启);
        scope="bot" 失效人设缓存。
        """

        del config_data, version  # 新配置已由 Runner 注入 self.config,这里只刷新派生缓存
        if scope == "self":
            self.assembler.reset()
            self._env_cache.clear()
            self._env_fetched_at = None
            self._snapshot_cache.clear()
            # 发现层共享缓存随热重载失效:discovery_count 热改后旧缓存页大小口径
            # 不再成立;续页游标与缓存列表同源,一并作废。限流退避态不清——限流
            # 是服务端状态,热重载清退避会在风控窗口内重新打 API 撞墙
            self._qzone_discovery_cache = None
            self._qzone_discovery_home_cursor = ""
            # 调度周期随配置热重载:weather/daily_settle 间隔取新值重注册
            # (注册的是 tick 派发入口,长 IO 后台执行)
            self._scheduler.unregister("weather")
            self._scheduler.register("weather", max(self.config.time_aware.weather_refresh_minutes, 1) * 60, self._refresh_environment_tick)
            self._scheduler.unregister("daily_settle")
            self._scheduler.register("daily_settle", max(self.config.favorability.window_hours, 1) * 3600, self._daily_settle_tick)
            # qzone 热重载比照 weather 模式:自检通过还需网关就绪上报,否则每次注入都会被宿主拒绝;
            # 拉取间隔取新值重注册(FeedInjector 的 decision_window 热刷新暂不支持)
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
            self._assemble_guard()  # 护栏随配置热生效:enabled/patterns 变更即重编译
            # 阈值校验随配置热重载:speak_threshold_level
            # 经热改注入非法值时同样显式告警+回退——旧实现只在 on_load 校验一次,
            # 热改坏值会静默停用日程主动发言直到下次重启
            self._validate_schedule_threshold()
            self._setup_debug_logging()  # debug 开关随配置热生效
            # 引擎配置引用重指(属性名以各引擎 __init__ 落库为准):睡眠/日程/备忘/
            # 好感度批次/衰减/贴表情/戳一戳引擎均在新节上读参;衰减器内嵌自己的
            # 批次引擎,同节一并重指
            self.sleep.config = self.config.sleep
            self.schedule_gen.cfg = self.config.schedule
            self.schedule_gen.sleep_cfg = self.config.sleep
            self.memo.config = self.config.memo
            self.fav_engine.config = self.config.favorability
            self.decay.config = self.config.favorability
            self.decay.engine.config = self.config.favorability
            self.react.config = self.config.msg_react
            self.poke.config = self.config.poke
            self.ctx.logger.info(
                "引擎配置引用已随热重载重指(sleep/schedule/memo/favorability/decay/msg_react/poke)"
            )
            self.ctx.logger.info("catsitate_core 配置已刷新,派生缓存已重置")
        elif scope == "bot":
            # personality 变化影响等级规则块注入与哨兵人设(下次渲染自动生效)
            self.assembler.reset()
            self._snapshot_cache.clear()
            self._persona_cache = None
            self._style_cache = None
            self._reply_style_cache = None

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
            return err  # 非法提醒时间显式返回给 LLM
        sid = stream_id or str(kwargs.get("stream_id") or "")
        # 私聊流官方 kwargs 自动注入可靠;群聊 user_id 常为空——以 fav_count 维护的
        # 最近说话人映射兜底(取数点;重启丢失可接受,_resolve_speaker 回退仍在)
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
            ToolParameterInfo(name="action", param_type="string", description="view(查看当前日程)/move(把某窗口挪到新时段)/add(新增活动)/delete(删除活动窗口)。日程按时间顺序排列,窗口序号以 view 输出为准。建议流程:编辑前先 view 看当前日程与窗口序号,编辑后再次 view 确认结果。常用示例:把睡眠窗口改成11:45到16:00 → action=move, window_index=view 中睡眠窗口的序号, start=11:45, end=16:00;新增下午听歌 → action=add, start=16:00, end=18:00, activity=和Hesitate_P一起听歌;新增刷空间时段 → action=add, start=20:00, end=21:00, activity=刷刷空间, read_qzone=true", required=True),
            ToolParameterInfo(name="window_index", param_type="integer", description="move/delete 时的窗口序号(view 结果每行开头数字)", required=False),
            ToolParameterInfo(name="start", param_type="string", description="move/add 的新开始时刻,HH:MM 格式如 11:45(自动按当天日期)", required=False),
            ToolParameterInfo(name="end", param_type="string", description="move/add 的新结束时刻,HH:MM 格式如 16:00(跨午夜自动次日)", required=False),
            ToolParameterInfo(name="activity", param_type="string", description="add 时的活动描述(如 和Hesitate_P一起听歌);move 时留空保持原活动", required=False),
            ToolParameterInfo(name="read_qzone", param_type="boolean", description="add 时可选:该窗口是否刷空间看好友动态(浏览 QQ空间);move 自动保留原窗口标记,无需传", required=False),
            ToolParameterInfo(name="send_qzone", param_type="boolean", description="add 时可选:该窗口是否发说说(分享 QQ空间);可与 read_qzone 同开;move 自动保留原窗口标记", required=False),
        ],
        visibility="visible",
    )
    async def update_schedule(self, action: str = "", window_index: int = 0, start: str = "", end: str = "",
                              activity: str = "", read_qzone: bool = False, send_qzone: bool = False,
                              **kwargs: Any) -> str:
        del kwargs
        if not self.config.plugin.enabled or not self.config.schedule.enabled:
            return "日程模块未启用。"
        if not self._schedule_data:
            return "今天还没有日程,等睡前一并安排吧。"
        if action == "view":
            return "当前日程(每行开头是窗口序号):\n" + schedule_overview_text(self._schedule_data)
        # 入参形态矫正:宿主不按声明类型矫型,模型可能传字符串形态的序号/布尔
        try:
            window_index = int(window_index)
        except (TypeError, ValueError):
            return "window_index 需为窗口序号整数(view 结果每行开头的数字)。"
        rq_flag, sq_flag = _tool_bool(read_qzone), _tool_bool(send_qzone)
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
                read_qzone=rq_flag, send_qzone=sq_flag,
            )
        elif action == "delete":
            data, err, history = apply_schedule_delete(
                self._schedule_data, window_index, self._schedule_edit_history,
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
            # 不落响应原文(安全考虑):仅记失败形态
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
            ToolParameterInfo(name="image_hash", param_type="string", description="图片hash(view_friend_feeds 返回的图标注,8位前缀即可)", required=False),
            ToolParameterInfo(name="question", param_type="string", description="针对图片的具体问题", required=True),
        ],
        visibility="visible",
    )
    async def inspect_image(self, message_id: str = "", image_index: int = 1, question: str = "",
                            image_hash: str = "", **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.image_relook.enabled:
            return "图片重看工具未启用。"
        if str(image_hash or "").strip():
            # hash 路径:覆盖非消息来源的图片——view_friend_feeds 等
            # 经 tool result media 入库的图片没有消息上下文,消息搜索必空手而归;
            # capability 无 LIKE 查询,database.get 拉表(Images 表量级有限)后插件侧
            # 前缀匹配。行为不变量:命中唯一才用;多命中/零命中/形态异常显式报错
            # 不猜,且不回退消息搜索(hash 语义失败换路会掩盖真实原因)。
            prefix = str(image_hash).strip()
            db_rows = await self.ctx.call_capability(
                "database.get", model_name="Images", filters={}, single_result=False,
            )
            if not isinstance(db_rows, list):
                msg = f"图片hash检索返回异常形态({type(db_rows).__name__}):{db_rows}"
                self.ctx.logger.error(msg)
                return msg
            matched = [
                r for r in db_rows
                if isinstance(r, dict) and str(r.get("image_hash") or "").startswith(prefix)
            ]
            if not matched:
                return f"未找到 hash 前缀 {prefix} 对应的图片"
            if len(matched) > 1:
                cand = ", ".join(f"{str(r.get('image_hash') or '')[:12]}…" for r in matched[:3])
                return f"hash 前缀 {prefix} 命中 {len(matched)} 张图片,存在歧义,请提供更长前缀(候选:{cand})"
            row = matched[0]
            if not row.get("full_path"):
                msg = f"图片 {prefix} 数据库记录缺 full_path:{row}"
                self.ctx.logger.error(msg)
                return msg
            # 命中唯一:合成图片段(hash 路径无消息上下文,段仅承载 hash/文件名),
            # 复用 message_id 路径的 full_path→读文件→relook 收尾链
            seg = {"type": "image", "hash": str(row.get("image_hash") or prefix),
                   "file_name": str(row.get("file_name") or "")}
            return await self._inspect_image_relook(question, seg, str(row["full_path"]))
        stream_id = str(kwargs.get("stream_id") or "")
        # 注入消息 timestamp=阅读时刻,天然落在宿主 24h 默认窗内,
        # 无需放宽取数窗(旧的 hours 放宽随之移除)
        recent = await self._fetch_recent(stream_id, limit=50)
        seg, err = find_image_segment(recent, message_id or None, image_index)
        if seg is None:
            self.ctx.logger.warning("inspect_image 失败:%s(stream=%s,message_id=%s)", err, stream_id, message_id)
            return f"取图失败:{err}"
        # 实测:image 段仅 hash 无 data;SDK 无 ctx.database 属性,直调 database.get 能力
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
        return await self._inspect_image_relook(question, seg, str(db_result["full_path"]))

    async def _inspect_image_relook(self, question: str, seg: dict, full_path: str) -> str:
        """重看收尾链(两路径共用):读 full_path 文件补 base64 → relook prompt
        → 旁路 LLM;读文件/LLM 失败显式返回并记日志(不静默)。"""

        image_path = Path("/MaiMBot") / full_path
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
            # 失败显式日志并返回失败(与哨兵层同款纪律);仅记异常类型防 PII
            msg = f"图片重看 LLM 调用异常({rpc_error_brief(exc)})"
            self.ctx.logger.warning(msg)
            return msg
        if not isinstance(result, dict) or not result.get("success"):
            # 不落响应原文(安全考虑):仅记失败形态
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
        "qzone_next",
        description="刷QQ空间时,主动翻开浏览队列的下一条说说(「看完了,继续刷」)。",
        brief_description="刷下一条",
        parameters=[],
        visibility="visible",
    )
    async def qzone_next(self, **kwargs: Any) -> str:
        """浏览窗口内主动刷下一条:释放当前 awaiting(这条已看完)再复用
        _qzone_pump 注入下一条(P1 通知优先)。不新造注入路径。

        门控顺序:流门控(防御非 qzone 流直调,正常经工具过滤已不可见)→
        窗口判定(不在浏览窗口直接拒)→ 队列判定(无积压即「见底」,省 pump)。
        见底时仍释放 awaiting:正在看一条但下面没了,「这条看完没下一条」也归
        见底,释放让泵状态干净——否则陈旧 awaiting 会卡住后续通知注入。"""
        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "")
        if (
            str(kwargs.get("platform") or "") != QZONE_PLATFORM
            and (not stream_id or stream_id not in self._qzone_session_id_set())
        ):
            # 防御:qzone_next 已由工具过滤在非 qzone 流剔除,此处兜底直接调用
            return "这个工具只在刷QQ空间时有用。"
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        if not self.qzone_injector.window_active:
            return "现在不在浏览窗口。"
        if self.qzone_injector.queue_size() == 0:
            # 无积压(P1/P2 都空):已看完当前且没有下一条,或正在看这条但下面
            # 没了——都归「见底」。有 awaiting 时一并释放,让泵状态干净
            # (on_turn_complete 对无 awaiting 是 no-op,统一调用最简洁)。
            self.qzone_injector.on_turn_complete(time.monotonic())
            return "队列见底了,没有更多动态。"
        # 有积压:先释放当前 awaiting 再 pump——next_to_inject 在 awaiting
        # 未释放时返回 None,顺序不可颠倒。
        self.qzone_injector.on_turn_complete(time.monotonic())
        await self._qzone_pump()
        return "已翻开下一条。"

    @Tool(
        "qzone_like",
        description="给好友说说点赞(QQ空间)。传 feed_id 精确指定(浏览消息尾部〔〕里或 view_friend_feeds 结果里都有说说ID);正在浏览动态时不传,默认对当前说说点赞。",
        brief_description="给说说点赞",
        parameters=[ToolParameterInfo(name="feed_id", param_type="string", description="目标说说ID(照抄消息尾部〔〕或 view_friend_feeds 结果里的说说ID;可选,缺省当前说说)", required=False)],
        visibility="visible",
    )
    async def qzone_like(self, feed_id: str = "", **kwargs: Any) -> str:
        # 全域工具(view_friend_feeds 提供说说ID 后任何聊天里都可互动);
        # 缺省目标依赖浏览态(awaiting_feed),非浏览流调用须显式带 feed_id
        del kwargs
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        target = str(feed_id or "").strip()
        if not target:
            awaiting = self.qzone_injector.awaiting_feed
            if awaiting is None:
                return "当前没有正在浏览的说说,想点赞请带上说说ID(可用 view_friend_feeds 查看好友说说)。"
            # 通知项取真实说说 tid(可点其原说说);无 origin_tid 的畸形通知显式拒绝
            target = awaiting.origin_tid or ("" if awaiting.source == "notify" else awaiting.tid)
            if not target:
                return "当前是互动通知且缺少原说说信息,无法点赞。"
        # 目标解析:registry → seen_store → awaiting(通知项不再拒赞——锚指向真实说说)
        fid, owner_uin, ctx = self._qzone_resolve_feed(target)
        if not fid:
            return f"未找到说说 {target[:12]},可能已过期,请核对消息尾部的说说ID。"
        try:
            # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次
            _, auth_err = await self._qzone_auth_retry(
                lambda: self.qzone_client.do_like(fid=fid, target_qq=owner_uin), "点赞")
            if auth_err:
                return f"点赞失败:{auth_err}。"
        except QzoneBizError as exc:
            self.ctx.logger.warning("QQ空间点赞业务错误(code=%s)", exc.code)
            if exc.code == BIZ_CODE_TOO_FREQUENT:
                return "点赞失败:QQ空间说操作太频繁——今天点赞够多了,先歇一歇别重试。"
            if exc.code == BIZ_CODE_SERVER_BUSY:
                return "点赞失败:QQ空间这会儿有点忙,稍后再试,不要立刻重试。"
            return f"点赞失败:QQ空间拒绝了这次点赞(code={exc.code}),先不要立刻重试。"
        except Exception:
            self.ctx.logger.exception("QQ空间点赞失败(tid=%s)", fid)
            return "点赞失败:远端接口异常,已记录日志。"
        # 记账(远端已成功,记账失败仅告警——错误显式暴露但不误报失败;与
        # qzone_comment/qzone_reply 同款 try 保护:SQLite 异常不得让已成功的
        # 远端点赞以失败收尾诱导重复点赞)
        try:
            self.qzone_seen.mark_interacted(fid)
            # dedup=False:工具路径每次调用对应一次真实远端动作,事件文本不含
            # feed 标识,同日去重会吞掉同好友第二次真实点赞的记账
            self.qzone_comment_seen.fav_event(
                owner_uin, "OUT_LIKE", f"你点赞了 {owner_uin} 的说说", dedup=False)
        except Exception:
            self.ctx.logger.exception("QQ空间点赞记账失败(远端已成功,仅告警)")
        # 空昵称回退 owner_uin:通知登记的 owner_nickname
        # 置空(评论者昵称与主人语义错位),回执不能显示空名——or 兜底补齐
        nickname = (ctx.owner_nickname if ctx else "") or owner_uin
        return f"点赞成功:{nickname} 的说说(说说ID={fid[:12]})"

    def _qzone_expression_call(self):
        """表达润色 llm_call 装配:旁路统一出口 + qzone 节模型/超时配置
        (三动作工具共用;partial 冻结 model/module/timeout,调用侧只传 messages)。"""

        return partial(self._side_llm_call, model=self.config.qzone.expression_llm_model,
                       module="qzone_expression", timeout_ms=self.config.qzone.expression_llm_timeout_ms)

    def _assemble_guard(self) -> None:
        """v1.0.0 内容护栏装配(on_load 调用,抽为独立方法供实例级行为测试):
        按 guard.enabled 编译正则到实例级 _guard_compiled;未启用零编译保持
        空列表(匹配恒 0,三工具与日记天然短路零行为变化);编译失败 warning
        后整组置空——护栏失效但不阻断插件加载(错误显式暴露)。"""

        self._guard_compiled = []
        if not self.config.guard.enabled:
            return
        _guard_list, _guard_err = compile_guard(self.config.guard.patterns)
        if _guard_err:
            self.ctx.logger.warning(
                "内容护栏正则编译失败,整组护栏失效(不拦截任何内容,请修正配置):%s", _guard_err
            )
            return
        self._guard_compiled = _guard_list

    async def _qzone_polish(self, draft: str, *, limit: int, scene: str) -> str:
        """表达润色:planner 草稿按人设+表达方式+场景语改写;失败告警后原样返回(草稿直发)。"""

        persona, _ = await self._persona_context()
        polished = await polish_action_text(
            self._qzone_expression_call(), persona=persona, voice=await self._voice_style(),
            draft=draft, scene=scene, limit=limit, logger=self.ctx.logger,
        )
        return polished or draft

    async def _qzone_auth_retry(self, call, label: str) -> tuple[Any, str]:
        """QzoneAuthError 同轮自愈(不得放弃本轮动作)。

        作废 cookie → 经 adapter 强制重取(NapCat 在线会话,免扫码)→ 原地重试
        一次;重取失败/重试仍失效返回 (None, 错误文本)交调用方生成显式回执。
        重试抛出的非 Auth 异常原样上抛(走调用方既有 Biz/Exception 分支)。"""

        try:
            return await call(), ""
        except QzoneAuthError:
            self.qzone_cookie.invalidate()
            cookies = await self.qzone_cookie.get()
            if not cookies:
                self.ctx.logger.warning("QQ空间%s遇登录态失效,cookie 重取失败(adapter 无有效登录态)", label)
                return None, "登录态失效且 cookie 重取失败——请检查 NapCat 的 QQ 登录状态"
            self.ctx.logger.warning("QQ空间%s遇登录态失效,cookie 已重取,本轮原地重试", label)
            try:
                return await call(), ""
            except QzoneAuthError:
                self.qzone_cookie.invalidate()
                self.ctx.logger.warning("QQ空间%s重取后仍登录态失效,cookie 再次作废", label)
                return None, "登录态失效,cookie 重取后仍未通过"

    @Tool(
        "qzone_comment",
        description="评论好友的QQ空间说说。content 填你想说的内容,用你平时的口吻直接写。",
        brief_description="评论说说",
        parameters=[
            ToolParameterInfo(name="feed_id", param_type="string", description="目标说说ID(照抄消息尾部「说说ID=」)", required=True),
            ToolParameterInfo(name="content", param_type="string", description="评论内容(≤200字,用你平时的口吻直接写)", required=True),
            ToolParameterInfo(name="at_user_id", param_type="string", description="回应评论时填评论者QQ,会自动@TA", required=False),
        ],
        visibility="visible",
    )
    async def qzone_comment(self, feed_id: str = "", content: str = "", at_user_id: str = "",
                            **kwargs: Any) -> str:
        """评论说说——bot 自主决定是否/如何评论(工具驱动,替代意图路由)。"""
        del kwargs  # 全域工具:view_friend_feeds 提供说说ID 后任何聊天里都可评论
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        if not feed_id.strip():
            return "缺少说说ID(浏览消息尾部〔〕里或 view_friend_feeds 结果里都有)。"
        content = content.strip()
        if not content:
            return "评论内容不能为空。"
        if len(content) > 200:
            return f"评论太长了({len(content)} 字,上限 200),请精简。"
        # 目标解析前置:纯本地查询(registry → seen_store → awaiting → 显式失败,
        # fid 回填全量 tid)——锚失效时在润色之前失败返回,旁路 LLM 润色不白耗
        fid, owner_uin, ctx = self._qzone_resolve_feed(feed_id)
        if not fid:
            return f"未找到说说 {feed_id[:12]},可能已过期,请核对消息尾部的说说ID。"
        # 表达润色:planner 草稿按人设+表达方式+场景语改写(失败以草稿直发)
        content = await self._qzone_polish(content, limit=200, scene="QQ空间里,想给好友的说说写一条评论")
        # v1.0.0 内容护栏:润色后的最终文本匹配(草稿直发形态同样覆盖),命中即
        # 拦截——零 API 调用零记账零 seen,回执明示规则编号
        hit = match_guard(self._guard_compiled, content)
        if hit:
            self.ctx.logger.warning(
                "内容护栏拦截:qzone_comment 命中规则%d,未发布(文本:%s...)", hit, content[:60]
            )
            return f"内容被拦截(命中规则{hit}),未发布。"
        # @ 前缀(napcat 适配器同格式,QQ 空间原生支持):nick 默认 QQ 号,
        # registry 有该评论者昵称则用昵称(通知场景回应评论最自然)
        at_nick = ""
        receipt_body = content  # 回执正文(不含 @ 标记,可读形态)
        if at_user_id.strip():
            at_uin = at_user_id.strip()
            at_nick = at_uin
            if ctx and ctx.commenter_uin == at_uin:
                at_nick = ctx.commenter_nickname or at_uin
            receipt_body = f"@{at_nick} {content}"
            content = f"@{{uin:{at_uin},nick:{at_nick},auto:1}}{content}"
        try:
            # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次
            _, auth_err = await self._qzone_auth_retry(
                lambda: self.qzone_client.do_comment(fid=fid, target_qq=owner_uin, content=content), "评论")
            if auth_err:
                return f"评论失败:{auth_err}。"
        except QzoneBizError as exc:
            self.ctx.logger.warning("QQ空间评论业务错误(code=%s)", exc.code)
            if exc.code == BIZ_CODE_TOO_FREQUENT:
                return "评论失败:QQ空间说操作太频繁——短时间内评论得够多了,先歇一歇别重试,等下次浏览时再互动。"
            if exc.code == BIZ_CODE_SERVER_BUSY:
                return "评论失败:QQ空间这会儿有点忙,稍后再试,不要立刻重试。"
            return f"评论失败:QQ空间拒绝了这次评论(code={exc.code}),先不要立刻重试。"
        except Exception:
            self.ctx.logger.exception("QQ空间评论失败(feed_id=%s)", fid[:16])
            return "评论失败:远端接口异常,已记录日志。"
        # 记账(远端已成功,记账失败仅告警——错误显式暴露但不误报失败)
        try:
            self.qzone_seen.mark_interacted(fid)
            self.qzone_comment_seen.note_bot_comment(fid, owner_uin, content, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
            fav_target = at_user_id.strip() or owner_uin
            fav_kind = "COMMENT" if at_user_id.strip() else "OUT_COMMENT"
            fav_text = f"你在 {owner_uin} 的说说下评论" + (f"并@了 {at_nick}" if at_user_id.strip() else "")
            # dedup=False:工具路径每次调用对应一次真实远端动作(同日去重会吞掉
            # 同好友第二次真实评论的记账)
            self.qzone_comment_seen.fav_event(fav_target, fav_kind, fav_text, dedup=False)
        except Exception:
            self.ctx.logger.exception("QQ空间评论记账失败(远端已成功,仅告警)")
        return f"评论成功,已发出:「{receipt_body}」"

    @Tool(
        "qzone_reply",
        description="回复好友在你说说下的评论或对你评论的回复(QQ空间楼中楼),任何聊天里都可用。feed_id/comment_id 照抄消息尾部〔〕、浏览动态评论区,或 view_friend_feed_detail 结果里的标注。",
        brief_description="回复评论(楼中楼)",
        parameters=[
            ToolParameterInfo(name="feed_id", param_type="string", description="目标说说ID(照抄消息尾部「说说ID=」)", required=True),
            ToolParameterInfo(name="comment_id", param_type="string", description="要回复的主评论ID(照抄消息尾部「评论ID=」)", required=True),
            ToolParameterInfo(name="content", param_type="string", description="回复内容(≤200字,用你平时的口吻直接写)", required=True),
        ],
        visibility="visible",
    )
    async def qzone_reply(self, feed_id: str = "", comment_id: str = "", content: str = "",
                          **kwargs: Any) -> str:
        """楼中楼回复——commentId+commentUin 二元组精确匹配主评论(联调实证)。

        全域工具:comment_id 锚来源=空间流通知参数行、
        浏览注入评论区,或 view_friend_feed_detail 的结果。"""
        del kwargs  # 全域工具:任何聊天流都可用
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        if not (feed_id.strip() and comment_id.strip() and content.strip()):
            return "说说ID、评论ID和回复内容都不能为空——评论ID 照抄消息尾部〔〕或 view_friend_feed_detail 结果里的标注。"
        content = content.strip()
        if len(content) > 200:
            return f"回复太长了({len(content)} 字,上限 200)。"
        # 目标解析前置(说说+楼中楼二元组,均为纯本地查询)——锚失效时在润色
        # 之前失败返回,旁路 LLM 润色不白耗
        fid, target_qq, ctx = self._qzone_resolve_feed(feed_id)
        if not fid:
            return f"未找到说说 {feed_id[:12]},可能已过期。"
        # 楼中楼二元组与 @ 目标解析:通知上下文只在锚匹配时采用——registry
        # 字段级合并会让被通知登记过的说说保留旧 comment_uin/commenter,
        # 无条件优先会把 comment_map 命中的「另一条评论」错挂到通知的
        # 主评论线程并 @ 错人):
        # ① 通知上下文且 ctx.comment_tid == comment_id(二元组精确对应本条评论)
        # ② comment_map(浏览注入/详情查看登记:comment_id → 主评论作者)
        # ③ 全 miss=锚过期/未查过详情,显式拒绝+指引(不猜测回退)
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        cid = comment_id.strip()
        notify_hit = bool(ctx and ctx.comment_tid == cid)
        map_entry = ctx.comment_map.get(cid) if ctx else None
        if notify_hit and ctx.comment_uin:
            comment_uin = ctx.comment_uin
        elif map_entry:
            comment_uin = map_entry[0]
        elif notify_hit:
            comment_uin = bot_uin  # 源B形态:被回复的主评论=bot 自己
        else:
            comment_uin = bot_uin  # 防御不可达(下方拒绝条件先行拦截)
        if not notify_hit and not map_entry:
            return (f"未找到这条评论(评论ID={cid[:12]} 锚可能已过期)——"
                    "先用 view_friend_feed_detail 查看该说说,照抄最新评论ID再回复。")
        # 表达润色:planner 草稿按人设+表达方式+场景语改写(失败以草稿直发)
        content = await self._qzone_polish(content, limit=200, scene="QQ空间里,想回复好友在你的说说下写的评论")
        # v1.0.0 内容护栏:润色后的最终文本匹配(草稿直发形态同样覆盖),命中即
        # 拦截——零 API 调用零记账,回执明示规则编号
        hit = match_guard(self._guard_compiled, content)
        if hit:
            self.ctx.logger.warning(
                "内容护栏拦截:qzone_reply 命中规则%d,未发布(文本:%s...)", hit, content[:60]
            )
            return f"内容被拦截(命中规则{hit}),未发布。"
        # @ 目标=正在对话的评论者/回复者(与二元组解耦:源B 回复线程头=bot 自己
        # 的评论,但 @ 的是回复者;仅锚匹配的通知才有评论者语境,否则 @ 主评论
        # 作者;前缀由 wire.build_reply_form 统一拼装)
        at_uin = ((ctx.commenter_uin if notify_hit and ctx else "")
                  or (map_entry[0] if map_entry else comment_uin))
        at_nick = ((ctx.commenter_nickname if notify_hit and ctx else "")
                   or (map_entry[1] if map_entry else "")
                   or at_uin or "好友")
        try:
            # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次
            _, auth_err = await self._qzone_auth_retry(
                lambda: self.qzone_client.do_reply(
                    fid=fid, target_qq=target_qq,
                    comment_tid=comment_id.strip(), comment_uin=comment_uin,
                    comment_nick=at_nick, content=content,
                    at_uin=at_uin, at_nick=at_nick),
                "楼中楼回复")
            if auth_err:
                return f"回复失败:{auth_err}。"
        except QzoneBizError as exc:
            self.ctx.logger.warning("QQ空间楼中楼回复业务错误(code=%s)", exc.code)
            if exc.code == BIZ_CODE_TOO_FREQUENT:
                return "回复失败:QQ空间说操作太频繁——这条评论今天回得够多了,别再重试这条,想说话等下次浏览时再说。"
            if exc.code == BIZ_CODE_SERVER_BUSY:
                return "回复失败:QQ空间这会儿有点忙,稍后再试,不要立刻重试。"
            return f"回复失败:QQ空间拒绝了这次回复(code={exc.code}),先不要立刻重试。"
        except Exception:
            self.ctx.logger.exception("QQ空间楼中楼回复失败(feed=%s,comment=%s)", fid[:12], comment_id)
            return "回复失败:远端接口异常,已记录日志。"
        try:
            # dedup=False:工具路径每次调用对应一次真实远端动作(同日去重会吞掉
            # 同好友第二次真实回复的记账)
            self.qzone_comment_seen.fav_event(
                at_uin, "COMMENT", f"你回复了 {at_nick} 的评论", dedup=False)
        except Exception:
            self.ctx.logger.exception("QQ空间楼中楼记账失败(仅告警)")
        return f"回复成功,已回复 {at_nick} 的评论:「{content}」"

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
        """发布说说——bot 自主决定是否/何时发,发布后自动注入虚拟流供后续互动引用;
        tid 流程(回注锚/seen/registry 锚定)沿用既有成果。"""
        del kwargs  # 全域工具:任何聊天里想分享心情都能发(qzone_post 自产内容无需锚)
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        content = content.strip()
        if not content:
            return "说说内容不能为空。"
        if len(content) > 500:
            return f"内容太长了({len(content)} 字,上限 500)。"
        # 回注昵称前置读取:_bot_echo_nickname
        # 读失败直接抛错(不兜底),若留在发布成功后的回注构造段,异常会让
        # 工具以失败收尾——远端已发布却谎报失败,诱导模型重复发布。前移两步:
        # 先置 do_publish 之前(失败时零发布调用);再前移到**润色之前**
        # ——昵称坏时连润色 LLM 也不必烧,异常原样上抛工具层。
        bot_echo_nickname = await self._bot_echo_nickname()
        # 表达润色:planner 草稿按人设+表达方式+场景语改写(失败以草稿直发)
        content = await self._qzone_polish(content, limit=500, scene="QQ空间里,想发一条自己的说说")
        # v1.0.0 内容护栏:润色后的最终文本匹配(草稿直发形态同样覆盖),命中即
        # 拦截——零 API 调用零记账零回注,回执明示规则编号
        hit = match_guard(self._guard_compiled, content)
        if hit:
            self.ctx.logger.warning(
                "内容护栏拦截:qzone_post 命中规则%d,未发布(文本:%s...)", hit, content[:60]
            )
            return f"内容被拦截(命中规则{hit}),未发布。"
        try:
            # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次
            tid, auth_err = await self._qzone_auth_retry(
                lambda: self.qzone_client.do_publish(content=content), "说说发布")
            if auth_err:
                return f"发布失败:{auth_err}。"
        except QzoneBizError as exc:
            self.ctx.logger.warning("QQ空间说说发布业务错误(code=%s)", exc.code)
            if exc.code == BIZ_CODE_TOO_FREQUENT:
                return "发布失败:QQ空间说操作太频繁——今天发得够多了,先歇一歇,想发等下次窗口。"
            if exc.code == BIZ_CODE_SERVER_BUSY:
                return "发布失败:QQ空间这会儿有点忙,稍后再试,不要立刻重试。"
            return f"发布失败:QQ空间拒绝了这次发布(code={exc.code}),先不要立刻重试。"
        except Exception:
            self.ctx.logger.exception("QQ空间说说发布失败")
            return "发布失败,已记录日志。"
        if not tid:
            # 发布已远端成功,仅回注缺锚(seen/registry 登记跳过)——显式告警,不误报失败
            self.ctx.logger.warning("QQ空间说说发布成功但响应未含新说说 tid,回注缺锚")
        # 回注:发布成功的说说以 self 消息注入虚拟流(不触发 planner 决策轮,仅入历史)。
        # 原因:后续好友评论此说说时通知轮询只带说说ID,bot 需要这段历史才知道自己发过什么;
        # 回注正文带全文(不截断)——完整上下文锚。
        # 尾部锚〔说说ID=前12位〕与浏览注入同款:模型照抄锚值即可对该说说评论/点赞
        # (registry 前缀解析口径);tid 缺失时无锚。
        # 回注失败不影响回执:说说已远端发布成功,谎报失败会诱导重复发布。
        self._qzone_seq += 1
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        echo_msg = {
            "message_id": f"qzone_self_{int(time.time())}_{self._qzone_seq}",
            "platform": QZONE_PLATFORM,
            "timestamp": str(int(time.time())),
            "message_info": {
                # 发送者=bot 自己:昵称用 bot 名(主程序对自身消息按 bot 名显示,
                # 且 [bot].platforms 声明后自身判定命中),标「我」会被当普通用户
                # (昵称已发布前读取,此处不再有可抛错的 await)
                "user_info": {"user_id": bot_uin, "user_nickname": bot_echo_nickname},
                "group_info": {
                    "group_id": QZONE_VIRTUAL_GROUP_ID,
                    "group_name": QZONE_VIRTUAL_GROUP_NAME,
                },
                # 不设 is_mentioned:这是 bot 自己发的,不需要触发 planner 决策轮
            },
            "raw_message": [{"type": "text",
                             "data": f"我发布了一条说说:{content}" + (f"\n〔说说ID={tid[:12]}〕" if tid else "")}],
        }
        try:
            await self.ctx.gateway.route_message(QZONE_GATEWAY_NAME, echo_msg)
            # 本地锚定三连(seen + registry):自己发布的说说也进已见库与注入上下文
            # 追踪——own-post 摘要(seen 引用)与自锚(registry,模型对自己
            # 说说评论/点赞)依赖此登记。仅 route 成功才锚定(与日记补注路径对称:
            # 注入异常时无 message_id 可挂,锚定必落空);失败仅
            # 告警,远端已成功不回滚回执。
            if tid:
                try:
                    self.qzone_seen.mark_queued(tid, abstime=str(int(time.time())), author_uin=bot_uin,
                                                summary=content, author_nickname="我")
                    self.qzone_seen.mark_seen(tid, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                                              echo_msg["message_id"])
                    self._qzone_registry.register(FeedContext(
                        tid=tid, owner_uin=bot_uin, owner_nickname="我", kind="self",
                        content_summary=content,
                    ))
                except Exception:
                    self.ctx.logger.exception("QQ空间说说发布后本地锚定失败(远端已成功,仅告警)")
        except Exception:
            self.ctx.logger.exception("QQ空间说说回注失败(发布已成功,仅上下文注入失败)")
        self.ctx.logger.info("QQ空间说说发布成功: %s", content[:30])
        return f"发布成功,已发出说说:「{content}」" + (f"(说说ID={tid[:12]})" if tid else "")

    @Tool(
        "view_friend_feeds",
        description="查看指定QQ好友最近的QQ空间说说(正文+图片,不含评论区——看某条说说的完整评论用 view_friend_feed_detail)。想了解某位好友近况时使用,任何聊天里都可用;想看更早的翻页即可。",
        brief_description="看好友说说",
        parameters=[
            ToolParameterInfo(name="qq", param_type="string", description="好友QQ号", required=True),
            ToolParameterInfo(name="count", param_type="integer", description="每页几条(默认3,上限10)", required=False),
            ToolParameterInfo(name="page", param_type="integer", description="页码(默认第1页=最近几条;想看更早的传2、3…;末页会提示没有更多)", required=False),
        ],
        visibility="visible",
    )
    async def view_friend_feeds(self, qq: str = "", count: int = 3, page: int = 1, **kwargs: Any) -> dict | str:
        """全域查看工具:任何聊天里都可看,也是 qzone_like/qzone_comment 等空间
        动作工具在真实聊天流里的参数来源(view_friend_feeds 结果带说说ID)。

        回 dict 而非 str:图片以 content_items(content_type=image+base64)经 tool
        result media 返回,宿主入库 Images 表后可被 inspect_image 的 image_hash
        前缀反查;文本摘要逐图列 sha256 前 8 位即该前缀的来源。成功即 registry
        登记(content_summary=正文全文;comment_map=评论级锚,qzone_reply 目标解析用)。
        """
        del kwargs  # 全域工具:不限会话(真实流/虚拟流都只读不写)
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        qq = str(qq).strip()
        if not qq.isdigit():
            return "请提供好友的QQ号(纯数字)。"
        count = max(1, min(int(count or 3), 10))
        try:
            # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次;
            # nickname 无上下文可用,传 QQ 号占位(仅请求参数,不影响拉取)
            feeds, auth_err = await self._qzone_auth_retry(
                lambda: self.qzone_client.get_user_feeds(target_uin=qq, nickname=qq, num=count, page=page),
                "好友说说查看")
            if auth_err:
                return f"查看失败:{auth_err}。"
        except Exception:
            self.ctx.logger.exception("QQ空间好友说说拉取失败(qq=%s)", qq)
            return "拉取失败,已记录日志。"
        if not feeds:
            # 空页形态分言:第 1 页空=该好友没有可见说说;翻页空=到底了(诚实,不编造)
            if int(page) > 1:
                return f"{qq} 的说说没有更多了(第 {int(page)} 页为空)。"
            return f"{qq} 最近没有可见的说说。"
        # 可读性格式(2026-09-02):同主免重复——所有条目同作者,头部点名一次;
        # 条目编号+发布时间前缀(浏览注入同款相对时间)分清新旧;正文超长截断;
        # 说说ID/图片hash 锚契约不变(模型照抄锚值即可评论/点赞/看图)
        nickname = feeds[0].nickname or qq
        now_epoch = time.time()
        text_parts: list[str] = [f"{nickname}(QQ:{qq})最近的说说,共 {len(feeds)} 条:"]
        content_items: list[dict] = []
        for idx, f in enumerate(feeds, start=1):
            self._qzone_registry.register(FeedContext(
                tid=f.tid, owner_uin=qq, owner_nickname=f.nickname or qq, kind="feed",
                content_summary=f.content or "(纯图)",
                comment_map={c.comment_tid: (c.uin, c.nickname) for c in f.comments},
            ))
            # 图片公共管线:下载(失败跳过+
            # 告警)→多图角标合成→压缩预算,恒单图 content_item;[:3] 截断已删除
            # (合成后无 media 爆炸面,QQ 上限 9 图自然封顶)
            pack = await run_feed_image_pipeline(
                f.image_urls,
                downloader=self.qzone_client.download_image,
                log=self.ctx.logger, scene="好友说说",
            )
            content_items.extend(_pack_image_content_items(pack))
            body = f.content or "(纯图)"
            if len(body) > 300:
                body = clip_text(body, 300)  # 截断尾加"...",读的人知道还有下文
            line = f"〔{idx}〕{comment_time_prefix(f.abstime, now_epoch)}{body}"
            if pack.anchor:
                # 图标注:单图「图N(hash)」(N=原始序号)/多图
                # 「图1-图N(拼接,hash=…)」单条(不再逐图列 hash;hash=拟合后
                # 实际送出字节的 sha256 前 8——与 content_items 一致,
                # 保 inspect_image 前缀反查)
                line += "\n" + pack.anchor
            line += f"\n〔说说ID={f.tid[:12]}〕"
            text_parts.append(line)
        self.ctx.logger.info("QQ空间好友说说查看(qq=%s,%d 条,%d 图)", qq, len(feeds), len(content_items))
        return {"content": "\n\n".join(text_parts), "content_items": content_items}

    @Tool(
        "view_friend_feed_detail",
        description="查看某一条QQ空间说说的完整信息:正文+图片+全部评论和楼中楼。想看清一条说说的完整讨论、或要回复某条评论需要评论ID时使用;任何聊天里都可用。",
        brief_description="看说说详情和评论",
        parameters=[
            ToolParameterInfo(name="feed_id", param_type="string", description="目标说说ID(照抄消息尾部〔〕或 view_friend_feeds 结果里的说说ID)", required=True),
            ToolParameterInfo(name="qq", param_type="string", description="说说主人的QQ号(可省——省略时按说说ID自动定位主人,定位不到会提示补)", required=False),
        ],
        visibility="visible",
    )
    async def view_friend_feed_detail(self, feed_id: str = "", qq: str = "", **kwargs: Any) -> dict | str:
        """单条说说详情。

        返回正文+图片(content_items)+全部评论与楼中楼(楼中楼每条评论最多展开
        10 条+总数标注,整块超 6000 字截断标注)。feed_id 必填、qq 选填
        (缺省经 registry/seen 前缀解析主人)。查看即 mark_seen(浏览轮询
        不再把该说说当新动态注入);registry 登记 comment_map 供 qzone_reply 的
        评论级目标解析。"""
        del kwargs  # 全域工具:任何聊天流都可用(虚拟流需白名单含本工具)
        if not self._qzone_available:
            return "QQ空间模块未启用。"
        anchor_tid = str(feed_id or "").strip()
        if not anchor_tid:
            return "缺少说说ID——照抄消息尾部〔〕或 view_friend_feeds 结果里的说说ID。"
        # 主人定位:显式 qq 优先,缺省经三级解析拿 owner;两者皆空显式索要
        fid, owner_uin, ctx = self._qzone_resolve_feed(anchor_tid)
        owner = str(qq or "").strip() or owner_uin
        if not owner.isdigit():
            return f"未能定位这条说说的主人(说说ID={anchor_tid[:12]}),请带上好友的QQ号再查。"
        try:
            feeds, auth_err = await self._qzone_auth_retry(
                lambda: self.qzone_client.get_user_feeds(target_uin=owner, nickname=owner, num=20, page=1),
                "说说详情查看")
            if auth_err:
                return f"查看失败:{auth_err}。"
        except Exception:
            self.ctx.logger.exception("QQ空间说说详情拉取失败(owner=%s)", owner)
            return "拉取失败,已记录日志。"
        # 目标匹配:解析出的全量 tid 精确命中,否则按锚前缀(≥12 位)匹配
        target = next((f for f in feeds if f.tid == fid), None) if fid else None
        if target is None:
            target = next((f for f in feeds if f.tid.startswith(anchor_tid)), None)
        if target is None:
            return (f"说说 {anchor_tid[:12]} 不在 {owner} 最近 20 条内——"
                    "可能已删除,或比这更早(更早的没有查看通路,属当前已知限制)。")
        # registry 登记:正文全文+评论级锚(与浏览注入同款合并语义)
        self._qzone_registry.register(FeedContext(
            tid=target.tid, owner_uin=owner, owner_nickname=target.nickname or owner, kind="feed",
            content_summary=target.content or "(纯图)",
            comment_map={c.comment_tid: (c.uin, c.nickname) for c in target.comments},
        ))
        # 查看即已见:落 seen 表(浏览轮询据此跳过;源A 评论扫描另一张表不受影响)
        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self.qzone_seen.mark_queued(
                target.tid, abstime=target.abstime or "", author_uin=owner,
                summary=target.content or "", author_nickname=target.nickname or "")
            # message_id=None:只置 seen 不抹 reply 段锚(浏览注入落的注入消息 id
            # 保留,后续该说说新评论通知仍可引用)
            self.qzone_seen.mark_seen(target.tid, now_iso, None)
        except Exception:
            self.ctx.logger.exception("QQ空间说说详情 seen 登记失败(仅告警)")
        # 图片公共管线(与 view_friend_feeds 同款):
        # 下载→多图角标合成→压缩预算,恒单图 content_item;[:3] 截断已删除
        pack = await run_feed_image_pipeline(
            target.image_urls,
            downloader=self.qzone_client.download_image,
            log=self.ctx.logger, scene="说说详情",
        )
        content_items = _pack_image_content_items(pack)
        # 文本:头部点名+时间前缀+全文正文(详情即「看完整」,正文不截断)+评论区+锚
        time_tag = comment_time_prefix(target.abstime, time.time())
        lines = [f"{target.nickname or owner}(QQ:{owner})的说说详情:"]
        body_line = f"{time_tag}{target.content or '(纯图)'}"
        if pack.anchor:
            body_line += "\n" + pack.anchor
        lines.append(body_line)
        comment_block = format_comment_block(
            target.comments, comment_total=target.comment_total, now_epoch=time.time())
        if comment_block:
            lines.append(comment_block)
        elif not target.comments:
            lines.append("评论区:还没有评论。")
        lines.append(f"〔说说ID={target.tid[:12]}〕")
        self.ctx.logger.info("QQ空间说说详情查看(owner=%s,评论%d条,图%d张)",
                             owner, len(target.comments), len(content_items))
        return {"content": "\n\n".join(lines), "content_items": content_items}

    # ---------- QQ空间(感知 / 互动) ----------

    async def _qzone_http_fetch(self, method: str, url: str, *, params: dict, headers: dict, timeout_ms: int, data: dict | None = None) -> tuple[int, bytes]:
        """插件传输层(client.py 的 fetch 注入点;超时与异常上抛由调用方告警)。

        传输策略(2026-09-05 空间风控实证):优先 curl_cffi `impersonate="chrome"`
        ——Chrome TLS/HTTP 指纹是过空间风控的必要条件,同 cookie 同参数下 httpx
        指纹在滚动窗口内零星请求即被 -10001(network busy)封锁,同刻 Chrome
        指纹与真浏览器恒过;大单页(count=50)即使 Chrome 指纹也会被单独封,
        页大小口径见 config。会话跨请求持久复用(keep-alive,更贴近真实浏览器);
        impersonation 自带成套浏览器头(UA/sec-ch-ua),外部 UA 头会破坏一致性
        故剥离,仅保留语义头(Cookie/Referer/Origin/Content-Type/Accept)。
        curl_cffi 缺失(部署环境未装)时回退 httpx 新建客户端(原行为)。

        统一返回 **bytes**:二进制图片经 resp.text 的 UTF-8 解码会失真,
        再 encode('latin-1') 必炸;文本/JSON 由 client 侧显式
        utf-8 解码。params 为空时必须传 None:httpx 的 params={} 会把 URL
        既有 query 整体清空——签名 URL 由此被剥签名致 404。
        data 为写路径表单(dict 时按 requests 语义 form-encode,
        Content-Type 由调用方 headers 指定);读路径保持 None。
        """

        if _CurlAsyncSession is not None:
            if self._qzone_curl_session is None:  # 防御:未经 on_load 的离线装配场景
                self._qzone_curl_session = _CurlAsyncSession(impersonate="chrome")
            headers = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
            resp = await self._qzone_curl_session.request(
                method, url, params=params or None, headers=headers,
                data=data, timeout=timeout_ms / 1000,
            )
            return resp.status_code, resp.content
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
            # 折叠失效=人物分裂不可接受:硬停用,不做降级分裂模式;
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
                self.ctx.logger.warning("QQ空间模块停用:主程序 experimental.focus_mode 开启(focus 槽会吞掉注入的强制触发)")
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
        """网关就绪上报(account 用真实 bot QQ:经别名折叠统一 bot 自身 person)。"""

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
        self.ctx.logger.info("QQ空间虚拟平台就绪(platform=%s,伪群=%s)", QZONE_PLATFORM, QZONE_VIRTUAL_GROUP_ID)

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
        """空间浏览轮询触发器:防重入后派发后台拉取,立即返回。

        调度器串行 await 各任务(60s tick),逐好友 2s sleep+HTTP 的长 IO 若在
        tick 内执行会拖住 sleep_tick/schedule_tick 等全部任务——长 IO 移入
        _qzone_poll_feeds 后台任务,守卫与窗口逻辑也随迁(单一事实源)。
        防重入分支先收窗再返回:窗口结束恰逢上一轮拉取未完时,收窗原本要等
        上一轮跑完+下个 tick,期间上一轮尾部的泵会照常注入浏览动态(时间纪律
        越界)——_qzone_maybe_close_read_window 幂等,不与后台轮竞争。
        """

        if self._qzone_poll_running:
            await self._qzone_maybe_close_read_window()
            return  # 上一轮后台拉取还在跑,跳过(防重入)
        self._qzone_poll_running = True
        self._spawn_background_task(self._qzone_poll_feeds())

    async def _qzone_maybe_close_read_window(self, *, in_read_window: bool | None = None) -> None:
        """浏览窗口收尾(幂等):窗口结束即持泵锁收窗、回退浏览队列未读,并派发
        见闻生成——从 _qzone_poll_feeds 抽出,供 tick 防重入分支先行收窗。

        为什么独立成方法:窗口结束若恰逢上一轮后台拉取仍在跑,收窗在旧位置要
        等上一轮跑完+下个 tick 才执行,期间上一轮尾部的 _qzone_pump 会因
        window_active 未翻转照常弹出 P2 注入——窗口结束后仍在注入浏览动态;
        先行收窗只翻标志不动数据(queued 行回退未读,不丢不重)。
        in_read_window 未给时按 _qzone_poll_feeds 开头同款前置(可用性/睡眠/
        日程)与 win/current_window 判定自算,防止窗口判定前置条件不一致;
        已收窗(window_active=False)则空转,重复调用安全。
        """

        if in_read_window is None:
            if not self._qzone_available:
                return
            if self.config.sleep.enabled and self.sleep.is_sleeping():
                return  # 睡眠绝对静默(与 poll_feeds 开头同款前置)
            if not self._schedule_data:
                return  # 日程未生成/未恢复:无窗口可言
            win = current_window(self._schedule_data, datetime.now().strftime("%Y-%m-%dT%H:%M"))
            in_read_window = bool(win and win.get("kind") == "daily" and win.get("read_qzone"))
        if in_read_window or not self.qzone_injector.window_active:
            return  # 仍在浏览窗口/已收窗:空转(幂等)
        # 浏览窗口结束(含 read→仅 send 邻接切换:send 窗口无浏览语义,
        # 浏览队列同样回退未读);通知队列保留 P1 等待注入。
        # 持泵锁收窗(防竞态):window_ended/revert_pending
        # 与在途泵(弹出→下载→route→mark_seen 的秒级 await 间隙)竞态会
        # 删掉在途动态的 queued 行/清掉已弹出 P1 状态——下窗口重复注入
        # 同一条说说、P1 被占位 awaiting 卡住
        async with self._qzone_pump_lock:
            self.qzone_injector.window_ended()
            reverted = self.qzone_seen.revert_pending()
        self.ctx.logger.info(
            "QQ空间浏览窗口结束,浏览队列回退未读(%d 条);通知队列保留等待注入", reverted
        )
        # 见闻生成:窗口边界把近 24h 滚动窗内的浏览与互动摘要为空间见闻,注入真实聊天
        self._spawn_background_task(self._qzone_generate_digest())

    def _qzone_enter_rate_limit_backoff(self) -> None:
        """进入发现层限流共享退避(warn-once):首页经共享层与翻页穿透路径撞
        -10001 时统一走此入口——退避期内全部消费方零请求,期满自动恢复探测。"""

        self._qzone_discovery_backoff_until = (
            time.monotonic() + DISCOVERY_RATE_LIMIT_BACKOFF_SECONDS
        )
        if not self._qzone_discovery_backoff_warned:
            self._qzone_discovery_backoff_warned = True
            self.ctx.logger.warning(
                "QQ空间发现层限流(network busy),进入 30 分钟退避"
                "(浏览与通知源B 共享,期间零请求),期满自动恢复"
            )

    async def _qzone_shared_discovery(self, count: int) -> tuple[str, list]:
        """发现层统一入口:单飞+共享缓存+限流退避(浏览层与通知源B 共用一次请求源)。

        浏览层(15 分钟/次)与通知源B(120 秒/次)原本各自直调统一时间线端点
        (同端点同口径、首页均无游标),合计约 860 次/天持续触发服务端限流
        (-10001);两处消费合并为一次请求源后,共享缓存命中即免请求,限流则共享
        同一退避窗口。返回 ("ok", 列表) 或 ("rate_limited", []);QzoneAuthError
        与其他异常原样上抛(浏览层 legacy 回退/源B 跳过的既有处置依赖透传)。
        带游标的翻页调用不经过本层(浏览层积压补全穿透直发)。锁内只做 client
        IO,不触泵与注入器。
        """

        now = time.monotonic()
        if now < self._qzone_discovery_backoff_until:
            return "rate_limited", []  # 退避期内静默(进入退避时已告警过一次)
        if (
            self._qzone_discovery_cache is not None
            and now - self._qzone_discovery_cache[0] < DISCOVERY_CACHE_TTL_SECONDS
        ):
            return "ok", self._qzone_discovery_cache[1]
        # 单飞锁按契约由 on_load 创建;离线装配等未跑 on_load 的场景就地补建
        # (容错一行,防 None 解引用)
        if self._qzone_discovery_fetch_lock is None:
            self._qzone_discovery_fetch_lock = asyncio.Lock()
        async with self._qzone_discovery_fetch_lock:
            # 双检:等锁期间 winner 可能已完成拉取或进入退避,等待者直接共享其结果,
            # 不再重复打端点
            now = time.monotonic()
            if now < self._qzone_discovery_backoff_until:
                return "rate_limited", []
            if (
                self._qzone_discovery_cache is not None
                and now - self._qzone_discovery_cache[0] < DISCOVERY_CACHE_TTL_SECONDS
            ):
                return "ok", self._qzone_discovery_cache[1]
            try:
                # 首页只传 count(与两消费方同口径,首页均无游标);带游标的翻页
                # 由消费方自行直发,不经本层
                discoveries, _cursor = await self.qzone_client.get_unified_timeline(count=count)
            except QzoneRateLimitError:
                # 限流退避(两消费方共享):风控窗口分钟~小时级,15 分钟节奏重试
                # 即持续撞墙——退避期内零请求,期满自动恢复探测;告警单次化
                # (退避期内静默返态,不重复刷 warning)
                self._qzone_enter_rate_limit_backoff()
                return "rate_limited", []
            # 首页列表与续页游标同源缓存:浏览层积压补全穿透翻页时取游标直发
            self._qzone_discovery_cache = (now, discoveries)
            self._qzone_discovery_home_cursor = str(_cursor or "")
            if self._qzone_discovery_backoff_until > 0:
                # 退避到期后首次成功真实拉取:复位退避与告警标记(再遇限流重新告警)
                self._qzone_discovery_backoff_until = 0.0
                self._qzone_discovery_backoff_warned = False
                self.ctx.logger.info("QQ空间发现层限流退避结束,恢复拉取")
            return "ok", discoveries

    async def _qzone_poll_feeds(self) -> None:
        """空间窗口内周期拉取(统一时间线架构);窗口切换时收泵并回退未读。

        浏览流三段式:①发现层 get_unified_timeline 逐页拉取(首页经统一入口
        _qzone_shared_discovery:与通知源B 共用一次请求源,单飞+共享缓存+限流
        退避;稳态首页全旧即止步恒 1 次调用,长时间离线后翻页补全积压)→
        ②过滤(说说 appid=311 且 seen 未登记的新 tid,is_new_candidate 纯查)→
        ③充实层按作者 uin 分组、每组 1 次 get_user_feeds 只拉有新动态的好友
        (1+N 次调用,N=有新动态的作者数,与好友总数无关)。
        发现层任何失败(限流/登录态/未知异常)一律告警后跳过本轮——绝不回退
        逐好友放大路径(风控帮凶,2026-09-05 生产实证)。
        send_qzone 窗口:仅 send 窗口(无 read)在窗口开始即派发
        发布触发后早退(无浏览语义);同窗形态(read+send)在首轮拉取收尾
        (含零新动态轮)派发发布触发(等泵空闲,分享有上下文)。
        """

        try:
            if not self._qzone_available:
                return
            if self.config.sleep.enabled and self.sleep.is_sleeping():
                return  # 睡眠绝对静默
            if not self._schedule_data:
                return  # 日程未生成/未恢复(日程节禁用等):无窗口可言,按窗口外处理
            win = current_window(self._schedule_data, datetime.now().strftime("%Y-%m-%dT%H:%M"))
            in_read_window = bool(win and win.get("kind") == "daily" and win.get("read_qzone"))
            in_send_window = bool(win and win.get("kind") == "daily" and win.get("send_qzone"))
            # 窗口收尾(窗口结束→持泵锁收窗+回退未读+见闻生成派发)抽为幂等辅助
            # 方法,与 tick 防重入分支共用;此处传入已算得的窗口判定,不自算
            await self._qzone_maybe_close_read_window(in_read_window=in_read_window)
            if not in_read_window and not in_send_window:
                return  # 非 qzone 窗口(自由时间/问候/睡眠):按窗口外处理
            # 发布触发武装:按窗口标识 "{day}|{start}" 判重——邻接
            # qzone 窗口间 window_active 不复位,泵状态不能当窗口切换信号
            day = datetime.now().strftime("%Y-%m-%d")
            armed_key = f"{day}|{win.get('start')}"
            if win.get("send_qzone"):
                if self._qzone_send_armed != armed_key:
                    self._qzone_send_armed = armed_key
                    self._qzone_send_first_poll_done = False
                    if not win.get("read_qzone"):
                        # 仅 send 窗口:无浏览上下文,窗口开始即触发
                        self._spawn_background_task(self._qzone_send_trigger(win, browsed=False))
            else:
                self._qzone_send_armed = ""
                self._qzone_send_first_poll_done = False
            if not in_read_window:
                return  # 仅 send 窗口:发布触发已发,无浏览语义,不做发现/充实
            if not self.qzone_injector.window_active:
                self.qzone_injector.window_started()
                # 回收跨窗口/跨启动的 queued 残留:注入泵队列在内存,重启即丢,
                # 而 seen 的 queued 行会让新轮拉取全部判重跳过
                stale = self.qzone_seen.revert_pending()
                if stale:
                    self.ctx.logger.info("QQ空间窗口开始,注入泵激活;回收跨启动 queued 残留 %d 条(重新拉取)", stale)
                else:
                    self.ctx.logger.info("QQ空间窗口开始,注入泵激活")
            # 拉取间距:间隔的语义是「两次拉取的间距」,不是与窗口无关的固定节奏
            # ——窗口开始的首轮由 _schedule_tick 进入窗口时立即派发,窗口内的
            # 后续刷新由定间隔任务承担;距上次实际拉取不足间隔时本轮跳过发现/
            # 充实(收窗判定与窗口激活在上方已照常执行),防进入拉取与节奏拉取
            # 相邻撞车。时刻在拉取尝试前打点:失败轮同样占距(防失败后连续重击);
            # 共享层缓存命中同样占距:维持浏览轮节奏,且限流退避与拉取间距自然叠加
            interval_s = max(self.config.qzone.poll_interval_minutes, 1) * 60
            now_mono = time.monotonic()
            if now_mono - self._qzone_last_fetch_at < interval_s:
                return
            self._qzone_last_fetch_at = now_mono
            # ① 发现层:统一时间线好友动态流(scope=2,7 天窗口)游标翻页
            # (2026-09-03 双路逆向+实机改造):续页游标=上页响应 main.begintime,
            # 仅此一参(refresh/pagenum/g_tk 均非必需);稳态第 1 页全旧即止步恒
            # 1 次调用,长时间离线后积压逐页回溯(空页/无新 tid/游标耗尽/页数上限
            # 四重终止)。旧 begin 偏移已被实证无视,删除
            page_size = max(self.config.qzone.discovery_count, 1)
            max_pages = max(self.config.qzone.discovery_max_pages, 1)
            discoveries: list[FeedDiscovery] = []
            try:
                # 首页经发现层统一入口(单飞+共享缓存+限流退避;与通知源B 共用
                # 一次请求源,缓存新鲜期浏览零端点调用);限流态按原文案告警后
                # 跳过本轮,不回退 legacy——限流期间逐好友 1→N+1 放大恰是火上浇油
                state, first_batch = await self._qzone_shared_discovery(page_size)
                if state == "rate_limited":
                    self.ctx.logger.warning("QQ空间服务限流(network busy),本轮浏览跳过,下轮再试")
                    return
                discoveries.extend(first_batch)
                has_new = any(
                    d.appid == FEED_APPID_SHUOSHUO and self.qzone_seen.is_new_candidate(d.tid)
                    for d in first_batch
                )
                # 积压补全穿透直发:续页游标与首页列表同源(统一入口随缓存保存),
                # 后续页仍直调 client 带游标逐页回溯,不经共享层;页间 2s 间隔与
                # 空页/无新 tid/游标耗尽/页数上限四重终止不变
                cursor: str | None = self._qzone_discovery_home_cursor or None
                if has_new and cursor:
                    for _page_idx in range(1, max_pages):
                        # 页间请求间隔:与充实层/通知源B 好友间隔同款 2 秒防风控口径
                        # (长时间离线补全会连发多页,无间隔易触发服务端限流)
                        await asyncio.sleep(2.0)
                        batch, cursor = await self.qzone_client.get_unified_timeline(
                            count=page_size, begintime=cursor
                        )
                        discoveries.extend(batch)
                        has_new = any(
                            d.appid == FEED_APPID_SHUOSHUO and self.qzone_seen.is_new_candidate(d.tid)
                            for d in batch
                        )
                        if not batch or not has_new or not cursor:
                            break  # 空页/本页无新/游标耗尽:翻页止步
            except QzoneAuthError:
                # 登录态失效自愈链:作废 cookie 下轮重取;不回退
                # legacy——cookie 失效对两路径同源,回退只会重复失败多打一轮 API
                self.qzone_cookie.invalidate()
                self.ctx.logger.warning("QQ空间登录态失效(统一时间线),cookie 已作废,下轮重取")
                return
            except QzoneRateLimitError:
                # 翻页穿透路径撞限流(首页已由共享层转换,此分支只接续页):
                # 进入共享退避并终止本轮——绝不能落进下方 legacy 回退,那是
                # 1→N+1 次请求的放大,恰在风控期间火上浇油;终止发生在发现层
                # 阶段(充实未运行,本轮零登记),已发现列表作废,下轮重拉
                self._qzone_enter_rate_limit_backoff()
                self.ctx.logger.warning("QQ空间服务限流(翻页),本轮浏览终止,下轮再试")
                return
            except Exception:
                # 未知失败(超时/HTTP 5xx/响应畸形):告警后跳过本轮,等下轮拉取
                # 间距自然重试。绝不回退 legacy 逐好友路径——风控/服务端故障期
                # 1→N+1 放大恰是火上浇油(2026-09-05 上午实证:未分类 -10001 落
                # 此分支连发逐好友回退,加重风控持续数小时;限流/登录态分支同口径)
                self.ctx.logger.exception("QQ空间统一时间线拉取失败,本轮跳过(不回退逐好友路径)")
                return
            if not discoveries:
                await self._qzone_pump()  # 空发现也泵——超时推进兜底(旧路径每轮必泵语义)
                self._qzone_send_first_poll_finish(win)  # 零新动态轮同样完成了浏览
                return
            # ② 过滤:说说(appid=311)且 seen 未登记——is_new_candidate 纯查不登记
            # (发现≠注入,登记留给充实层 mark_queued,防预占主键判重跳过);排除 bot
            # 自己(自己发的说说不当新动态围观,与源B侧交叉排除同款语义)
            bot_uin = str(self.config.favorability.bot_user_id or "").strip()
            new_items = [
                d for d in discoveries
                if d.appid == 311 and d.uin != bot_uin and self.qzone_seen.is_new_candidate(d.tid)
            ]
            if not new_items:
                await self._qzone_pump()  # 无新动态也泵——超时推进兜底(旧路径每轮必泵语义)
                self._qzone_send_first_poll_finish(win)  # 零新动态轮同样完成了浏览
                return
            # ③ 充实层:按作者分组(保发现顺序),每组 1 次 get_user_feeds 拉完整实体
            by_uin: dict[str, list[FeedDiscovery]] = {}
            for d in new_items:
                by_uin.setdefault(d.uin, []).append(d)
            added_total = 0
            for _author_idx, (uin, group) in enumerate(by_uin.items()):
                if _author_idx:
                    # 好友间请求间隔在循环开头(首作者前不多睡):continue/终止
                    # 路径也覆盖间距,防失败后零间隔连击下一作者加重风控
                    await asyncio.sleep(2.0)
                try:
                    feeds = await self.qzone_client.get_user_feeds(
                        target_uin=uin, nickname=group[0].nickname, num=len(group) + 2
                    )
                except QzoneAuthError:
                    # 登录态失效:立即作废 cookie 缓存(下轮重取),本轮终止(自愈链)
                    self.qzone_cookie.invalidate()
                    self.ctx.logger.warning("QQ空间登录态失效(充实层 uin=%s),cookie 已作废,本轮终止", uin)
                    return
                except QzoneRateLimitError:
                    # 服务端限流:逐人重试只会加重,终止本轮充实,下轮间距后再拉;
                    # 本轮已入队条目留待下轮浏览泵(通知队列不受影响,通知 tick 自泵)
                    self.ctx.logger.warning("QQ空间服务限流(充实层 uin=%s),本轮充实终止,下轮再试", uin)
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
                        f.tid, abstime=f.abstime, author_uin=f.uin, summary=f.content or "",
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
            if added_total:
                self.ctx.logger.info("QQ空间新动态入队 %d 条(统一时间线发现 %d 条)", added_total, len(new_items))
            await self._qzone_pump()
            self._qzone_send_first_poll_finish(win)
        finally:
            self._qzone_poll_running = False

    async def _qzone_poll_feeds_legacy(self) -> None:
        """旧逐好友浏览路径(统一时间线重构前架构):好友列表→每人 get_user_feeds(num=3)。

        已不再被自动回退(2026-09-05:发现层任何失败均跳过本轮,逐好友 1→N+1
        放大是风控帮凶);方法保留供手工调用/调试(窗口守卫已由
        _qzone_poll_feeds 完成)。保留 OneBot 好友列表通道。
        """

        # 拉取架构:好友列表走 adapter OneBot API,
        # 逐好友拉最近说说(msglist_v6 为指定用户接口);好友间固定间隔防风控
        friends = await self._qzone_friend_list()
        if not friends:
            self.ctx.logger.warning("QQ空间好友列表为空或获取失败,本轮跳过")
            return
        added_total = 0
        for _friend_idx, friend in enumerate(friends):
            if _friend_idx:
                # 好友间请求间隔在循环开头(首好友前不多睡):continue/终止路径
                # 也覆盖间距,防失败后零间隔连击下一好友加重风控
                await asyncio.sleep(2.0)
            try:
                feeds = await self.qzone_client.get_user_feeds(
                    target_uin=friend["user_id"], nickname=friend["nickname"], num=3
                )
            except QzoneAuthError:
                # 登录态失效:立即作废 cookie 缓存(下轮重取),本轮终止(自愈链)
                self.qzone_cookie.invalidate()
                self.ctx.logger.warning("QQ空间登录态失效(code=-3000/-10005),cookie 已作废,下轮重取")
                return
            except QzoneRateLimitError:
                # 服务端限流:逐好友重试只会加重,终止本轮回退,已入队部分保留
                self.ctx.logger.warning("QQ空间服务限流(逐好友回退 uin=%s),本轮终止,下轮再试", friend["user_id"])
                break
            except Exception:
                # 单个好友失败不中止整轮(逐人隔离,显式告警)
                self.ctx.logger.exception("QQ空间说说拉取失败(uin=%s),该好友本轮跳过", friend["user_id"])
                continue
            added = [
                f for f in feeds
                if self.qzone_seen.mark_queued(
                    f.tid, abstime=f.abstime, author_uin=f.uin, summary=f.content or "",
                    author_nickname=friend["nickname"],
                )
            ]
            if added:
                self.qzone_injector.enqueue(added)
                added_total += len(added)
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
        """qzone 数据保留期清理:comment_seen/like_seen 30 天+seen 表 7 天。

        qzone_comments/qzone_likes/qzone_feeds 无限增长会拖慢每轮判重与通知
        轮询反查;seen 保留 7 天(recent_seen 只需 summary_days≤3,7 天留余量);
        queued 行不动——回退未读语义由窗口收泵的 revert_pending 负责,prune 不越权。
        """

        now = datetime.now()
        pruned_comments = self.qzone_comment_seen.prune(30, now)
        pruned_likes = self.qzone_like_seen.prune(days=30, now=now)
        try:
            self.store.execute(
                "DELETE FROM qzone_feeds WHERE state = 'seen' AND injected_at < ?",
                ((now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"),),
            )
        except Exception:
            self.ctx.logger.exception("qzone_feeds 清理失败")
            return
        if pruned_comments or pruned_likes:
            self.ctx.logger.info(
                "QQ空间数据清理:评论去重 %d 行,赞去重 %d 行,seen 保留 7 天",
                pruned_comments, pruned_likes,
            )

    async def _qzone_pump(self) -> None:
        """串行注入:超时兜底 → 取队首 → 构造(通知=build_notify_message 带 reply 段
        /浏览=下载图片+build_feed_message)→ route_message → mark_seen。

        泵有两个入口(调度 tick 直接 await 与轮完成信号后台任务),整体持锁串行:
        next_to_inject(弹出)到 mark_injected(置 awaiting)之间有图片下载/route_message
        等 await 点,无锁时重叠入口会同时弹出第二条,破坏「一动态一轮」。
        """

        async with self._qzone_pump_lock:
            # 泵门控不卡浏览窗口:通知(P1)推送语义任何时刻可注入,
            # 窗口外浏览动态由 next_to_inject 的窗口判定拦截
            if not self._qzone_available:
                return
            if self.config.sleep.enabled and self.sleep.is_sleeping():
                return  # 入睡后在途泵静默退出——注入的消息会被睡眠拦截链拦进回顾缓冲(白注入)
            now = time.monotonic()
            if self.qzone_injector.awaiting_timed_out(now):
                self.ctx.logger.warning("QQ空间注入等待轮完成超时(tid=%s),强制推进", self.qzone_injector.awaiting_tid)
                self.qzone_injector.force_release(now)
            feed = self.qzone_injector.next_to_inject(now)
            if feed is None:
                return
            try:
                await self._qzone_inject_one(feed)
            except asyncio.CancelledError:
                # 取消落点在弹出之后(热重载/任务回收):
                # to_thread 等真实挂起点使「弹出→标记」间隙可被取消命中——
                # 在途项回队首,防 P1 通知静默丢失
                self.qzone_injector.requeue_popped()
                self.ctx.logger.warning("QQ空间注入在途被取消,该项已回队首(tid=%s)", feed.tid)
                raise

    async def _qzone_inject_one(self, feed: FeedItem) -> None:
        """单条注入(泵弹出后的主体):构造(通知=带 reply 段/浏览=图片链)→
        route → mark_seen → registry 登记。异常分支内各自处理;取消由泵层
        统一回队。"""
        if feed.source == "notify":
            # 通知走专用构造:带 reply 段引用**原说说**的注入消息
            # (napcat quote 式上下文关联)——引用目标经 seen_store.get_message_id
            # 查原说说注入时记录的 message_id;原说说未注入过(窗口外通知/已被
            # 7 天清理/旧库未记录)时查无 id → reply 段省略,回退纯文本不静默臆造
            reply_target_id = self.qzone_seen.get_message_id(feed.origin_tid)
            if feed.origin_tid and not reply_target_id:
                self.ctx.logger.debug(
                    "QQ空间通知原说说无注入记录(origin_tid=%s),reply 段省略", feed.origin_tid
                )
            msg = build_notify_message(
                feed, group_id=QZONE_VIRTUAL_GROUP_ID,
                group_name=QZONE_VIRTUAL_GROUP_NAME, now_epoch=time.time(),
                reply_target_id=reply_target_id, reply_target_sender=feed.origin_sender,
            )
        else:
            # 图片公共管线:下载(失败跳过+
            # 告警)→多图角标合成→压缩预算,统一走 imaging.run_feed_image_pipeline;
            # 多图恒单图注入(省 VLM token 与上下文,角标空位天然示诚实),全失败
            # 交 build_feed_message 既有 [图片] 占位;[:3] 截断已删除(QQ 上限 9 图封顶)
            try:
                pack = await run_feed_image_pipeline(
                    feed.image_urls,
                    downloader=self.qzone_client.download_image,
                    log=self.ctx.logger, scene="浏览注入",
                )
                self._qzone_seq += 1
                msg = build_feed_message(
                    feed, seq=self._qzone_seq, group_id=QZONE_VIRTUAL_GROUP_ID,
                    group_name=QZONE_VIRTUAL_GROUP_NAME, images=pack.segments,
                    now_epoch=time.time(),
                )
            except Exception:
                self.ctx.logger.exception("QQ空间图片管线/消息构造异常(tid=%s),降级全占位注入", feed.tid)
                # 极端兜底:逐图占位(不送任何图片字节,正文按图给 [图片])
                images = [(url, None) for url in feed.image_urls]
                self._qzone_seq += 1
                msg = build_feed_message(
                    feed, seq=self._qzone_seq, group_id=QZONE_VIRTUAL_GROUP_ID,
                    group_name=QZONE_VIRTUAL_GROUP_NAME, images=images,
                    now_epoch=time.time(),
                )
        try:
            # SDK route_message 返回 bool accepted(不抛异常):宿主 adapter policy
            # 或网关状态拒绝时返回 False,不得当成功继续标记
            accepted = await self.ctx.gateway.route_message(QZONE_GATEWAY_NAME, msg)
        except Exception:
            self.ctx.logger.exception("QQ空间动态注入失败(tid=%s),本轮跳过", feed.tid)
            # 通知项注入未成功不永久丢失,但重试有上限(见
            # _qzone_notify_retry_backoff);浏览动态无此问题(queued 行由窗口
            # 尾 revert_pending 回退未读)
            self._qzone_notify_retry_backoff(feed)
            return
        if not accepted:
            # 拒绝时不 mark_injected/mark_seen:feed 已从内存队列弹出,但 DB 仍
            # queued,窗口尾 revert_pending 会回退未读——不丢数据,下窗口可重试
            self.ctx.logger.warning("QQ空间动态注入被宿主拒绝(tid=%s,adapter policy 或网关状态),跳过且不标记已见", feed.tid)
            # 通知项被拒不永久丢失,重试上限内回退待重检
            self._qzone_notify_retry_backoff(feed)
            return
        self.qzone_injector.mark_injected(feed.tid, time.monotonic())
        # message_id 随 mark_seen 落库:后续通知的 reply 段据此引用本条注入消息
        self.qzone_seen.mark_seen(feed.tid, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), msg["message_id"])
        # 工具驱动架构:登记 FeedContext(替代意图绑定,工具按 tid 解析目标)。
        # 键=真实说说 tid(通知项用 origin_tid——消息尾部锚展示真实 tid,合成 tid
        # 模型不可见);owner=说说主人(浏览=作者;通知源B=好友;源A=bot 自己);
        # commenter/comment_tid/comment_uin=通知场景的评论者与主评论二元组素材;
        # content_summary=说说正文全文;comment_map=评论级锚(浏览=结构化评论,
        # 通知=主评论二元组),qzone_reply 的 comment_id 目标解析来源
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        self._qzone_registry.register(FeedContext(
            tid=feed.origin_tid or feed.tid,
            owner_uin=(feed.friend_uin or bot_uin) if feed.source == "notify" else feed.uin,
            # notify 分支 feed.nickname 实为评论者/点赞者昵称,与 owner_uin(说说
            # 主人)语义错位——原样登记会让 qzone_like 回执张冠李戴,置空串交
            # 消费方回退 owner_uin;registry 字段级合并保留
            # 浏览/detail 登记过的正确主人昵称(新值空不清旧值)
            owner_nickname="" if feed.source == "notify" else feed.nickname,
            commenter_uin=feed.uin if feed.source == "notify" else "",
            commenter_nickname=feed.nickname if feed.source == "notify" else "",
            comment_tid=feed.comment_tid,
            comment_uin=feed.comment_uin,
            kind=feed.source,
            content_summary=(feed.origin_content if feed.source == "notify" else feed.content) or "(无文字)",
            # 评论级锚:浏览=结构化评论全量;通知=主评论二元组(作者 uin,
            # 昵称未知留空,@ 目标优先走 commenter 上下文)
            comment_map=(
                {feed.comment_tid: (feed.comment_uin, "")}
                if feed.source == "notify" and feed.comment_tid
                else {c.comment_tid: (c.uin, c.nickname) for c in feed.comments}
            ),
        ))
        self.ctx.logger.info("QQ空间动态已注入(tid=%s,作者=%s)", feed.tid, feed.nickname)

    def _qzone_notify_retry_backoff(self, feed: FeedItem) -> None:
        """通知项注入失败(被拒/异常)的回退决策。

        回退去重键令下轮通知轮询重新发现,通知不因一次拒绝永久丢失;
        重试有上限——revert 为软回退(qzone_comments 行保留),note_retry
        的累计跨「回退→重发现」循环存活;满 QZONE_NOTIFY_MAX_RETRIES 次仍失败
        则保留登记放弃(is_new 恒 False 跳过),防宿主持续拒绝时同一通知每
        轮询周期(120s)无限重注入。浏览动态(source=feed)不走本路径。
        源C 赞事件例外:去重在 qzone_likes 表(无 pending_retry/
        revert 通道),note_retry/revert 作用于 qzone_comments 为空转——注入被拒
        即显式告警放弃,不误报「待下轮重试」(软回退通道待后续实现)。"""

        if feed.source != "notify" or not feed.dedup_key:
            return
        if feed.tid.startswith("notify_like_"):
            self.ctx.logger.warning(
                "源C 赞事件通知被拒,放弃(源C 无重试通道)(dedup_key=%.40s)", feed.dedup_key
            )
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
        """统一通知轮询触发器:防重入后派发后台扫描,立即返回。

        源A/B 逐好友 2s sleep+HTTP 与泵注入都是长 IO,移入 _qzone_notify_scan
        后台任务执行,不阻塞调度器 60s tick;守卫随迁(单一事实源)。
        """

        if self._qzone_notify_running:
            return  # 上一轮后台扫描还在跑,跳过(防重入)
        self._qzone_notify_running = True
        self._spawn_background_task(self._qzone_notify_scan())

    def _qzone_send_first_poll_finish(self, win: dict) -> None:
        """首轮浏览收尾派发:零新动态轮同样算完成浏览——置位一次性
        标记并派发 browsed=True 发布触发(其内部等泵空闲,时序安全);未武装/
        已触发过的窗口为 no-op;仅 send 窗口不经浏览主路径,不在此派发。"""

        if not self._qzone_send_armed or self._qzone_send_first_poll_done:
            return
        if not win.get("read_qzone"):
            return
        self._qzone_send_first_poll_done = True
        # 同窗形态:等首轮浏览注入完成(泵空闲)再触发,分享有上下文
        self._spawn_background_task(self._qzone_send_trigger(win, browsed=True))

    async def _qzone_send_trigger(self, win: dict, *, browsed: bool) -> None:
        """发布触发:planner 自主决定是否用 qzone_post 发说说(沉默=正常结束)。

        browsed=True(同窗形态)等待首轮浏览注入完成再触发——刚看完好友动态,
        分享有上下文;等待上限 180s,超时也触发(首批已读足够)。
        """

        if browsed:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                st = self.qzone_injector.stats()
                if not st["p1_queued"] and not st["p2_queued"] and not st["awaiting"]:
                    break
                await asyncio.sleep(5)
        activity = str(win.get("activity") or "")
        if browsed:
            intent = (
                "你刚刷完QQ空间,现在有点想分享点什么。"
                "如果确实想发,用 qzone_post 工具直接写你想发的内容;"
                f"不想发就保持沉默,什么都不用做。当前活动:{activity}"
            )
        else:
            intent = (
                f"你在忙{activity},忙里偷闲想上QQ空间发条说说。"
                "如果确实想发,用 qzone_post 工具直接写你想发的内容;"
                "不想发就保持沉默,什么都不用做。"
            )
        stream_id = self._qzone_expected_session_id()
        if not stream_id:
            self.ctx.logger.warning("QQ空间发布触发跳过:虚拟流 session id 不可得")
            return
        try:
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=stream_id, intent=intent, reason="日程窗口send_qzone", priority="",
            )
        except Exception:
            self.ctx.logger.exception("QQ空间发布触发失败(虚拟流)")
            result = {"success": False, "error": "异常"}
        if isinstance(result, dict) and result.get("success"):
            self.ctx.logger.info("QQ空间发布触发已发出(browsed=%s)", browsed)
            return
        err_text = str((result or {}).get("error") or "") if isinstance(result, dict) else ""
        if "未找到" in err_text and "聊天流" in err_text:
            # 冷启动自举:虚拟流会话在首条消息进入后才诞生,proactive.trigger
            # 需要已存在会话——注入无 is_mentioned 的种子消息仅建会话后重试。
            # 种子消息的会话注册是异步的,立即重试会早于会话建立(实机观测:
            # 种子已入库但重试仍报「未找到」),带间隔重试数次
            if await self._qzone_seed_virtual_session():
                for attempt in range(3):
                    await asyncio.sleep(2)
                    try:
                        result2 = await self.ctx.maisaka.proactive.trigger(
                            stream_id=stream_id, intent=intent,
                            reason="日程窗口send_qzone(种子自举重试)", priority="",
                        )
                        if isinstance(result2, dict) and result2.get("success"):
                            self.ctx.logger.info(
                                "QQ空间发布触发已发出(种子自举第 %d 次重试,browsed=%s)", attempt + 1, browsed
                            )
                            return
                    except Exception:
                        self.ctx.logger.exception("QQ空间发布触发重试失败(种子自举后,第 %d 次)", attempt + 1)
                        break
        self.ctx.logger.warning("QQ空间发布触发未成功(%s),等下个窗口", err_text[:60])

    async def _qzone_seed_virtual_session(self) -> bool:
        """冷启动种子:注入一条无 is_mentioned 的 self 消息,仅让主程序创建
        虚拟流会话(不触发 planner 决策轮),供 proactive.trigger 使用。"""

        # 构造纳入 try:取昵称失败直接抛错不兜底,若留在 try 外会沿日程窗口
        # 触发链上抛——纳入后按注入失败处理
        # (告警+返回 False),本窗口跳过自举重试,异常不外泄。
        try:
            self._qzone_seq += 1
            bot_uin = str(self.config.favorability.bot_user_id or "").strip()
            msg = {
                "message_id": f"qzone_seed_{int(time.time())}_{self._qzone_seq}",
                "platform": QZONE_PLATFORM,
                "timestamp": str(int(time.time())),
                "message_info": {
                    "user_info": {"user_id": bot_uin, "user_nickname": await self._bot_echo_nickname()},
                    "group_info": {
                        "group_id": QZONE_VIRTUAL_GROUP_ID,
                        "group_name": QZONE_VIRTUAL_GROUP_NAME,
                    },
                },
                "raw_message": [{"type": "text", "data": "(打开了QQ空间)"}],
            }
            await self.ctx.gateway.route_message(QZONE_GATEWAY_NAME, msg)
            return True
        except Exception:
            self.ctx.logger.exception("QQ空间种子消息注入失败")
            return False

    async def _qzone_notify_scan(self) -> None:
        """统一通知扫描(原 _qzone_notify_poll_tick 主体,始终运行,醒着即可;替代旧评论轮询)。

        三源检测:源A=自己说说下的新评论(含 bot 评论下的楼中楼回复 list_3,
        同载荷三视图补跑解析);源B=自己在他人说说下的评论收到的
        新楼中楼回复(list_3);源C=有人赞了我的说说(「与我相关」流 scope=1)。
        通知构造为 FeedItem(source="notify")走 P1
        优先级队列(插队于浏览动态之前),泵注入成功后登记 FeedContext 供
        qzone_comment/qzone_reply 解析目标(工具驱动,替代意图路由)。
        通知注入走 build_notify_message:reply 段引用原说说注入消息承载上下文
        (target_message_content=原说说正文前 60 字),正文自然可读并带参数独立
        尾行(可读性优化 2026-09-01)——源A「评论了你的说说:…」换行
        「〔说说ID=xx 评论ID=xx 评论者QQ=xx 评论于(今天HH:MM)〕」、源B
        「回复了你的评论「{bot原评论前20字}」:…」换行同款参数行(action=回复),
        评论内 @{uin,nick} 解析为 @昵称;参数行供模型照抄调用工具(映射由场景
        prompt 解释),不重复引用原文,正文不带发布时间前缀——互动新旧由参数行
        动作时间承载(create_time 缺失则省略该段,不编造时间);源C 正文
        「赞了你的说说「{自己说说摘要前20字}」」换行同款参数行(action=点赞,
        摘要取 seen_store.get_summary——发布回注的 own 说说才有的
        标题素材,未登记则无标题不臆造)。
        """

        try:
            if not self._qzone_available:
                self.ctx.logger.debug("通知轮询跳过:QQ空间模块不可用(自检未过/热重载停用)")
                return
            if not self.config.qzone.comment_poll_enabled:
                self.ctx.logger.debug("通知轮询跳过:comment_poll_enabled 关闭")
                return
            if self.config.sleep.enabled and self.sleep.is_sleeping():
                self.ctx.logger.debug("通知轮询跳过:睡眠中(绝对静默)")
                return
            # awaiting 占用时先驱动泵:超时兜底与下一条
            # P1 注入只在泵里做,而泵的两个常规入口是浏览窗口 tick 与轮完成信号——
            # 窗口结束后自然概率下 planner 可能长期不跑轮,awaiting 会无限卡住,
            # 「awaiting 占用→跳过扫描」随之死锁(实机:20:22 注入后卡 18 分钟,
            # 期间好友新评论全部未被扫描)。通知 tick 属 P1 推送语义,这里驱动泵:
            # 超时则强制推进并注入下一条;未超时才维持「不叠加」。
            if self.qzone_injector.awaiting_feed is not None:
                await self._qzone_pump()
                if self.qzone_injector.awaiting_feed is not None:
                    self.ctx.logger.debug("通知轮询跳过:上一条还在等回复(awaiting 占用,不叠加)")
                    return  # 上一条通知/动态还在等 bot 回复,不叠加(下轮再取)
            bot_uin = str(self.config.favorability.bot_user_id or "").strip()
            if not bot_uin:
                return  # 写路径身份缺失(on_load 自检已停用模块,防御性再判)
            now_epoch = time.time()
            # 新鲜度截断:早于 summary_days 的过旧通知不注入;
            # create_time 为 epoch 秒字符串按数值比较,不可解析不截断(保守注入)
            stale_before = now_epoch - max(self.config.qzone.summary_days, 1) * 86400
            notifications: list[FeedItem] = []
            registered_keys: list[str] = []  # 本轮已登记去重键(入队前异常时回退,防静默丢失)
            enqueued = False  # 入队成功后不再回退(队列已持有,pump 异常不视为丢失)

            # ---- 源A:自己说说下的新评论 ----
            try:
                # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次
                scanned_a, auth_err = await self._qzone_auth_retry(
                    lambda: self.qzone_client.get_own_feed_comments(
                        bot_uin=bot_uin, num=max(self.config.qzone.own_feed_scan_count, 1)),
                    "通知源A",
                )
            except QzoneRateLimitError:
                # 服务端限流:源A本轮空,但不得阻断源B/C——它们各自有隔离的
                # 取数与处置,连带丢弃只会在限流期间持续放大通知缺口
                self.ctx.logger.warning("QQ空间通知源A限流(network busy),本轮源A跳过,源B/C 照常")
                scanned_a = ({}, {}, [])
                auth_err = ""
            except Exception:
                self.ctx.logger.exception("QQ空间通知轮询源A失败,本轮跳过")
                return
            if auth_err:
                return  # 同轮自愈失败已显式告警,下轮(120s)再试
            comments, ctx, own_replies = scanned_a
            for feed_tid, items in comments.items():
                for c in items:
                    if not c.comment_tid:
                        continue  # 空 comment_tid 的畸形条目:跳过(防空 tid 畸形请求)
                    if str(c.uin) == bot_uin:
                        # 自己说说(含楼中楼)由源A 覆盖:顶层评论+bot 评论下的
                        # list_3 回复(下方楼中楼段);friend_uin 记 bot_uin 使源B
                        # 名单反查跳过自己(源B 只管他人说说下的 bot 评论)。
                        # 自己的顶层评论重见即幂等登记,不注入
                        self.qzone_comment_seen.note_bot_comment(
                            feed_tid, bot_uin, c.content, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        )
                        continue
                    # 先 is_new 判新(发现即登记,store 契约)再判旧跳过——已登记则下轮
                    # 判重不再重扫
                    dedup_key = f"{feed_tid}:{c.comment_tid}:{c.uin}"
                    if not self.qzone_comment_seen.is_new(dedup_key):
                        continue
                    registered_keys.append(dedup_key)  # 原子性:入队前异常时回退
                    try:
                        comment_epoch = float(str(c.create_time or "").strip())
                    except ValueError:
                        comment_epoch = 0.0
                    if comment_epoch > 0 and comment_epoch < stale_before:
                        self.ctx.logger.info(
                            "QQ空间评论过旧跳过(create_time=%s,昵称=%s)", c.create_time, c.nickname
                        )
                        continue
                    feed_summary = clip_text(ctx.get(feed_tid) or "(无文字)", 30)
                    notifications.append(FeedItem(
                        tid=f"notify_comment_{feed_tid}_{c.comment_tid}",
                        abstime=c.create_time, uin=str(c.uin), nickname=c.nickname,
                        # 正文=自然可读+参数独立尾行(可读性优化 2026-09-01):评论内
                        # @{uin,nick} 解析为 @昵称;〔〕参数行(说说ID/评论ID/评论者QQ/
                        # 评论于时间)供模型照抄调用 qzone_comment/qzone_reply,与工具参数名
                        # (feed_id/comment_id/at_user_id)的映射由场景 prompt 解释;
                        # 动作时间让 bot 分得清互动新旧(缺失省略不编造)
                        content=(
                            f"评论了你的说说:{parse_qzone_mentions(c.content, bot_uin=bot_uin)}\n"
                            + format_comment_param_line(
                                feed_tid=feed_tid, comment_tid=c.comment_tid, commenter_uin=str(c.uin),
                                action="评论", create_time=str(c.create_time or ""), now_epoch=now_epoch,
                            )
                        ),
                        source="notify", dedup_key=dedup_key,
                        # reply 段关联原说说:origin_* 供泵构造引用段,
                        # 引用内容=原说说正文前 60 字(截断统一在 messages 构造层,
                        # 源A:原说说作者=bot 自己)
                        origin_tid=feed_tid, origin_content=ctx.get(feed_tid) or "(无文字)",
                        origin_sender=bot_uin,
                        # 楼中楼二元组素材(qzone_reply):主评论 tid+主评论作者
                        # (源A=评论好友的评论,作者=好友;源B=bot 的评论,作者=bot)
                        comment_tid=c.comment_tid, comment_uin=str(c.uin),
                    ))
                    try:
                        self.qzone_comment_seen.fav_event(
                            str(c.uin), "COMMENT", f"{c.nickname} 评论了你的说说「{feed_summary}」: {c.content[:40]}"
                        )
                    except Exception:
                        self.ctx.logger.exception("QQ空间评论事件好感度记账失败(仅告警)")
                    if len(notifications) >= 3:  # 单轮上限,防通知风暴
                        break
                if len(notifications) >= 3:
                    break
            # 源A 楼中楼段:好友在自己说说下回复 bot 的评论(list_3,同载荷第三视图)
            # ——「好友评论→bot 楼中楼回复→好友再回复」线程原本永久断链(源B 名单
            # 交叉显式排除 bot 自己,解析层丢弃楼中楼)。边界:只覆盖「回复 bot
            # 评论」的线程(parse_feed_replies 只解析 bot 评论的 list_3);好友回复
            # 另一好友的旁听线程不通知——bot 不插话他人对话
            for r in own_replies:
                if len(notifications) >= 3:
                    break
                if not r.feed_tid or not r.reply_tid:
                    continue  # 空 feed/reply tid 的畸形条目:跳过(防空 tid 畸形请求)
                if str(r.uin) == bot_uin:
                    continue  # bot 自己的楼中楼回复不通知(解析层已滤,此处防重)
                key = f"{r.feed_tid}:{r.parent_comment_tid}:reply:{r.reply_tid}"
                if not self.qzone_comment_seen.is_new(key):
                    continue
                registered_keys.append(key)  # 原子性:入队前异常时回退
                try:
                    reply_epoch = float(str(r.create_time or "").strip())
                except ValueError:
                    reply_epoch = 0.0
                if reply_epoch > 0 and reply_epoch < stale_before:
                    self.ctx.logger.info(
                        "QQ空间楼中楼回复过旧跳过(create_time=%s,昵称=%s)", r.create_time, r.nickname
                    )
                    continue
                # 通知构造形态与源B 一致:正文=楼中楼上下文(bot 在该线程最近的
                # 发言前 20 字——好友楼中楼回复的对象通常是 bot 的回复而非顶层
                # 评论,缺内容回退「你之前的评论」)+参数行;区别在归属——
                # 自己说说下 origin_sender=bot。二元组锚=线程顶层评论(与
                # qzone_reply 实际发送形态一致:楼中楼锚定顶层评论,不区分回复谁)
                bot_ctx = clip_text(
                    parse_qzone_mentions(r.bot_reply_content or r.parent_comment_content, bot_uin=bot_uin), 20
                ) or "你之前的评论"
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
                    source="notify", dedup_key=key,
                    origin_tid=r.feed_tid,
                    origin_content=r.feed_content or ctx.get(r.feed_tid) or "(无文字)",
                    origin_sender=bot_uin,  # 自己说说:原说说作者=bot
                    # 楼中楼二元组素材(qzone_reply):锚=线程顶层评论 id+作者
                    # (bot 自己的顶层评论时即 bot;好友顶层评论的线程内为好友)
                    comment_tid=r.parent_comment_tid,
                    comment_uin=r.parent_comment_uin or bot_uin,
                ))
                try:
                    self.qzone_comment_seen.fav_event(
                        str(r.uin), "COMMENT", f"{r.nickname} 回复了你的评论: {r.content[:40]}"
                    )
                except Exception:
                    self.ctx.logger.exception("QQ空间楼中楼事件好感度记账失败(仅告警)")

            # ---- 源B:自己在他人说说下的评论收到的新回复(list_3) ----
            # 统一时间线重构:搭时间线便车——只对「发现层显示有新活动+bot 评论过该好友」
            # 的说说拉评论,不再逐好友全量轮询(发现层无交集时零源B拉取)
            if len(notifications) < 3:
                # 名单先行:本地反查零 HTTP,名单空则跳过发现层调用省 API
                try:
                    commented_friends = set(self.qzone_comment_seen.bot_commented_friends(days=30))
                except Exception:
                    # 反查失败显式告警后按空处理(源B 仅是增量来源,不阻断源A 已得通知)
                    self.ctx.logger.exception("QQ空间通知轮询源B好友反查失败,本轮跳过源B")
                    commented_friends = set()
                discoveries_b: list[FeedDiscovery] = []
                if commented_friends:
                    try:
                        # 单页不翻页(源B 只需找「有新活动且评论过」的交集,
                        # 页大小取 discovery_count 与浏览流同口径);经发现层
                        # 统一入口与浏览层共用一次请求源(单飞+共享缓存+限流
                        # 退避),限流态源B 静默跳过(浏览侧已告警过,不重复)
                        state_b, discoveries_b = await self._qzone_shared_discovery(
                            max(self.config.qzone.discovery_count, 1)
                        )
                        if state_b == "rate_limited":
                            self.ctx.logger.debug("通知源B发现层限流退避中,本轮源B跳过")
                            discoveries_b = []
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
                    await asyncio.sleep(2.0)  # 好友前固定间隔(防风控,与浏览流同款 2 秒)
                    try:
                        # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次
                        scanned_b, auth_err = await self._qzone_auth_retry(
                            lambda uin=friend_uin: self.qzone_client.get_user_feeds_raw(target_uin=uin, num=10),
                            f"通知源B(好友 {friend_uin})",
                        )
                    except Exception:
                        self.ctx.logger.exception("QQ空间通知轮询源B拉取失败(好友 %s),该好友跳过", friend_uin)
                        continue
                    if auth_err:
                        break  # 同轮自愈失败(已告警):登录态同源失效,终止源B
                    raw = scanned_b
                    for r in parse_feed_replies(raw, bot_uin=bot_uin):
                        if not r.feed_tid:
                            continue  # 空 feed_tid 的 ReplyItem 过滤
                        if not r.reply_tid:
                            continue  # 空 reply_tid 的畸形回复:跳过(防空 tid 畸形请求)
                        key = f"{r.feed_tid}:{r.parent_comment_tid}:reply:{r.reply_tid}"
                        if not self.qzone_comment_seen.is_new(key):
                            continue
                        registered_keys.append(key)  # 原子性:入队前异常时回退
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
                        # 2026-09-01):引用 bot 在该线程最近的发言前 20 字
                        # (好友楼中楼回复的对象通常是 bot 的回复而非顶层评论;
                        # 缺内容回退「你之前的评论」),@{uin,nick} 解析为 @昵称;
                        # 〔〕参数行供模型照抄调用 qzone_reply(说说ID/主评论ID/
                        # 回复者QQ/回复于时间,create_time 缺失省略不编造);
                        # 楼中楼二元组锚=线程顶层评论 id+作者(与 qzone_reply
                        # 实际发送形态一致:楼中楼锚定顶层评论,不区分回复谁)
                        bot_ctx = clip_text(
                            parse_qzone_mentions(r.bot_reply_content or r.parent_comment_content, bot_uin=bot_uin), 20
                        ) or "你之前的评论"
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
                            # 楼中楼二元组素材(qzone_reply):锚=线程顶层评论
                            # (bot 自己的顶层评论时即 bot;好友顶层评论的线程内
                            # 为好友;@ 目标另由 commenter 承载)
                            comment_tid=r.parent_comment_tid,
                            comment_uin=r.parent_comment_uin or bot_uin,
                        ))
                        try:
                            self.qzone_comment_seen.fav_event(
                                str(r.uin), "COMMENT", f"{r.nickname} 回复了你的评论: {r.content[:40]}"
                            )
                        except Exception:
                            self.ctx.logger.exception("QQ空间楼中楼事件好感度记账失败(仅告警)")
                        if len(notifications) >= 3:
                            break

            # ---- 源C:有人赞了我的说说(feeds3_html_more?scope=1) ----
            # 去重走 qzone_likes(键=liker_owner_hash,取消赞再赞不重复
            # 通知);自愈链同源B 纪律——QzoneAuthError 作废 cookie 但不 return,
            # 源A/B 已得通知不能丢,仅本轮源C 按空处理
            if len(notifications) < 3:
                # 同轮自愈:AuthError 作废并重取 cookie 后原地重试一次;
                # 自愈失败按空处理(源A/B 已得通知不丢);非 Auth 异常同样在调用点
                # 独立隔离(源B 同款纪律):告警后按空继续不上抛
                # ——上抛会触发扫描级原子性兜底,回退本轮源A/B 已登记的全部去重键,
                # 通知未入队即中止且每 120 秒重复崩溃(实机:源C 相对时间折算遇
                # 非闰年 2月29日 ValueError 无捕获沿 get_like_events 上抛)
                try:
                    scanned_c, auth_err = await self._qzone_auth_retry(
                        lambda: self.qzone_client.get_like_events(count=30), "通知源C")
                except Exception:
                    # 源C 仅是增量来源:likes 按空、parsed_ok=False(不参与漂移观测)
                    self.ctx.logger.exception("QQ空间通知轮询源C拉取失败,本轮跳过源C")
                    scanned_c = None
                likes = scanned_c or []
                parsed_ok = scanned_c is not None  # 取数成功(自愈失败不参与漂移观测)
                # 解析观测线:常规轮次不打解析条数日志(信息噪音),
                # 仅保留异常信号——解析失败走上方 except 告警;连续 3 轮取数成功但
                # 零事件 → 锚点漂移告警(warn-once,恢复有事件即复位)
                if parsed_ok:
                    if likes:
                        self._qzone_sourcec_empty_rounds = 0
                        self._qzone_sourcec_drift_warned = False
                    else:
                        self._qzone_sourcec_empty_rounds += 1
                        if self._qzone_sourcec_empty_rounds >= 3 and not self._qzone_sourcec_drift_warned:
                            self._qzone_sourcec_drift_warned = True
                            self.ctx.logger.warning(
                                "源C 连续 %d 轮解析 0 条赞事件(模块可用),解析锚点可能漂移,"
                                "请实机核对 feeds3_html_more?scope=1 响应与 LIKE_EVENT_RE 锚点",
                                self._qzone_sourcec_empty_rounds,
                            )
                for ev in likes:
                    # 自己的赞跳过;空 target_tid 的畸形事件跳过(防空 tid 畸形键)
                    if str(ev.liker_uin) == bot_uin or not ev.target_tid:
                        continue
                    if not self.qzone_like_seen.is_new(
                        ev.like_key, liker_uin=str(ev.liker_uin), target_tid=str(ev.target_tid)
                    ):
                        continue
                    try:
                        like_epoch = float(str(ev.create_time or "").strip())
                    except ValueError:
                        like_epoch = 0.0
                    if like_epoch > 0 and like_epoch < stale_before:
                        self.ctx.logger.info(
                            "QQ空间赞事件过旧跳过(create_time=%s,昵称=%s)", ev.create_time, ev.liker_nickname
                        )
                        continue
                    # 「摘要」标题素材:发布回注的 own 说说才有 summary;
                    # 他人点赞的目标是自己说说(scope=1 流语义),未登记则无标题不臆造
                    try:
                        origin_summary = self.qzone_seen.get_summary(str(ev.target_tid))
                    except Exception:
                        self.ctx.logger.exception("QQ空间源C 标题素材读取失败,标题省略")
                        origin_summary = ""
                    time_tag = comment_time_prefix(ev.create_time, now_epoch)
                    # 参数行自拼(源C 字段集与评论不同:无 comment_id/commenter_uin,
                    # 动作「点赞于」;时间标签承 comment_time_prefix 括号形态)
                    param = "〔说说ID=" + str(ev.target_tid)[:12]
                    if time_tag:
                        param += f" 点赞于{time_tag}"
                    param += "〕"
                    title = f"「{clip_text(origin_summary, 20)}」" if origin_summary else ""
                    notifications.append(FeedItem(
                        tid=f"notify_like_{ev.like_key}",
                        abstime=ev.create_time, uin=str(ev.liker_uin),
                        nickname=ev.liker_nickname or str(ev.liker_uin),
                        content=f"赞了你的说说{title}\n{param}",
                        source="notify", dedup_key=ev.like_key,
                        origin_tid=str(ev.target_tid), origin_content=origin_summary,
                        origin_sender=bot_uin,
                    ))
                    try:
                        # 标题缺失的退化文本须拼入 target_tid 前 8 位:同人多条
                        # 不同说说的赞事件文本同为「(我的内容)」,同日去重会让
                        # 第二条真实赞被当重放吞掉;tid 片段保证事件文本可区分
                        # (保持 dedup=True——通知扫描路径必须防「回退→重发现」重放)
                        degraded = f"(我的内容 {str(ev.target_tid)[:8]})"
                        self.qzone_comment_seen.fav_event(
                            str(ev.liker_uin), "LIKE",
                            f"{ev.liker_nickname or ev.liker_uin} 赞了你的说说{title or degraded}",
                        )
                    except Exception:
                        self.ctx.logger.exception("QQ空间赞事件好感度记账失败(仅告警)")
                    if len(notifications) >= 3:
                        break

            if notifications:
                added = self.qzone_injector.enqueue_priority(notifications)
                enqueued = True
                self.ctx.logger.info("QQ空间通知入队 %d 条(源A+B+C,P1 插队)", added)
                # 泵优先取 P1(推送语义,任何时刻可注入);浏览动态仅窗口内注入
                await self._qzone_pump()
        except BaseException:
            # 原子性兜底:发现侧「登记即消费」与队尾
            # 入队非原子——登记后入队前异常/热重载取消,已登记键的通知会永久
            # 静默丢失(is_new 恒 False)。回退本轮已登记且未入队的键,下轮重新
            # 发现(fav_events 同日去重,重发现不重复入库);CancelledError 回退后
            # 原样上抛,不打断取消语义
            if not enqueued and registered_keys:
                self.ctx.logger.exception(
                    "QQ空间通知扫描异常,回退本轮已登记去重键 %d 个(下轮重新发现)", len(registered_keys)
                )
                for k in registered_keys:
                    try:
                        self.qzone_comment_seen.revert(k)
                    except Exception:
                        self.ctx.logger.exception("QQ空间通知去重键回退失败(键前缀=%s)", k[:60])
            raise
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
        """注入块前插 system 之后、历史之前;失败仅记录日志不阻塞。"""

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
                # qzone_* 工具全域默认可用(2026-09-02);其余工具 qzone 流走白名单,
                # 非 qzone 流原样放行(view_friend_feeds 为真实流提供说说ID/图片hash)
                filtered = filter_qzone_tools_for_stream(
                    [d for d in defs if isinstance(d, dict)],
                    is_qzone=is_qzone_session, whitelist=self.config.qzone.tool_whitelist,
                )
                new_kwargs = {**new_kwargs, "tool_definitions": filtered}
            rendered = self.assembler.render(blocks)
            if not rendered:
                self.ctx.logger.debug("注入跳过: render 结果为空(blocks=%d)", len(blocks))
                return {"action": "continue", "modified_kwargs": kwargs}
            # 实测:items 须为合法快照格式,朴素 dict 会被主程序拒绝——render 输出转快照后插入
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
            return  # 睡眠期消息不得计数(绝对静默;睡眠模块关闭时计数不暂停)
        if not self.config.plugin.enabled or not self.config.favorability.enabled:
            return
        msg = kwargs.get("message")
        if not isinstance(msg, dict):
            return
        # 通知类消息(戳一戳等)不参与好感度计数
        if msg.get("is_notify"):
            return
        # QQ空间虚拟流豁免:虚拟流注入消息不计好感度(好友发说说≠与 bot 互动;
        # 空间互动走显式事件路径);顺带收集虚拟流 session 供晚安/白名单豁免
        if is_qzone_message(msg):
            sid = str(msg.get("session_id") or "")
            if sid:
                self._qzone_session_ids.add(sid)
            return
        # 实测:user/stream 在 message 内(user_info 与 session_id;user_id 字段名以实机联调为准)
        msg_info = msg.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        user_id = str(user_info.get("user_id") or user_info.get("sender_id") or "")
        stream_id = str(msg.get("session_id") or "")
        if not user_id or not stream_id:
            return
        # 取数点:记录流→最近真实说话人(群聊 memo_write 工具 user_id 空时兜底;
        # 纯内存映射,重启丢失可接受——_resolve_speaker 的 get_recent 回退仍在)
        self._last_speaker_map[stream_id] = user_id
        # 计数与触发判定按人:先 bump 活跃账本,再判定(check_trigger 不再内部计数)
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
        """晚安短句判定(可入睡时间内):SLEEP → 入睡并触发生成醒来日日程。"""

        if not self.config.plugin.enabled or not self.config.sleep.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        self._last_activity_ts = datetime.now().timestamp()  # 任何出站回复都算活动(含空间评论,与既有路径语义一致)
        # QQ空间虚拟流豁免:虚拟流的评论文本不进晚安判定(防深夜短评论触发全局入睡)
        # 注意:活动计时刷新在豁免之前——空间活动同样延迟静默入睡
        if str(kwargs.get("session_id") or "") in self._qzone_session_id_set():
            return {"action": "continue", "modified_kwargs": kwargs}
        if self.sleep.is_sleeping():
            return {"action": "continue", "modified_kwargs": kwargs}
        # 晚安判定仅在睡眠窗口内有效(可入睡时间),与静默开关无关
        _win = current_window(self._schedule_data, datetime.now().strftime("%Y-%m-%dT%H:%M"))
        if not _win or _win.get("kind") != "sleep":
            return {"action": "continue", "modified_kwargs": kwargs}
        text = str(kwargs.get("response") or "")
        if not is_goodnight_utterance(text):
            return {"action": "continue", "modified_kwargs": kwargs}
        # 短句为变量尾(stable_ctx 纪律);判定器为轻量任务,固定 memory
        messages, _ = build_side_prompt(
            "sleep_confirm", [], [f"待判定晚安短句:{text}"]
        )
        try:
            result = await self._side_llm_call(messages, "memory", "sleep_confirm")
        except Exception as exc:  # noqa: BLE001
            # 失败显式日志并跳过本轮(与哨兵层同款纪律);仅记异常类型防 PII
            self.ctx.logger.warning("晚安判定 LLM 调用异常(%s),本轮不入睡", rpc_error_brief(exc))
            return {"action": "continue", "modified_kwargs": kwargs}
        if not isinstance(result, dict) or not result.get("success"):
            # 不落响应原文,仅记失败形态
            detail = f"success={result.get('success')}" if isinstance(result, dict) else f"结果类型={type(result).__name__}"
            self.ctx.logger.warning("晚安判定 LLM 失败,本轮不入睡:%s", detail)
            return {"action": "continue", "modified_kwargs": kwargs}
        verdict, _ = parse_sleep_confirm_response(str(result.get("response") or ""))
        if verdict == "SLEEP":
            await self._enter_sleep()
        return {"action": "continue", "modified_kwargs": kwargs}

    async def _enter_sleep(self) -> None:
        """入睡:计算 clamp 醒来时刻,状态落盘,触发入睡任务(醒来日日程生成+日记生成)。"""

        if self.sleep.is_sleeping():
            return  # 已睡幂等:晚安判定 await 交错期间不得二次入睡/二次生成
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
        self._sleep_window_settled = str(sleep_win.get("end") or "") if sleep_win else ""  # 入睡已执行入睡任务,窗口终点不再补执行
        self.ctx.logger.info("已入睡:醒来 %s", wake_at)
        # 日程目标日 = 醒来日(wake_at 前 10 位):午夜后入睡时 now+1 会错成
        # 醒来日的次日(见 _generate_tomorrow_schedule 说明)
        self._spawn_background_task(self._generate_tomorrow_schedule(wake_at[:10]))
        # 日记与日程同属入睡任务:旁路 LLM 与发布 API 均不经消息链,睡眠期可执行;
        # 日记素材日 = 睡眠窗口起始日(午夜后入睡时,主观上的「今天」仍是入睡日)
        sleep_day = str(sleep_win.get("start") or "")[:10] if sleep_win else ""
        self._spawn_background_task(self._generate_and_publish_diary(sleep_day=sleep_day or None))

    # ---------- Hook:内容护栏(replyer 拦截,v1.0.0) ----------

    @HookHandler("maisaka.replyer.after_response", name="catsitate_content_guard", mode=HookMode.BLOCKING, order=HookOrder.EARLY)
    async def content_guard_replyer(self, **kwargs: Any) -> dict[str, Any]:
        """v1.0.0 内容护栏 replyer 拦截(EARLY,先于哨兵 LATE):response 投影命中
        护栏正则 → 改写为空串——主程序 reply 工具拿空文本走 build_failure_result,
        planner 看到 [失败] 即 bot 真沉默(调查实证:不自纠不重试;模型自主重调
        reply 时每次生成再过本钩子,拦截可重复生效,不漏发)。全部会话生效
        (内容级护栏,不区分流)。"""

        if not self.config.plugin.enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        # 护栏关→_guard_compiled 空列表,match_guard 恒 0,原样 continue(零行为变化)
        response_text = str(kwargs.get("response") or "")
        hit = match_guard(self._guard_compiled, response_text)
        if not hit:
            return {"action": "continue", "modified_kwargs": kwargs}
        self.ctx.logger.warning(
            "内容护栏拦截:回复 命中规则%d,置空未发送(文本:%s...)", hit, response_text[:60]
        )
        # response 改空;output_items 原样——主程序 replace_output_projection 自行
        # 处理正文投影,勿手工改 items 以免形态错误
        return {"action": "continue", "modified_kwargs": {**kwargs, "response": "", "output_items": kwargs.get("output_items")}}

    # ---------- Hook:reply 补传与哨兵 ----------

    @HookHandler("maisaka.planner.after_response", name="catsitate_reply_backfill", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def reply_backfill(self, **kwargs: Any) -> dict[str, Any]:
        """规则层补传:三条件触发,零成本,不改动其它工具调用。"""

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
        # 日志侧同样按宿主快照形态取工具名(func_name 在 tool_call 内)
        self.ctx.logger.info(
            "reply 补传:%s",
            [
                (t.get("tool_call") or {}).get("func_name")
                for t in new_items
                if t.get("item_type") == "FunctionCallItem"
                and (t.get("tool_call") or {}).get("func_name") == "reply"
            ],
        )
        return {"action": "continue", "modified_kwargs": new_kwargs}

    @HookHandler("maisaka.replyer.after_response", name="catsitate_sentinel", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def sentinel_check(self, **kwargs: Any) -> dict[str, Any]:
        """LLM 哨兵层(默认关);撤回能力以实机验证结论为准,不能则仅日志。"""

        cfg = self.config.reply_guard
        if not self.config.plugin.enabled or not cfg.enabled or not cfg.sentinel_enabled:
            return {"action": "continue", "modified_kwargs": kwargs}
        reply_text = str(kwargs.get("response") or "")
        if not reply_text.strip():
            return {"action": "continue", "modified_kwargs": kwargs}
        persona = await self._persona()
        chat_context = await self._recent_context_text(str(kwargs.get("session_id") or ""), limit=10)
        messages, _ = build_sentinel_prompt(persona, reply_text, chat_context)
        try:
            result = await self._side_llm_call(messages, cfg.sentinel_model, "sentinel", cfg.sentinel_timeout_ms)
        except Exception as exc:  # noqa: BLE001
            # BLOCKING 钩子裸抛交由宿主未知处置;失败显式日志后放行(与晚安判定同款纪律,仅记异常简报防 PII)
            self.ctx.logger.warning("哨兵层 LLM 调用异常(%s),放行回复", rpc_error_brief(exc))
            return {"action": "continue", "modified_kwargs": kwargs}
        if not isinstance(result, dict):
            # 返回形态异常:非 dict 时下方 result.get 会崩,显式告警后放行
            self.ctx.logger.warning("哨兵层 LLM 返回非对象结果(类型=%s),放行回复", type(result).__name__)
            return {"action": "continue", "modified_kwargs": kwargs}
        if not result.get("success"):
            self.ctx.logger.warning("哨兵层 LLM 调用失败,放行回复:%s", result.get("response", "")[:200])
            return {"action": "continue", "modified_kwargs": kwargs}
        should_send, reason = parse_sentinel_response(str(result.get("response") or ""))
        if should_send is None or should_send:
            self.ctx.logger.info("哨兵判定:放行回复")
            return {"action": "continue", "modified_kwargs": kwargs}
        self.ctx.logger.warning("哨兵判定:撤回回复:%s", reason)
        # 撤回动作(待撤回能力验证后实现:删除待发送项或调用撤回 API);当前先日志
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler("maisaka.replyer.before_model_request", name="catsitate_qzone_replyer_scene", mode=HookMode.BLOCKING, order=HookOrder.LATE)
    async def qzone_replyer_scene(self, **kwargs: Any) -> dict[str, Any]:
        """replyer 侧场景替换:before_request 不带 items,必须挂 before_model_request。"""

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
        """轮完成信号:无 tool_calls 的 planner 响应=本轮不再有出站,释放注入泵。"""

        if not self._qzone_available or str(kwargs.get("session_id") or "") not in self._qzone_session_id_set():
            return
        called = self._called_tools(kwargs)
        if called:
            if "wait" in called:
                self.qzone_injector.on_wait_state(time.monotonic())
            return
        if self.config.sleep.enabled and self.sleep.is_sleeping():
            return  # 睡眠期不推进泵——在途消息已被拦,推进会注入下一条进拦截链
        self.qzone_injector.on_turn_complete(time.monotonic())
        self._spawn_background_task(self._qzone_pump())

    # ---------- 内部辅助 ----------

    # 实测确认的字段名集中于此,不符仅改此处
    _MESSAGES_KEY = "items"  # 主程序 planner.before_request payload 键(快照格式)
    _OUTPUT_ITEMS_KEY = "output_items"

    def _messages_from_kwargs(self, kwargs: dict[str, Any]) -> list[dict] | None:
        return kwargs.get(self._MESSAGES_KEY)

    def _system_tail_index(self, messages: list[dict]) -> int:
        """注入点 = system 消息之后(快照 item 无 role 字段,按 item_type 定位)。"""

        for i, m in enumerate(messages):
            if m.get("item_type") == "SystemMessageItem" or m.get("role") == "system":
                return i + 1
        self.ctx.logger.warning("注入定位失败:items 中无 SystemMessageItem,已回退追加尾部(缓存纪律受损)")
        return len(messages)  # 无 system 时追加尾部(回退语义)

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
            # 不会附栈回溯,只有一行类型名
            self.ctx.logger.exception("后台任务异常:%s", exc, exc_info=exc)

    def _to_snapshot_item(self, text: str) -> dict:
        """渲染块 → 合法快照 UserMessageItem(朴素 dict 被主程序拒绝)。

        同文本返回同一对象(前缀缓存纪律,缓存键=文本);item_id 用 sha256 前缀,
        勿用内置 hash()(进程内哈希随机化);timestamp 随对象绑定(创建时确定)。
        """

        cached = self._snapshot_cache.get(text)
        if cached is not None:
            self._snapshot_cache.move_to_end(text)  # 命中刷新新近度(LRU)
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
            self._snapshot_cache.popitem(last=False)  # 逐最旧(防文本键无界增长)
        return item

    async def _build_inject_blocks(self, kwargs: dict[str, Any]) -> list[InjectionBlock]:
        cfg = self.config
        # planner.before_request payload 无 user_id/stream_id 键(实机确认):
        # 流 = session_id;说话人 = 私聊流对端 / 群聊最近非 bot 消息发送者
        stream_id = str(kwargs.get("session_id") or "")
        # 虚拟流说话人 = 注入泵当前动态作者——注入 timestamp=阅读时刻,
        # 宿主 24h 窗天然可靠;说话人仍取注入泵当前作者(交叉校验)
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
                # 集成表:read_qzone 窗口=正在浏览空间,行末明文追加状态
                # (planner 据此知道此刻的「刷手机」具体是在刷QQ空间)
                if win.get("read_qzone"):
                    line += "(正在刷QQ空间)"
                blocks.append(InjectionBlock("schedule", f"sch:{win.get('start')}|{'fired' if fired else ''}", line))
        if cfg.qzone.enabled and self._qzone_available:
            qz = self._qzone_block(stream_id)
            if qz:
                blocks.append(InjectionBlock("qzone", qz[0], qz[1]))
        if cfg.inject.memo_enabled and cfg.memo.enabled:
            # read 为 OR 语义(流 ∪ 主QQ ∪ 附带QQ),一次查询即含原「流+说话人」两维度;
            # 单查询无重复,按 id 去重保留为防御;取数与截断均按 inject_max
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
                        # 5 级规则全量注入改为按等级单条注入(等级规则块并入好感度块)
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
        """qzone 注入块:虚拟流=当前浏览动态状态;真实聊天=近期见闻摘要。

        注入块去重(可读性优化 2026-09-01):场景全文已由 apply_scene_surgery
        原位替换进 system 段,虚拟流分支只保留动态状态——不再拼场景常量,
        免同轮双份场景说明互相漂移。
        """

        if stream_id in self._qzone_session_id_set():
            state = self.qzone_injector.describe_current()
            return f"qzone:v:{state}", f"[空间] {state}"
        # 当日空间见闻优先:窗口结束旁路 LLM 摘要的空间印象,比逐条
        # 「近期刷到」更像人转述见过的事;无当日见闻回退下方既有路径
        digest = self._qzone_digest_snapshot.load()
        today = datetime.now().strftime("%Y-%m-%d")
        if digest.get("text") and digest.get("date") == today:
            return f"qzone:d:{today}:{len(digest['text'])}", f"[空间见闻] {digest['text']}"
        entries = self.qzone_seen.recent_seen(
            limit=self.config.qzone.summary_count, days=self.config.qzone.summary_days, now=datetime.now()
        )
        if not entries:
            return None
        # 叙事格式(与浏览动态的自然文本一致):「昵称发了「摘要」」比键值对
        # 「昵称:摘要」更像转述见闻;摘要截 100 字(原 20),
        # 纯图说说以「图片」占位
        lines = [
            f"{e['author_nickname'] or e['author_uin']}发了「{clip_text(e['summary'] or '图片', 100)}」"
            for e in entries
        ]
        text = "[空间] 近期刷到: " + ";".join(lines)
        key = "qzone:s:" + "|".join(e["tid"] for e in entries)
        return key, text

    async def _qzone_generate_digest(self) -> None:
        """read_qzone 窗口结束:把近 24h 滚动窗内的浏览与互动摘要为见闻。

        主程序会话摘要由 bot 发言后的回写服务生成,虚拟流 receive-only 无
        发言投递,主程序记忆层不会为虚拟流产出内容,插件亦无 API 读取记忆
        段落——故由插件在窗口边界自行摘要(素材→摘要→存储→注入,与主程序
        记忆摘要方法一致)。失败告警并保留上一份。
        """

        if not (self.config.qzone.enabled and self.config.qzone.digest_enabled):
            return
        if not self._qzone_available:
            return
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        seen = self.qzone_seen.recent_seen(limit=15, days=1, now=now)
        lines = [
            f"{e['author_nickname'] or e['author_uin']}发了「{clip_text(e['summary'] or '图片', 20)}」"
            for e in seen
        ]
        try:
            # 素材锚点统一为近 24h 滚动窗(浏览 recent_seen days=1 与互动 fav_events_window
            # 同窗),跨零点会话昨晚素材自然衔接,不按自然日切割
            events = self.qzone_comment_seen.fav_events_window(
                (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
            )
        except Exception:
            self.ctx.logger.exception("QQ空间见闻素材(互动事件)读取失败,本轮按空处理")
            events = []
        # 截断统一保留最新(升序取尾,与结算路径 events[-5:] 同款)
        lines += [clip_text(e["text"], 40) for e in events[-10:]]
        if not lines:
            return  # 窗内无素材:不生成,保留旧见闻
        persona, _ = await self._persona_context()
        stable_ctx = [f"bot 人设:{persona}", f"日期:{day}"]
        messages, _ = build_side_prompt("qzone_digest", stable_ctx, ["素材:\n" + "\n".join(lines)])
        try:
            result = await self._side_llm_call(
                messages, self.config.qzone.digest_llm_model, "qzone_digest",
                self.config.qzone.digest_llm_timeout_ms,
            )
        except Exception:
            self.ctx.logger.exception("QQ空间见闻生成失败,保留上一份")
            return
        if not isinstance(result, dict) or not result.get("success"):
            self.ctx.logger.warning("QQ空间见闻 LLM 失败,保留上一份")
            return
        text = str(result.get("response") or "").strip()
        if not text or len(text) > 400:
            self.ctx.logger.warning("QQ空间见闻文本异常(长度 %d),保留上一份", len(text))
            return
        self._qzone_digest_snapshot.save({"date": day, "text": text})
        self.ctx.logger.info("QQ空间见闻已生成(%d 字)", len(text))

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
        parts.append(QZONE_VIRTUAL_GROUP_ID)
        return hashlib.md5("_".join(parts).encode()).hexdigest()

    def _environment_block(self, stream_id: str) -> tuple[str, str] | None:
        """环境块:节日+天气;缓存 45 分钟。"""

        del stream_id
        cfg = self.config.time_aware
        if self._env_fetched_at and (datetime.now() - self._env_fetched_at).total_seconds() < cfg.weather_refresh_minutes * 60:
            cached = self._env_cache.get("env")
            return ("env", cached) if cached else None
        return None  # 数据未就绪时跳过(首次由后台任务填充后自动出现)

    async def _refresh_environment_tick(self) -> None:
        """环境刷新调度入口:tick 内防重入标记+后台派发。

        节日(双源各 15s 超时)与天气拉取是外网 HTTP 长 IO,tick 内直接 await 会
        拖住同 tick 串行的全部任务(与 qzone_poll 模式同源问题)。"""

        if self._env_refresh_running:
            self.ctx.logger.info("环境刷新后台仍在进行,跳过本 tick 派发(防重入)")
            return
        self._env_refresh_running = True
        self._spawn_background_task(self._refresh_environment_bg())

    async def _refresh_environment_bg(self) -> None:
        """环境刷新后台壳:防重入标记在 finally 复位(异常/取消一致,防永久卡死)。"""

        try:
            await self._refresh_environment()
        finally:
            self._env_refresh_running = False

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
        # 天气快照落库供日程生成联动
        if weather is not None:
            self.store.execute(
                """
                INSERT INTO weather_snapshot (id, city, fetched_at, data) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET city = excluded.city, fetched_at = excluded.fetched_at, data = excluded.data
                """,
                (cfg.city, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), json.dumps(weather, ensure_ascii=False)),
            )
        # 当天 + 临近 3 天节日/节气(注入示例)
        # 公历节日走回退链结果;农历节日经 lunar-python 实算;双源重名按名去重
        today_holidays = dedup_festival_names(holidays.get(today.strftime("%m-%d"), []) + lunar_festivals_near(today, days=0))
        today_terms = solar_terms_near(today, days=0)
        # 临近节日/节气单独一段(带日期),不与「今天」混淆(联调 bug:3 天窗口混入今日)
        upcoming: list[str] = []
        for offset in range(1, 4):
            day = today + timedelta(days=offset)
            upcoming.extend(f"{day.month}月{day.day}日 {n}" for n in holidays.get(day.strftime("%m-%d"), []))
        upcoming += [n for n in lunar_festivals_upcoming(today, days=3) if "月" in n]
        # 临近节气带日期构造:solar_terms_near 返回裸节气名,曾按「月」过滤导致
        # 临近节气恒被整段丢弃——改为逐日实算(当天不进 upcoming,today_terms 已含)
        for offset in range(1, 4):
            day = today + timedelta(days=offset)
            term = solar_term_on(day)
            if term:
                upcoming.append(f"{day.month}月{day.day}日 {term}")
        upcoming = dedup_festival_names(upcoming)
        text = build_environment_text(today, cfg.city, weather, today_holidays, today_terms, upcoming=upcoming)
        self._env_cache["env"] = text
        self._env_fetched_at = datetime.now()

    async def _cleanup_memos(self) -> None:
        removed = self.memo.cleanup()
        if removed:
            self.ctx.logger.info("备忘清理:%s 条过期", removed)

    def _dispatch_daily_settle(self) -> None:
        """日终结算统一后台派发:醒后补跑与调度 tick
        共享防重入守卫——在飞则跳过本轮(间隔小时级,跳过后下个周期自然重试)。"""

        if self._daily_settle_running:
            self.ctx.logger.info("日终结算后台仍在进行,跳过本轮派发(防重入)")
            return
        self._daily_settle_running = True
        self._spawn_background_task(self._daily_settle_bg())

    async def _daily_settle_tick(self) -> None:
        """日终结算调度入口:tick 内只做防重入派发,长 IO 全在后台任务。

        结算含逐流取消息(RPC)与逐人 LLM 判定的分钟级 IO,tick 内直接 await
        会拖住同 tick 串行的 sleep_tick/schedule_tick 等全部任务。"""

        self._dispatch_daily_settle()

    async def _daily_settle_bg(self) -> None:
        """日终结算后台壳:防重入标记在 finally 复位(异常/取消一致,防永久卡死)。"""

        try:
            await self._daily_settle()
        finally:
            self._daily_settle_running = False

    async def _daily_settle(self) -> None:
        """日终兜底:对当日有消息且未日终结算的用户结算当前批次(不计提前上限)。"""

        if self.sleep.is_sleeping():
            return  # 睡眠期调度静默,醒来补跑
        await self._daily_decay()  # 先衰减后结算(同一 tick 调用顺序)
        if not self.config.plugin.enabled or not self.config.favorability.enabled:
            return
        # 按人语义:多流用户日终只结一次,结算聚合该人全部流素材(规格全局决策)
        candidates = set(self.fav_engine.iter_today_active())
        # 纯空间互动好友并集——只有空间事件(评论/点赞/出站)而无
        # 当日聊天的人没有 batch 行,iter_today_active 只扫 batch_counter 扫不到,
        # 原实现下纯空间互动者永不结算;并集回看窗内 qzone_fav_events 的 user_id
        # 令其进入日终兜底(空间事件本身即结算素材)。bot 自身排除:源A自评回复
        # 的 OUT_COMMENT 以 bot 为 target 落事件,但 bot 不是好感度结算对象。
        # 并集改 window_hours 回看窗(fav_events_window,与结算
        # 窗口同口径)——旧自然日(day=今天)在跨零点结算时漏掉昨晚互动的纯空间
        # 好友(其事件 day=昨日),同样不进日终兜底。
        since = (
            datetime.now() - timedelta(hours=max(self.config.favorability.window_hours, 1))
        ).strftime("%Y-%m-%dT%H:%M:%S")
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        try:
            event_users = {
                str(e["user_id"]) for e in self.qzone_comment_seen.fav_events_window(since)
            }
        except Exception:
            self.ctx.logger.exception("回看窗空间事件反查失败,日终候选仅回退当日活跃流")
            event_users = set()
        candidates |= {u for u in event_users if u and u != bot_uin}
        for user_id in sorted(candidates):
            if self.fav_engine.has_daily_settle_today(user_id):
                continue
            try:
                await self._settle_and_log(user_id, kind="daily")
            except Exception:
                # 单用户结算失败隔离,不拖垮整轮(与衰减逐流隔离对齐)
                self.ctx.logger.exception("日终结算失败(user=%s),跳过该用户", user_id)
                continue

    async def _sleep_tick(self) -> None:
        if not self.config.plugin.enabled or not self.config.sleep.enabled:
            return
        now = datetime.now()
        now_iso = now.strftime("%Y-%m-%dT%H:%M")
        # 注意:不能经 is_sleeping() 判断睡眠中——其语义含 now < wake_at,
        # wake_at 过后直接返回 False 落入 else 分支,自然醒分支会成死代码
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
            # 睡眠窗口已过而未入睡:不入睡,但补执行入睡时会做的任务(醒来日日程生成)
            await self._maybe_settle_passed_sleep_window(now)
            return
        # 可入睡时间(睡眠窗口语义):
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
        """睡眠窗口已过而未入睡:不入睡,但执行入睡时会做的任务(醒来日日程生成+日记生成);每窗口一次。

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
        # 目标日 = 窗口 end 所在自然日(将要醒来的日),日记素材日 = 窗口起始日
        # ——与 _enter_sleep 同源语义,午夜后补执行不再错日
        self._spawn_background_task(self._generate_tomorrow_schedule(end[:10]))
        sleep_day = str(sleep_win.get("start") or "")[:10]
        self._spawn_background_task(self._generate_and_publish_diary(sleep_day=sleep_day or None))

    async def _wake_up(self) -> None:
        # 先取入睡时刻再唤醒:wake() 会清空睡眠状态,回顾的备忘附列需要它过滤
        sleep_at = str(self.sleep.state.sleep_at or "")
        self.sleep.wake()
        if self.config.sleep.review_enabled:
            self._spawn_background_task(self._write_sleep_review(sleep_at))
        else:
            # 回顾未启用也要清缓冲:唯一清理点原在 _write_sleep_review 开头,关闭
            # 回顾时无人派发它 → 缓冲跨夜无界增长且睡眠期每条消息全量重写 JSON
            self._sleep_review_buffer = []
            self._sleep_review_buffer_snapshot.save({"messages": []})
            self.ctx.logger.info("睡醒回顾未启用,回顾缓冲已清空")
        # 醒来补跑当日结算(内部已先衰减后结算;勿再单独 spawn 衰减,防并发双计);
        # 经 _dispatch_daily_settle 与调度 tick 共享防重入守卫(在飞则跳过)
        self._dispatch_daily_settle()

    async def _daily_decay_tick(self) -> None:
        """衰减调度入口:tick 内防重入标记+后台派发。

        衰减含逐流取消息与逐人 LLM 判定的分钟级 IO,tick 内直接 await 会拖住
        同 tick 串行的全部任务。与日终结算内部的先衰减后结算为顺序调用,
        两路并发由 _daily_decay 内的 _decaying 标记拦截(防 delta 双计)。"""

        if self._daily_decay_running:
            self.ctx.logger.info("衰减后台仍在进行,跳过本 tick 派发(防重入)")
            return
        self._daily_decay_running = True
        self._spawn_background_task(self._daily_decay_bg())

    async def _daily_decay_bg(self) -> None:
        """衰减后台壳:防重入标记在 finally 复位(异常/取消一致,防永久卡死)。"""

        try:
            await self._daily_decay()
        finally:
            self._daily_decay_running = False

    async def _daily_decay(self) -> None:
        """自然衰减(按人跨流):先衰减后结算(与 _daily_settle 同 tick 调用顺序)。

        计时基准(规格「判定后重置计时」):基准 = max(各活跃流内最近 bot 直接互动时间,
        最近一次 decay 判定时间)——衰减判定本身即一次「想起」,7 天内不重复衰减。
        群聊互动 = @ 或 quote(reply 段经 message.get_by_id 解析原发送者);
        quote 解析在本函数内预解析后注入 resolved_quote_user_id(decay.py 保持纯函数)。
        """

        if self.sleep.is_sleeping():
            return
        if not self.config.plugin.enabled or not self.config.favorability.decay_enabled:
            return
        # 并发防护:醒后 _wake_up spawn 的 _daily_settle 与调度 tick
        # 的 daily_decay/daily_settle 可并发(后台任务不与调度器串行),衰减会跑两次,
        # 而 judge_id 幂等键只同秒去重——delta 双计。实例级在飞标记:在飞则跳过本轮。
        if self._decaying:
            self.ctx.logger.info("衰减本轮已在进行,跳过并发触发(防 delta 双计)")
            return
        self._decaying = True
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
                    # quote 语义:群聊 bot 消息 reply 段为纯消息 id,
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
                # 空间互动事件参与衰减计时基准(窗口外评论是真实双向互动)。
                # 事件时间取自事件表全局、不依赖流存在——流消亡(上文 continue)只影响
                # 消息素材基准不影响事件基准;读取失败告警后按空串(不参与 max)
                event_ts = ""
                try:
                    event_ts = self.qzone_comment_seen.last_fav_interaction(user_id)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("衰减取空间互动事件失败,该人事件基准按空处理(user=%s):%s", user_id, rpc_error_brief(exc))
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
        finally:
            self._decaying = False  # 在飞标记复位(异常/正常退出一致,防永久卡死跳过)

    def _stream_is_group(self, stream_id: str) -> bool:
        info = self._stream_cache.get(stream_id) or {}
        return str(info.get("is_group_session") or "").lower().startswith(("true", "1"))

    async def _write_sleep_review(self, sleep_at: str = "") -> None:
        """睡醒回顾:拦截缓冲按流聚合,LLM 摘要,写单份聚合报告文件。

        sleep_at(入睡时刻)由 _wake_up 传入(wake() 先清空睡眠状态,此处读
        不到);为空时兜底读状态机(直接调用场景),仍取不到则备忘附列按并集
        全列(见下),宁可多列不可漏列。"""

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
            preview = " | ".join(f"{m['nickname'] or m['user_id']}:{clip_text(m['text'], 50)}" for m in msgs[:20])
            messages, _ = build_side_prompt(
                "sleep_review", [], [f"睡眠期间 {stream_id} 的消息(共 {len(msgs)} 条):\n{preview}"]
            )
            try:
                result = await self._side_llm_call(messages, self.config.sleep.review_llm_model, "sleep_review", self.config.sleep.review_llm_timeout_ms)
                summary = clip_text(str(result.get("response") or ""), 200) if isinstance(result, dict) else ""

            except Exception:
                self.ctx.logger.exception("回顾摘要失败(流 %s)", stream_id)
                summary = ""
            sections.append(f"## 流 {stream_id}({len(msgs)} 条)\n{summary or '摘要生成失败'}")
        # 睡眠期到期的备忘提醒静态附列(不占 LLM 额度,备忘不丢失原则)。
        # 跨午夜:睡眠窗口 23:00 起、remind_at 落在入睡日 23:00~24:00 段的备忘
        # 在入睡日单查(旧逻辑从不查)与醒来日「今天」单查(日期已换)两边都
        # 查不到,静默丢失——按「入睡日 ∪ 今天」并集取,再过滤
        # [入睡时刻, 现在](ISO 字符串比较),两边都不漏
        if not sleep_at:
            state = getattr(getattr(self, "sleep", None), "state", None)
            sleep_at = str(getattr(state, "sleep_at", "") or "")
        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        due_days = sorted({now_iso[:10], sleep_at[:10]} - {""})
        merged: list[dict] = []
        for day in due_days:
            merged.extend(self.memo.due_on(day))
        if sleep_at:
            sleep_day_due = [e for e in merged if sleep_at <= e["remind_at"] <= now_iso]
        else:
            # 取不到入睡时刻(状态缺失):并集全列附上并 debug 记录,宁可多列不可漏列
            self.ctx.logger.debug("睡醒回顾取不到入睡时刻,睡眠期到期备忘按并集全列附列")
            sleep_day_due = merged
        if sleep_day_due:
            sections.append("## 睡眠期到期的备忘提醒\n" + "\n".join(f"- {e['content']}({e['remind_at']})" for e in sleep_day_due))
        path = report_dir / f"sleep_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        path.write_text("\n\n".join(sections) or "(无内容)", encoding="utf-8")
        os.chmod(path, 0o600)  # 报告含消息文本/用户标识,仅属主可读(安全纪律)
        self.ctx.logger.info("睡醒回顾已生成: %s", path)

    async def _generate_tomorrow_schedule(self, target_date: str) -> None:
        """入睡任务的日程侧:生成醒来日日程(旁路 LLM 不经消息链,睡眠期可执行);失败用默认作息模板并告警。

        目标日由调用方显式传入(=将要醒来的自然日):默认配置(睡眠窗 23:00 起+
        静默 60 分钟)下静默入睡必然发生在午夜后,此刻 now+1 会得到醒来日的次日,
        生成的日程醒来后首个 tick 因日期≠今天被丢弃,整天跑默认模板——故不得在
        函数内从当前时刻推导目标日。
        """

        target = target_date
        due = [f"{e['content']}({e['remind_at'][11:16]})" for e in self.memo.due_on(target)]
        try:
            persona, style = await self._persona_context()
            data, err = await self.schedule_gen.generate(
                persona=persona, behavior_style=style, today_review=self._today_review_text(),
                weather_text=self._weather_text(), fav_summary=self._fav_summary_text(),
                due_memos=due, target_date=target,
            )
        except Exception:
            self.ctx.logger.exception("醒来日日程生成异常,使用默认作息模板")
            data, err = _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target), "异常"
        if err:
            self.ctx.logger.warning("醒来日日程生成:%s(模板兜底)", err)
        self._schedule_data = data
        self._schedule_generated = (not err)  # 模板兜底日不视为生成日程,备忘提醒兜底保持开启
        self._schedule_edit_history = []
        self._persist_schedule()
        self.ctx.logger.info("醒来日日程已生成(target=%s):%s", target, json.dumps(data, ensure_ascii=False)[:200])

    async def _diary_chat_timeline(self, *, max_messages: int = 300, per_message_chars: int = 100, day: str | None = None) -> str:
        """日记聊天时间线素材(2026-09-02 对齐 diary_plugin message_fetcher 蓝本)。

        经 message.get_by_time **全局**拉素材日 00:00 起的全部消息(跨全部聊天
        流,不限条数——旧逐流 get_recent 只覆盖插件缓存的流且每流限量,一天的
        聊天记录拿不全);素材日由调用方传入(day=日记素材日),午夜后入睡时
        仍取入睡日,不用 now 取日。空间虚拟流消息剔除(日记素材=真实聊天)。
        逐条「[HH:MM] 谁说了什么」按时间序铺开(像翻一天的聊天记录),单条截
        per_message_chars 字加"...";总量超限保留最近 max_messages 条并标注
        更早的略过;bot 标「我」,他人标昵称(缺省回退 QQ 号);纯图片/表情等
        无文本消息不进时间线。能力失败或返回形态异常均显式告警后回退旧逐流
        路径(显式回退不静默);无可用消息返回空串(素材行整体省略,不臆造
        聊天内容)。"""

        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        now = datetime.now()
        try:
            day_start = datetime.strptime(str(day or now.strftime("%Y-%m-%d")), "%Y-%m-%d")
        except ValueError:
            # 素材日非法(调用方契约破坏):显式告警后按今天取数,不静默也不抛错
            self.ctx.logger.warning("日记时间线素材日非法(%r),按今天取数", day)
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        msgs: list[dict] | None = None
        try:
            # 蓝本同款:limit=0 不限条数,时间窗=素材日全天(上界取 now 防时钟毛刺)
            result = await self.ctx.call_capability(
                "message.get_by_time",
                start_time=day_start.timestamp(), end_time=now.timestamp() + 1,
                limit=0, limit_mode="earliest", filter_mai=False,
            )
            # 实机形态(2026-09-02 联调):SDK 已解包 messages 键,直接返回 list
            # (get_recent 同款);dict+success 形态保留兼容,两种都能吃
            if isinstance(result, list):
                msgs = [m for m in result if isinstance(m, dict)]
            elif isinstance(result, dict) and result.get("success"):
                loaded = result.get("messages")
                if isinstance(loaded, list):
                    msgs = [m for m in loaded if isinstance(m, dict)]
                else:
                    # success 但缺 messages 键/非 list:形态异常不得静默按 0 条处理
                    # (日记会误写「今天没和人聊天」),显式告警后走逐流回退
                    self.ctx.logger.warning(
                        "get_by_time 返回形态异常:success 但缺 messages 键,回退逐流取数"
                    )
            else:
                self.ctx.logger.warning(
                    "日记聊天素材 get_by_time 返回失败形态(%s),回退逐流取数",
                    "非dict非list" if not isinstance(result, (dict, list)) else "success=False",
                )
        except Exception:
            self.ctx.logger.exception("日记聊天素材 get_by_time 能力异常,回退逐流取数")
        if msgs is None:
            msgs = await self._diary_timeline_legacy_msgs(bot_uin, max_messages)
        entries: list[tuple[float, str]] = []
        for m in msgs:
            # 空间虚拟流剔除(平台判定,全局取数跨未知流,不能靠会话名单)
            if is_qzone_message(m):
                continue
            info = (m.get("message_info") or {}).get("user_info") or {}
            sender = str(info.get("user_id") or "")
            if not sender:
                continue  # 无发送者的畸形条目跳过
            text = str(m.get("processed_plain_text") or "") or "".join(
                s.get("data", "") for s in (m.get("raw_message") or [])
                if isinstance(s, dict) and s.get("type") == "text"
            )
            if not text.strip():
                continue  # 纯图片/表情等无文本消息不进时间线
            try:
                ts = float(str(m.get("timestamp") or ""))
                dt = datetime.fromtimestamp(ts)
            except (ValueError, TypeError, OSError):
                continue  # 无有效时间戳无法定位,跳过
            if dt < day_start:
                continue  # 素材日全天素材:早于素材日 00:00 的消息不进时间线
            who = "我" if sender == bot_uin else (str(info.get("user_nickname") or "") or sender)
            entries.append((ts, f"[{dt:%H:%M}] {who}:{clip_text(text, per_message_chars)}"))
        if not entries:
            return ""
        entries.sort(key=lambda e: e[0])
        lines = [line for _ts, line in entries]
        if len(lines) > max_messages:
            # 保留最近的:一天的聊天太长时,丢最早的并显式标注(不静默截断)
            lines = ["(更早的聊天已略)"] + lines[-max_messages:]
        return "\n".join(lines)

    async def _diary_timeline_legacy_msgs(self, bot_uin: str, max_messages: int) -> list[dict]:
        """旧逐流取数回退(get_by_time 不可用时):流缓存内逐流 get_recent,
        当日过滤在主装配做。bot_uin 参数保留对位(过滤在公共装配层)。"""

        del bot_uin
        virtual_ids = self._qzone_session_id_set()
        out: list[dict] = []
        for stream_id in sorted(self._stream_cache):
            if not stream_id or stream_id in virtual_ids:
                continue
            try:
                out.extend(await self._fetch_recent(stream_id, max_messages))
            except Exception:
                # 单流失败只跳过该流(同衰减取数纪律),不拖垮日记整体
                self.ctx.logger.warning("日记聊天素材取数失败(stream=%s),该流跳过", stream_id)
        return out

    def _diary_weather_line(self) -> str:
        """日记天气素材行:当前真实天气(time_aware 快照,日程生成同款来源)。

        无快照数据时省略该行(不臆造天气);读取异常显式告警后同样省略——
        素材行可缺,日记主链路不被单个素材拖垮。"""

        try:
            text = self._weather_text()
        except Exception:
            self.ctx.logger.exception("日记天气素材读取失败,素材行省略")
            return ""
        if text == "无数据":
            return ""
        return f"当前天气:{text}"

    async def _generate_and_publish_diary(self, sleep_day: str | None = None) -> None:
        """入睡任务的日记侧:睡前用当日素材生成日记并发布为空间说说。

        与日程生成同属入睡任务——旁路 LLM 与发布 API 均不经消息链,不受睡眠
        拦截(深夜直发)。素材只取当日真实数据(日程活动/备忘/空间见闻/聊天
        时间线/真实天气,可省略行见各构建器),模板明令不得编造,防日记虚构
        没发生的事;篇幅区间由配置指导(diary_word_count_min~max 进素材行,
        不做随机化)。发布成功后正文存 pending
        快照,回注延迟到醒来(睡眠期 route_message 会被 sleep_gate 拦进回顾
        缓冲,白注入)。
        素材日由调用方传入(sleep_day=睡眠窗口起始日):午夜后入睡时当前时刻
        已是次日,日记素材仍属入睡日,不得用 now 取日。
        """

        if not self.config.qzone.enabled or not self.config.qzone.diary_enabled:
            return
        if not self._qzone_available:
            return
        today = sleep_day or datetime.now().strftime("%Y-%m-%d")
        try:
            day_dt = datetime.strptime(today, "%Y-%m-%d")
        except ValueError:
            # 素材日非法(调用方契约破坏):显式告警后按今天,好过日期行写错或抛错
            self.ctx.logger.warning("日记素材日非法(%s),按今天生成", today)
            day_dt = datetime.now()
            today = day_dt.strftime("%Y-%m-%d")
        # 日程素材只取活动窗口(睡眠窗口没有「做过什么」);与醒来日日程生成并发
        # 时本任务先读今日日程(派发序保证,LLM 调用前完成素材组装)
        schedule_summary = ";".join(
            str(w.get("activity") or "") for w in (self._schedule_data.get("windows") or [])
            if w.get("kind") != "sleep"
        )
        memos = ";".join(e["content"] for e in self.memo.due_on(today)[:3])
        seen_feeds = self.qzone_seen.recent_seen(limit=3, days=1, now=datetime.now())
        seen_summary = ";".join(clip_text(e["summary"], 20) for e in seen_feeds)
        # 补聊天时间线与真实天气两素材(可省略行,见各构建器)
        chat_timeline = await self._diary_chat_timeline(day=today)
        weather_line = self._diary_weather_line()
        # 素材蓝本形态(diary_plugin prompts.py,2026-09-02):「我的名字是…/人设/
        # 今天是{日期},回顾一下到现在为止的聊天记录:{时间线}」在前,当日其余
        # 素材随后;「日记内容:」收尾作生成引导——单串蓝本里它紧跟指令,两段式
        # 布局下放素材尾等价(生成恰从引导语后开始)。人设为第二人称散文体
        # (「你是…」),不套蓝本的「我{personality_desc}」前缀以免读破
        nickname = await self._bot_echo_nickname()
        persona, _ = await self._persona_context()
        # 篇幅指导(去目标字数随机化,对齐 diary_plugin
        # qzone_min/max_word_count 形态):配置区间直接进素材行作软指导,
        # 模板指令句的长度口径引用这一行
        material = (
            f"我的名字是{nickname}\n"
            f"{persona}\n\n"
            f"今天是{day_dt.year}年{day_dt.month}月{day_dt.day}日,回顾一下到现在为止的聊天记录:\n"
            f"{chat_timeline or '(今天没和人聊天)'}\n\n"
            f"今天的日程:{schedule_summary or '自由活动'}\n"
            f"备忘:{memos or '无'}\n看到的好友动态:{seen_summary or '无'}\n"
        )
        if weather_line:
            material += f"{weather_line}\n"
        material += (
            f"(目标篇幅{self.config.qzone.diary_word_count_min}"
            f"~{self.config.qzone.diary_word_count_max}字)\n\n日记内容:"
        )
        messages, _ = build_side_prompt("qzone_diary", [material], [])
        # 生成温度可配置(-1=不传,走主程序任务默认)
        temperature = self.config.qzone.diary_llm_temperature
        if temperature < 0:
            temperature = None
        try:
            result = await self._side_llm_call(
                messages, self.config.qzone.diary_llm_model, "qzone_diary",
                self.config.qzone.diary_llm_timeout_ms, temperature=temperature
            )
        except Exception:
            self.ctx.logger.exception("QQ空间日记 LLM 生成失败,跳过本轮")
            return
        if not isinstance(result, dict) or not result.get("success"):
            # 不落响应原文(安全纪律):仅记失败形态
            detail = f"success={result.get('success')}" if isinstance(result, dict) else f"结果类型={type(result).__name__}"
            self.ctx.logger.warning("QQ空间日记 LLM 失败(%s),跳过", detail)
            return
        diary_text = str(result.get("response") or "").strip()
        # 不设硬上限截断/拦截(对齐 diary_plugin:长度完全由
        # 素材行的篇幅区间软约束;蓝本也无上限)。仅拦空文本——空日记没有发布意义
        if not diary_text:
            self.ctx.logger.warning("QQ空间日记内容为空,跳过发布")
            return
        # v1.0.0 内容护栏:LLM 产出文本发布前匹配,命中即拦截——不发布不落快照
        hit = match_guard(self._guard_compiled, diary_text)
        if hit:
            self.ctx.logger.warning(
                "内容护栏拦截:日记 命中规则%d,未发布(文本:%s...)", hit, diary_text[:60]
            )
            return
        try:
            # 同轮自愈(与 qzone_post 同款):
            # AuthError 作废并重取 cookie 后原地重试一次——未接入前登录态失效当晚
            # 日记会静默丢失(入睡任务无用户回执,失败只有日志可见)。
            tid, auth_err = await self._qzone_auth_retry(
                lambda: self.qzone_client.do_publish(content=diary_text), "日记发布")
            if auth_err:
                self.ctx.logger.warning("QQ空间日记发布失败:%s(内容已生成,发布跳过)", auth_err)
                return
        except Exception:
            self.ctx.logger.exception("QQ空间日记发布失败(内容已生成,发布跳过)")
            return
        if not tid:
            # 发布已远端成功,仅醒来回注缺锚——显式告警,不误报失败
            self.ctx.logger.warning("QQ空间日记发布成功但响应未含新说说 tid,回注缺锚")
        self.ctx.logger.info("QQ空间日记发布成功: %s", diary_text[:30])
        # 回注延迟到醒来:存入 pending 快照,醒态 sleep_tick 补注(tid 随快照
        # 传递,醒后据此锚定 seen/registry;空串同存——旧快照兼容口径)
        self._pending_diary_snapshot.save({
            "text": diary_text,
            "published_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "tid": tid,
        })

    async def _echo_pending_diary(self) -> None:
        """醒来后补注昨晚发布的日记(self 消息,仅入历史)。

        回注是上下文锚:好友次日评论日记说说时,bot 需要这段历史才知道自己
        昨晚写过什么;回注正文带全文(不做截断)。快照带 tid 时尾部加〔说说ID=前12位〕锚并锚定 seen/registry
        (与 qzone_post 同款;旧快照无 tid 则无锚)。不设 is_mentioned——
        bot 自己的旧说说不需要触发决策轮。route_message 失败保留快照,
        醒态 sleep_tick 下轮重试。
        """

        data = self._pending_diary_snapshot.load()
        text = str(data.get("text") or "").strip()
        if not text:
            return
        tid = str(data.get("tid") or "")
        bot_uin = str(self.config.favorability.bot_user_id or "").strip()
        # 构造纳入 route try:取昵称失败直接抛错(不兜底),若留在 try 外,
        # sleep_tick 每 60s 的补注会持续抛错——入睡判定
        # 链被瘫痪且快照永不清空;纳入后按补注失败处理(告警+保留快照),
        # 下个 tick 重试。
        try:
            self._qzone_seq += 1
            msg = {
                "message_id": f"qzone_self_diary_{int(time.time())}_{self._qzone_seq}",
                "platform": QZONE_PLATFORM,
                "timestamp": str(int(time.time())),
                "message_info": {
                    "user_info": {"user_id": bot_uin, "user_nickname": await self._bot_echo_nickname()},
                    "group_info": {
                        "group_id": QZONE_VIRTUAL_GROUP_ID,
                        "group_name": QZONE_VIRTUAL_GROUP_NAME,
                    },
                },
                "raw_message": [{"type": "text",
                                 "data": f"我昨晚发布的日记:{text}" + (f"\n〔说说ID={tid[:12]}〕" if tid else "")}],
            }
            await self.ctx.gateway.route_message(QZONE_GATEWAY_NAME, msg)
            # 本地锚定三连(seen + registry,与 qzone_post 同款;summary/摘要存
            # 全文,不做截断)。内层独立兜底:锚定失败不拦快照清空——远端已成功,
            # 补注只做一次,失败仅告警(浏览发现层后续自行补登记)。
            if tid:
                try:
                    self.qzone_seen.mark_queued(tid, abstime=str(int(time.time())), author_uin=bot_uin,
                                                summary=text, author_nickname="我")
                    self.qzone_seen.mark_seen(tid, datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                                              msg["message_id"])
                    self._qzone_registry.register(FeedContext(
                        tid=tid, owner_uin=bot_uin, owner_nickname="我", kind="self",
                        content_summary=text,
                    ))
                except Exception:
                    self.ctx.logger.exception("QQ空间日记补注本地锚定失败(远端已成功,仅告警)")
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
        logger.debug("schedule_tick 进入窗口 kind=%s 活动=%s", win.get("kind"), win.get("activity"))
        if win.get("read_qzone") or win.get("send_qzone"):
            # qzone 窗口开始即首拉:拉取间隔=两次拉取的间距而非独立节奏,进入
            # 窗口立即派发一轮拉取(60 秒粒度检出),窗口内后续刷新由定间隔
            # 任务承担;poll_feeds 的间距判定防两路相邻撞车。置于发言上限判定
            # 之前——刷空间与发不发消息互不牵连,超限当天入窗首拉不得被牵连跳过
            await self._qzone_poll_tick()
        if self._speak_counts.get(day, 0) >= self.config.schedule.daily_speak_limit:
            logger.debug("schedule_tick 跳过:已达每日发言上限 %s", self.config.schedule.daily_speak_limit)
            return
        if win.get("kind") == "greeting":
            await self._greet_exclusive(day, win)  # 主动问候:仅特别者+私聊通道,无日程窗口的群流路径
            self._schedule_tick_fired[day] = mark
            return
        await self._window_trigger(day, win)
        self._schedule_tick_fired[day] = mark

    async def _window_trigger(self, day: str, win: dict) -> None:
        """日程窗口 trigger:门槛过滤 → 活跃流排序取前 n → 每流 trigger(计 1)。"""

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
        """候选流(日程窗口日常发言):近 24h 活跃流按人取等级,门槛固定 speak_threshold_level,
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
        logger.debug("日程候选流: 活跃 %d -> 门槛过滤后 %d", len(rows), len(candidates))
        return [{k: c[k] for k in ("stream_id", "user_id", "level_name", "note")} for c in candidates]

    async def _greet_exclusive(self, day: str, win: dict) -> bool:
        """主动问候:仅「特别」等级者 + 必须存在私聊流;greeting 窗口起点触发,无每日一次限制。"""

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
        # 防 remind_fired.json 跨天无限增长)
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
                continue  # 失败不标记:留重试机会
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
        """当前天气文本:读 weather_snapshot 快照(环境刷新落库)。

        无快照、或快照过期(距 fetched_at 超过 WEATHER_MAX_AGE_HOURS——环境刷新
        持续失败时旧天气不得无限期当当前天气用)均返回 '无数据';fetched_at 解析
        失败同样按过期处理(解析异常不得抛出)。"""

        rows = self.store.query(
            "SELECT data, fetched_at FROM weather_snapshot WHERE id = 1 ORDER BY fetched_at DESC LIMIT 1"
        )
        if not rows:
            return "无数据"
        try:
            fetched_at = datetime.strptime(str(rows[0][1]), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            fetched_at = None
        if fetched_at is None or datetime.now() - fetched_at > timedelta(hours=WEATHER_MAX_AGE_HOURS):
            # 刷新失败已有告警,此处过期只记 debug(不重复 warning 防刷屏)
            self.ctx.logger.debug("天气快照过期或时刻不可解析(fetched_at=%s),按无数据处理", rows[0][1])
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
        """重启恢复:schedule.json 的 date 为今天或明天时恢复日程/编辑历史/生成标记。

        入睡生成的日程日期=醒来日(午夜前入睡时即「明天」,午夜后入睡时即「今天」),
        夜间重启不得误删——date ∈ {今天, 明天} 均恢复;
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
        """按人结算(规格全局决策):聚合该人所有流的消息,一次 LLM 判定。

        并发防护保留:fav_count 与 _daily_settle 可能并发发起
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
            # 空间互动事件并入结算素材(LLM 计权无硬编码数值)。
            # 本方法素材形态是 history 列表(build_material 的输入 list[dict]),
            # 事件按合成 user 消息追加:role/user_id=目标保证进入素材锚点;
            # stream_id 用合成流隔离邻居,seq 取大且逐条唯一(build_material 以
            # (stream,seq) 去重)。ts 用事件原始时刻——同日多次结算(early→daily)时
            # 首次结算已把 window_start 前移,已判事件被窗口过滤排除(真实消息同机制),
            # 防同一事件反复并入素材重判
            # 取数改 window_start 滚动窗(fav_events_since),与
            # 聊天消息同窗口口径——旧自然日取数在跨零点结算(如 00:30
            # 日终)时昨夜事件 day=昨日恒取空,空间互动素材整窗漏判。窗口起点空串
            # (该人尚无结算记录)= 全量事件,与 build_material 空窗口语义一致。
            window_start = (self.fav_engine.get_level(user_id) or {}).get("window_start") or ""
            try:
                events = self.qzone_comment_seen.fav_events_since(user_id, window_start)
            except Exception as exc:  # noqa: BLE001
                events = []
                self.ctx.logger.warning("空间互动事件读取失败,本次结算不含事件素材(user=%s):%s", user_id, rpc_error_brief(exc))
            for i, e in enumerate(events[-5:]):  # 取最近 5 条(首次结算无窗口会命中全量,旧互动无当前状态代表性)
                label = QZONE_FAV_EVENT_LABELS.get(e["kind"], "空间互动")
                history.append({
                    "role": "user",
                    "user_id": user_id,
                    "stream_id": "qzone-events",  # 合成流:不与真实流撞 id,事件互为邻居
                    "text": f"[空间互动] {label}: {clip_text(e['text'], 60)}",
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
        self, messages: list[dict], model: str, module: str, timeout_ms: int | None = None,
        temperature: float | None = None
    ) -> dict:
        """旁路 LLM 统一出口:model 填主程序 task 名;用量按模块记账。

        经 call_capability 直调,超时由各能力配置节传入(0/None=主程序默认 30s;
        配置默认值用 0 而非 None——主机配置回写经 tomlkit 序列化,None 会致激活失败)。
        联调实测:utils 模型 31-53s 会触发默认超时,慢模型建议配置 120000。
        temperature 仅在显式传入时携带(主机 core.py 采纳 args["temperature"],
        None 走任务默认)——当前仅日记生成使用(生成风格可配置)。
        """

        call_args: dict[str, Any] = {
            "timeout_ms": timeout_ms or None, "prompt": messages, "model": model or ""
        }
        if temperature is not None:
            call_args["temperature"] = temperature
        result = await self.ctx.call_capability("llm.generate", **call_args)
        if isinstance(result, dict):
            if "model" not in result and result.get("model_name"):
                result = {**result, "model": result["model_name"]}
        self._record_llm_usage(module, result)
        return result

    def _record_llm_usage(self, module: str, result: dict) -> None:
        """旁路调用记账(可观测性):次数+token 按日/模块分列;超阈值告警。"""

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
            self._llm_warned_day = day  # 跨越阈值当天只告警一次(防每次调用刷屏)
            self.ctx.logger.warning("旁路 LLM 当日调用次数已达或超过阈值 %s,请注意用量", total)

    async def _fetch_recent(self, stream_id: str, limit: int) -> list[dict]:
        """取近期消息。实测:返回 list;image 段仅 hash。

        公测发现:include_binary_data=True 时,含大附件(数十 MB)的消息会把 RPC
        响应帧撑爆(主机 16MB 上限,E_UNKNOWN)——插件消费方(衰减互动判定/说话人
        解析/结算素材)只用文本与元数据,二进制一律不取。
        注入消息 timestamp=阅读时刻,宿主默认 24h 窗天然适用。
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
            # 仅记异常类型,不插值 exc 本体(安全纪律,同 decay.py)
            return None, f"能力调用异常({rpc_error_brief(exc)})"
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
        if reply_ids:  # 解析观测日志(每次调用一条,低频:结算/衰减路径;仅计数,不落明文 QQ 号——安全纪律)
            resolved = sum(1 for v in senders.values() if v)
            self.ctx.logger.info("quote 发送者解析: 成功 %d/%d(stream=%s)", resolved, len(senders), stream_id)
        return senders, first_err

    async def _fetch_recent_for_history(self, stream_id: str, limit: int, target_user_id: str = "") -> list[dict]:
        """取近期消息并归一化为 build_material 所需形状
        {role, user_id, stream_id, text, seq, ts, is_group, addressed}。

        实测:消息 dict 键含 message_id/timestamp/platform/message_info/raw_message/
        is_*/session_id/processed_plain_text;user 在 message_info.user_info。
        role:message_info.user_info.user_id == bot_user_id 判为 bot(配置留空则一律 user)。
        addressed 仅群聊 bot 消息有意义,两类命中(互动定义):
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
        # quote 语义:群聊 bot 消息 reply 段先批量收集、再逐条解析原发送者
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
                # 兜底:从 raw_message 段拼文本(text 段 data 直接是字符串)
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

    def _normalize_ts(self, raw_ts: Any) -> str:
        """消息时间戳归一化为 ISO(与 favorability.window_start 同格式,保证窗口过滤可比)。

        实机实测:主程序序列化的 timestamp 为 epoch 浮点(字符串);直接与 ISO window_start
        字符串比较恒 False,导致批次素材恒空(联调发现)。
        实例方法:原 staticmethod 的 except 分支引用 self,
        坏时间戳触发即 NameError;调用点 self._normalize_ts(...) 不受影响。
        """

        if raw_ts is None:
            return ""
        try:
            return datetime.fromtimestamp(float(raw_ts)).strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError, OSError, OverflowError):
            # OverflowError:超大 epoch(如 inf)超出平台范围,同样按坏值处理不上抛
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
        """debug 日志开关:开启时把 catsitate.core 的 debug 级日志落盘到数据目录 logs/ 当日文件。

        文件权限 0600(日志含 user_id/stream_id,仅属主可读——安全纪律);关闭时移除并
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
            os.chmod(path, 0o600)  # 仅属主可读(安全纪律:日志含用户标识)
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

        on_unload 卸载与 debug 开关关闭共用(关闭分支语义抽离);
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

    async def _voice_style(self) -> str:
        """bot 表达方式(主程序全局配置 personality.reply_style,带缓存;空则润色层按无人设口吻)。"""

        if self._reply_style_cache is not None:
            return self._reply_style_cache
        try:
            value = await self.ctx.config.get("personality.reply_style", "")
        except Exception:
            self.ctx.logger.exception("读取 bot 表达方式失败,润色按默认口吻")
            value = ""
        self._reply_style_cache = str(value or "").strip()
        return self._reply_style_cache

    async def _bot_echo_nickname(self) -> str:
        """回注/种子消息的发送者昵称:用主程序 bot 昵称(bot.nickname)。

        主程序对 bot 自身消息按 bot 名显示;虚拟流平台(qzone-qq)的 bot 账号
        经主程序 [bot].platforms 声明后,自身判定与显示替换才能命中——回注
        发送者若标「我」会被当普通用户(污染回复必要性与间隔统计)。
        """

        # 直接抛错不兜底:昵称只有主程序有 bug 才会为空,
        # 静默回退「我」反而掩盖——曾把 bot 当普通用户污染统计。调用方纪律:
        # qzone_post 在润色与发布 try 之前读取
        # (异常原样上抛,零润色/零发布调用);日记补注/种子自举在各自 try 内
        # 构造(异常被捕获告警,不沿 tick 链外泄)
        value = await self.ctx.config.get("bot.nickname", "")
        nickname = str(value or "").strip()
        if not nickname:
            raise RuntimeError("bot 昵称(bot.nickname)读取失败或为空——主程序配置异常,请检查 bot_config")
        return nickname

    async def _recent_context_text(self, stream_id: str, limit: int) -> str:
        raw = await self._fetch_recent(stream_id, limit)
        lines = []
        for m in raw:
            text = str(m.get("processed_plain_text") or "")
            if not text:
                # 兜底:raw_message 段(data 键)
                text = "".join(s.get("data", "") for s in (m.get("raw_message") or []) if isinstance(s, dict) and s.get("type") == "text")
            lines.append(f"[{m.get('message_id')}] {text}")
        return "\n".join(lines)


def _tool_bool(value: Any) -> bool:
    """工具布尔参数形态矫正:宿主 materialize_args 不按声明类型矫型,模型
    可能传字符串("true"/"false")——bool("false") 为 True 会静默错置标记,
    故字符串仅按明确真值词判定,其余(含缺省)一律 False。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是")
    return bool(value)


def _pack_image_content_items(pack: FeedImagePack) -> list[dict]:
    """工具出口打包差异:管线 segments →
    content_items(宿主 tool result media 入库形态)。合成图恒 JPEG
    (mime 不再探测);单图原图直发保留魔数探测。"""

    items: list[dict] = []
    for _url, data in pack.segments:
        if not data:
            continue  # 极端超预算丢弃:不进 content_items(锚也已置空,不误导反查)
        items.append({
            "content_type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mime_type": "image/jpeg" if pack.composed else _sniff_image_mime(data),
        })
    return items


def _sniff_image_mime(data: bytes) -> str:
    """图片魔数探测(PNG/GIF/WebP,其余按 jpeg)——content_items 的 mime 按
    真实字节标注,未压缩原图不再被硬标 jpeg。"""

    head = bytes(data[:12])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def create_plugin() -> CatsitatePlugin:
    """插件工厂(入口约定)。"""

    return CatsitatePlugin()
