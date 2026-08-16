"""Catsitate 核心插件入口:薄接线层,业务逻辑全部在 catsitate_core 包内。

规格:docs/superpowers/specs/2026-08-14-catsitate-core-maibot-design.md
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncio
import hashlib
import json
import logging
import os
import sys

import httpx
from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
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


class CatsitatePlugin(MaiBotPlugin):
    """Catsitate 猫耳少女核心插件。"""

    config_model = CatsitateConfig
    config_reload_subscriptions = ("bot",)

    _persona_cache: str | None = None  # bot 人设缓存(config.get 一次,bot 配置变更时失效)
    _style_cache: str | None = None  # bot 行为风格缓存(同上)
    _debug_handler: logging.Handler | None = None  # debug 日志文件 handler(配置开关控制)
    _debug_prev_level: int = logging.NOTSET  # 开启前 logger 级别(关闭时恢复)

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
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
        # 睡眠期拦截消息缓冲(回顾报告素材);持久化防重启丢失(联调发现)
        self._sleep_review_buffer_snapshot = JsonSnapshot(data_dir / "sleep_review_buffer.json")
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
        self._snapshot_cache: dict[str, dict] = {}  # 注入块文本 -> 快照 UserMessageItem
        self._env_cache: dict[str, str] = {}  # content_key -> 环境块文本
        self._env_fetched_at: datetime | None = None
        self._stream_cache: dict[str, dict] = {}  # session_id -> 流信息(说话人解析,10 分钟 TTL)
        self._stream_cache_at: float = 0.0
        self._settling: set[str] = set()  # 结算并发防护键(按人,user_id;最终审查 Important#1/M2)
        self._background_tasks: set[asyncio.Task] = set()  # 后台任务引用(最终审查 Important#2)
        self._scheduler = Scheduler(tick_seconds=60)
        self._scheduler.register("weather", max(self.config.time_aware.weather_refresh_minutes, 1) * 60, self._refresh_environment)
        self._scheduler.register("holiday", 24 * 3600, self._refresh_environment)
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
        detailed_description="写入后会在后续对话中注入(当前流+当前说话人两个维度,合计上限见配置),过期自动清理。",
        parameters=[
            ToolParameterInfo(name="content", param_type="string", description="备忘内容,≤80 字符", required=True),
            ToolParameterInfo(name="stream_id", param_type="string", description="关联聊天流,默认当前流", required=False),
            ToolParameterInfo(name="user_id", param_type="string", description="关联用户,默认当前说话人", required=False),
            ToolParameterInfo(name="ttl_hours", param_type="number", description="单条有效期小时数,缺省用默认", required=False),
            ToolParameterInfo(name="remind_at", param_type="string",
                              description="可选提醒时刻,ISO 格式如 2026-08-16T19:00。重要:备忘内容含时间要求时(如「5分钟后」「今晚8点」)必须换算为绝对时间传入本参数,否则到期不会提醒", required=False),
        ],
        visibility="visible",
    )
    async def memo_write(self, content: str = "", stream_id: str = "", user_id: str = "", ttl_hours: float | None = None, **kwargs: Any) -> str:
        if not self.config.plugin.enabled or not self.config.memo.tool_enabled:
            return "备忘录工具未启用。"
        remind_at = str(kwargs.get("remind_at") or "")
        if err := validate_remind_at(remind_at):
            return err  # 非法提醒时间显式返回给 LLM(审查 M-10)
        ok, msg = self.memo.write(content, stream_id or str(kwargs.get("stream_id") or ""), user_id or str(kwargs.get("user_id") or ""), ttl_hours, remind_at=remind_at)
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
            return f"选表情 LLM 调用失败:{str(result)[:200]}"
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
            detail = result.get("response", "")[:200] if isinstance(result, dict) else str(result)[:200]
            msg = f"图片重看 LLM 调用失败:{detail}"
            self.ctx.logger.warning(msg)
            return msg
        return str(result.get("response") or "")

    # ---------- 命令 ----------

    @Command("/记一下", description="记一条短时备忘", pattern=r"^/记一下\s+(?P<content>.+)$", aliases=["/备忘"])
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
            rendered = self.assembler.render(blocks)
            if not rendered:
                self.ctx.logger.debug("注入跳过: render 结果为空(blocks=%d)", len(blocks))
                return {"action": "continue", "modified_kwargs": kwargs}
            # spike ② 实测:items 须为合法快照格式,朴素 dict 会被主程序拒绝——render 输出转快照后插入
            rendered = [self._to_snapshot_item(m["content"]) for m in rendered]
            insert_at = self._system_tail_index(messages)
            new_messages = messages[:insert_at] + rendered + messages[insert_at:]
            new_kwargs = {**kwargs, self._MESSAGES_KEY: new_messages}
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
        # spike ③ 实测:user/stream 在 message 内(user_info 与 session_id;user_id 字段名以实机联调为准)
        msg_info = msg.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        user_id = str(user_info.get("user_id") or user_info.get("sender_id") or "")
        stream_id = str(msg.get("session_id") or "")
        if not user_id or not stream_id:
            return
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
        self._last_activity_ts = datetime.now().timestamp()  # 任何出站回复都算活动(静默入睡计时)
        if self.sleep.is_sleeping() or not self._in_goodnight_window():
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
            detail = result.get("response", "")[:200] if isinstance(result, dict) else str(result)[:200]
            self.ctx.logger.warning("晚安判定 LLM 失败,本轮不入睡:%s", detail)
            return {"action": "continue", "modified_kwargs": kwargs}
        verdict, _ = parse_sleep_confirm_response(str(result.get("response") or ""))
        if verdict == "SLEEP":
            await self._enter_sleep()
        return {"action": "continue", "modified_kwargs": kwargs}

    def _in_goodnight_window(self) -> bool:
        """可入睡时间:睡前语境活动期间(活动窗口 kind=greeting 且 activity 含睡眠关键词)。"""

        win = current_window(self._schedule_data, datetime.now().strftime("%Y-%m-%dT%H:%M"))
        return bool(win and win.get("kind") == "greeting" and any(k in str(win.get("activity") or "") for k in ("睡", "洗漱", "晚安", "休息", "就寝")))

    async def _enter_sleep(self) -> None:
        """入睡:计算 clamp 醒来时刻,状态落盘,触发生成次日日程(睡眠期间唯一 LLM 调用)。"""

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
        self.ctx.logger.info("已入睡:醒来 %s", wake_at)
        self._spawn_background_task(self._generate_tomorrow_schedule())

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
            self.ctx.logger.exception("后台任务异常:%s", exc)

    def _to_snapshot_item(self, text: str) -> dict:
        """渲染块 → 合法快照 UserMessageItem(spike ②:朴素 dict 被主程序拒绝)。

        同文本返回同一对象(前缀缓存纪律,缓存键=文本);item_id 用 sha256 前缀,
        勿用内置 hash()(进程内哈希随机化);timestamp 随对象绑定(创建时确定)。
        """

        cached = self._snapshot_cache.get(text)
        if cached is not None:
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
        return item

    async def _build_inject_blocks(self, kwargs: dict[str, Any]) -> list[InjectionBlock]:
        cfg = self.config
        # planner.before_request payload 无 user_id/stream_id 键(实机确认):
        # 流 = session_id;说话人 = 私聊流对端 / 群聊最近非 bot 消息发送者
        stream_id = str(kwargs.get("session_id") or "")
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
        if cfg.inject.memo_enabled and cfg.memo.enabled:
            # 当前流维度 + 当前说话人维度各取 3 条,按 id 去重后合计 ≤ inject_max(规格 §4.4)
            by_stream = self.memo.read(stream_id, "", limit=3)
            by_user = self.memo.read("", speaker, limit=3) if speaker else []
            seen: set[int] = set()
            entries = []
            for entry in by_stream + by_user:
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
            result = await self.ctx.chat.get_all_streams()
            # SDK 对 chat.get_all_streams 解包后返回裸 list;兼容 dict 包装形状(实机 2.3 验收发现)
            if isinstance(result, list):
                raw = result
            elif isinstance(result, dict) and result.get("success"):
                raw = result.get("streams") or []
            else:
                self.ctx.logger.warning("聊天流列表形状异常(%s),沿用旧缓存", type(result).__name__)
                return
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
        for user_id in self.fav_engine.iter_today_active():
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
            return
        win = current_window(self._schedule_data, now_iso)
        if not win:
            return
        if win.get("kind") == "sleep" and now_iso >= win.get("start"):
            self.ctx.logger.info("睡眠窗口起点已到,兜底强制入睡")
            await self._enter_sleep()
            return
        # 静默入睡:仅睡前语境活动期间,无任何活动满 N 分钟
        if self.config.sleep.silent_sleep_enabled and self._in_goodnight_window():
            if self._last_activity_ts and now.timestamp() - self._last_activity_ts >= self.config.sleep.silent_sleep_minutes * 60:
                self.ctx.logger.info("静默入睡:安静 %d 分钟", self.config.sleep.silent_sleep_minutes)
                await self._enter_sleep()

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
                # 最近一次衰减判定时间作为基准参与取 max(该人全局,跨流)
                decay_rows = self.store.query(
                    "SELECT judged_at FROM favorability_log WHERE user_id = ? AND judge_id LIKE 'decay-%' "
                    "ORDER BY judged_at DESC LIMIT 1",
                    (user_id,),
                )
                decay_ts = decay_rows[0][0] if decay_rows else ""
                candidates.append((user_id, max(best or "", decay_ts)))
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
        """入睡确认:生成次日日程(睡眠期间唯一 LLM 调用);失败用默认模板并告警。"""

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
        仅真正过期(早于今天)的文件删除并告警;损坏/结构非法文件告警并忽略(错误显式暴露,不静默)。
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
        if saved_date not in (now.strftime("%Y-%m-%d"), (now + timedelta(days=1)).strftime("%Y-%m-%d")):
            self.ctx.logger.warning("schedule.json 为过期日程(%s),删除并忽略恢复", saved_date)
            try:
                path.unlink()
            except OSError:
                self.ctx.logger.exception("过期 schedule.json 删除失败")
            return
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
                history.extend(await self._fetch_recent_for_history(stream_id, 50, user_id))
            persona, style = await self._persona_context()
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

        经 call_capability 直调,超时由各能力配置节传入(留空=主程序默认 30s)。
        联调实测:utils 模型 31-53s 会触发默认超时,慢模型建议配置 120000。
        """

        result = await self.ctx.call_capability(
            "llm.generate", timeout_ms=timeout_ms, prompt=messages, model=model or ""
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
        if total >= self.config.plugin.llm_daily_call_warning_threshold:
            self.ctx.logger.warning("旁路 LLM 当日调用次数已达或超过阈值 %s,请注意用量", total)

    async def _fetch_recent(self, stream_id: str, limit: int) -> list[dict]:
        """取近期消息。spike ④ 实测:返回 list;include_binary_data 透传不产生二进制(image 段仅 hash)。"""

        result = await self.ctx.call_capability("message.get_recent", chat_id=stream_id, limit=limit, include_binary_data=True)
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
