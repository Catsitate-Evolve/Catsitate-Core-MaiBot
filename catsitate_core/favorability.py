"""好感度 v3 批次结算制(规格 §4.3):纯计数触发、日终兜底、顺延不丢弃。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .config import FavorabilitySection
from .storage import SQLiteStore

LEVELS: list[str] = ["陌生", "熟悉", "亲近", "挚友", "特别"]
LEVEL_INDEX: dict[str, int] = {name: i for i, name in enumerate(LEVELS)}
EXCLUSIVE_LEVEL: int = 4  # LEVELS 下标:「特别」
_ISO = "%Y-%m-%dT%H:%M:%S"


def _level_for_score(score: int) -> int:
    """分数 → 等级下标:0-9 陌生 / 10-29 熟悉 / 30-59 亲近 / 60-99 挚友 / ≥100 特别。"""

    if score >= 100:
        return 4
    if score >= 60:
        return 3
    if score >= 30:
        return 2
    if score >= 10:
        return 1
    return 0


class BatchEngine:
    """好感度批次引擎。"""

    def __init__(self, store: SQLiteStore, config: FavorabilitySection) -> None:
        self.store = store
        self.config = config

    def ensure_schema(self) -> None:
        # 开发期裁定:检测旧形状(含 stream_id 列)直接重建,不做数据迁移
        cols = {r[1] for r in self.store.query("PRAGMA table_info(favorability)")}
        if "stream_id" in cols:
            self.store.execute("DROP TABLE IF EXISTS favorability")
            self.store.execute("DROP TABLE IF EXISTS favorability_log")
            self.store.execute("DROP TABLE IF EXISTS batch_counter")
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability (
                user_id TEXT PRIMARY KEY,
                level INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                window_start TEXT NOT NULL DEFAULT '',
                judged_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability_log (
                judge_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                delta INTEGER NOT NULL,
                note TEXT NOT NULL,
                judged_at TEXT NOT NULL
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS batch_counter (
                user_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                last_bump TEXT NOT NULL DEFAULT '',
                window_start TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (user_id, stream_id)
            )
            """
        )

    def count_message(
        self, user_id: str, stream_id: str, now: Callable[[], datetime] | None = None
    ) -> dict:
        """记录一条用户消息,仅作活跃账本 bump(batch_counter (user, stream) 行),返回该流计数。"""

        now_fn = now or datetime.now
        current = now_fn()
        self.store.execute(
            """
            INSERT INTO batch_counter (user_id, stream_id, count, last_bump)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, stream_id) DO UPDATE SET
                count = count + 1,
                last_bump = excluded.last_bump
            """,
            (user_id, stream_id, current.strftime(_ISO)),
        )
        rows = self.store.query(
            "SELECT count FROM batch_counter WHERE user_id = ? AND stream_id = ?",
            (user_id, stream_id),
        )
        return {"messages": rows[0][0]}

    def check_trigger(
        self, user_id: str, now: Callable[[], datetime] | None = None
    ) -> str | None:
        """按人判定触发:总计数 = 该人跨流 SUM(count);早结上限按人查日志。返回 "early" 或 None。"""

        now_fn = now or datetime.now
        current = now_fn()
        rows = self.store.query("SELECT SUM(count) FROM batch_counter WHERE user_id = ?", (user_id,))
        total = rows[0][0] or 0
        early_today = len(
            self.store.query(
                """
                SELECT 1 FROM favorability_log
                WHERE user_id = ? AND judge_id LIKE 'early-%' AND judged_at LIKE ?
                """,
                (user_id, f"{current.strftime('%Y-%m-%d')}%"),
            )
        )
        if total >= self.config.early_settle_threshold and early_today < self.config.daily_max_early_settle:
            return "early"
        return None

    def reset_batch(self, user_id: str, judged_at: str) -> None:
        """结算后该人所有流批次清零(judged_at 保留签名,账本不再写窗口起点)。"""

        self.store.execute(
            "UPDATE batch_counter SET count = 0 WHERE user_id = ?",
            (user_id,),
        )

    def is_exclusive_holder(self, user_id: str) -> bool:
        """「特别」之位是否被他人占据(全表最多 1 人,规格全局决策 #8)。"""

        rows = self.store.query(
            "SELECT 1 FROM favorability WHERE level >= ? AND user_id != ? LIMIT 1",
            (EXCLUSIVE_LEVEL, user_id),
        )
        return bool(rows)

    def apply_delta(
        self,
        user_id: str,
        delta: int,
        note: str,
        judged_at: str,
        judge_id: str | None = None,
    ) -> str:
        """结算结果落库:累加分数、重算等级、注记强制截断、写判定日志。

        返回状态:"ok" 或 "clamped_exclusive"(升「特别」但位被他人占据 → 钳 99 分/挚友)。
        judge_id: 判定日志幂等键;None 时默认 early-{judged_at}(日终结算须显式传 daily- 前缀)。
        """

        row = self.get_level(user_id)
        score = (row["score"] if row else 0) + delta
        level = _level_for_score(score)
        status = "ok"
        if level >= EXCLUSIVE_LEVEL and self.is_exclusive_holder(user_id):
            # 特别之位已被他人占据:钳制在 99 分(挚友),显式返回状态由调用方记录
            score = 99
            level = 3
            status = "clamped_exclusive"
        trimmed_note = note.strip()[: self.config.note_max_chars]
        current = judged_at or datetime.now().strftime(_ISO)
        log_id = judge_id or f"early-{current}"
        self.store.execute(
            """
            INSERT INTO favorability (user_id, level, score, note, window_start, judged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                level = excluded.level,
                score = excluded.score,
                note = excluded.note,
                window_start = excluded.window_start,
                judged_at = excluded.judged_at
            """,
            (user_id, level, score, trimmed_note, current, current),
        )
        # log.delta 记录判定意图,特别钳制时与实际落库分数变化可能有差
        self.store.execute(
            """
            INSERT OR IGNORE INTO favorability_log (judge_id, user_id, delta, note, judged_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (log_id, user_id, delta, trimmed_note, current),
        )
        return status

    def get_level(self, user_id: str) -> dict | None:
        rows = self.store.query(
            "SELECT user_id, level, score, note, window_start, judged_at FROM favorability WHERE user_id = ?",
            (user_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "user_id": r[0], "level": r[1], "score": r[2], "note": r[3],
            "window_start": r[4], "judged_at": r[5],
        }

    def build_material(self, user_id: str, history: list[dict]) -> list[str]:
        """结算素材(按人跨流聚合;规格全局决策 #7)。"""

        row = self.get_level(user_id)
        window_start = (row or {}).get("window_start") or ""
        fresh = [m for m in history if not window_start or m.get("ts", "") > window_start]
        fresh.sort(key=lambda m: (m.get("ts") or "", m.get("seq") or 0))
        target = [m for m in fresh if m["role"] == "user" and m["user_id"] == user_id]
        if not target:
            return []
        # 锚点数守卫:0/负数取全量,避免 [-0:] 切片语义歧义
        anchor = target[-self.config.material_max_messages:] if self.config.material_max_messages > 0 else target
        by_stream: dict[str, list[dict]] = {}
        for m in fresh:
            by_stream.setdefault(m["stream_id"], []).append(m)
        pos_of = {s: {id(m): i for i, m in enumerate(ms)} for s, ms in by_stream.items()}
        selected: dict[tuple, dict] = {}
        for msg in anchor:
            selected[(msg["stream_id"], msg["seq"])] = msg
            pos = pos_of[msg["stream_id"]][id(msg)]
            neighbors = by_stream[msg["stream_id"]][max(0, pos - 1):pos + 2]  # 前后各 1
            for neighbor in neighbors:
                selected[(neighbor["stream_id"], neighbor["seq"])] = neighbor
        user_streams = {m["stream_id"] for m in target}
        for msg in fresh:  # bot 消息随附:仅目标用户发过言的流(私聊全收,群聊仅 quote/@ 该人)
            if msg["role"] == "bot" and msg["stream_id"] in user_streams and (not msg["is_group"] or msg.get("addressed")):
                selected[(msg["stream_id"], msg["seq"])] = msg
        material: list[str] = []
        for msg in sorted(selected.values(), key=lambda m: (m.get("ts") or "", m.get("seq") or 0)):
            ctx = "群聊" if msg["is_group"] else "私聊"
            text = msg["text"]
            if len(text) > self.config.material_message_max_chars:
                text = text[: self.config.material_message_max_chars] + "…"
            material.append(f"[{msg['user_id']}]({ctx}·{msg['role']}) {text}")
        return material

    def iter_today_active(
        self, now: Callable[[], datetime] | None = None
    ) -> list[str]:
        """当日有消息且批次未清零的人(user_id 去重,日终兜底扫描对象)。"""

        now_fn = now or datetime.now
        day = now_fn().strftime("%Y-%m-%d")
        rows = self.store.query(
            "SELECT DISTINCT user_id FROM batch_counter WHERE count > 0 AND last_bump LIKE ?",
            (f"{day}%",),
        )
        return [r[0] for r in rows]

    def has_daily_settle_today(
        self, user_id: str, now: Callable[[], datetime] | None = None
    ) -> bool:
        """当日是否已对该用户执行过日终结算(judge_id 前缀 daily-YYYY-MM-DD)。"""

        now_fn = now or datetime.now
        day = now_fn().strftime("%Y-%m-%d")
        rows = self.store.query(
            """
            SELECT 1 FROM favorability_log
            WHERE user_id = ? AND judge_id LIKE ?
            LIMIT 1
            """,
            (user_id, f"daily-{day}%"),
        )
        return bool(rows)


"""好感度结算执行器与块渲染。"""

import json
import re
from datetime import datetime
from typing import Awaitable, Callable

from .llm_provider import build_side_prompt

LlMCall = Callable[[list[dict], str], Awaitable[dict]]


def parse_judge_response(text: str) -> dict | None:
    """从 LLM 文本提取判定 JSON,容忍 markdown 代码围栏。"""

    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace:
            cleaned = brace.group(0)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        # 合法 JSON 但非对象(如 "[]"/"42"/"\"str\"")同样视为解析失败,不得抛出
        return None
    if not isinstance(data.get("delta"), int) or not isinstance(data.get("note"), str):
        return None
    return {"delta": data["delta"], "note": data["note"]}


class SettleExecutor:
    """结算执行:材料构造 → 旁路 LLM 判定 → 落库/顺延/失败保持。"""

    def __init__(self, engine: BatchEngine, llm_call: LlMCall) -> None:
        self.engine = engine
        self.llm_call = llm_call

    async def settle(
        self,
        user_id: str,
        stream_id: str,
        history: list[dict],
        kind: str,
        model: str = "",
        persona: str = "",
    ) -> dict:
        """执行一次结算。kind: "early" 或 "daily";persona 为 bot 人设背景(结合角色性格判定关系变化)。"""

        material = self.engine.build_material(user_id, history)
        # 素材为空(取不到消息/窗口过滤后无目标用户消息):不调 LLM,不落库(审查 M2)
        if not material:
            return {"status": "failed", "error": "素材为空,跳过结算"}
        # 顺延口径 = 素材中目标用户本人的消息条数(审查 Minor#8,群聊邻居不计入)
        target_count = sum(1 for m in material if f"[{user_id}]" in m)
        if kind == "daily" and target_count < self.engine.config.daily_settle_min:
            return {"status": "carried_over", "reason": f"用户消息不足 {self.engine.config.daily_settle_min} 条,顺延"}
        # 稳定段 = 判定指令(system 模板)+ 5 级规则(配置,stable_ctx);变量尾 = 批次素材
        level_rules = self.engine.config.level_rules_list()
        stable_ctx = ([f"bot 人设:{persona}"] if persona.strip() else []) + level_rules
        messages, _cache_key = build_side_prompt(
            "favorability", stable_ctx, material,
            replacements={"delta_max": str(max(1, self.engine.config.delta_max))},
        )
        try:
            result = await self.llm_call(messages, model)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"LLM 调用异常: {exc}"}
        if not isinstance(result, dict) or not result.get("success"):
            detail = result.get("response", "")[:200] if isinstance(result, dict) else str(result)[:200]
            return {"status": "failed", "error": f"LLM 返回失败: {detail}"}
        parsed = parse_judge_response(str(result.get("response", "")))
        if parsed is None:
            return {"status": "failed", "error": "判定 JSON 解析失败"}
        delta_limit = max(1, self.engine.config.delta_max)
        delta = max(-delta_limit, min(delta_limit, parsed["delta"]))
        judged_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        judge_id = f"{kind}-{judged_at}"
        self.engine.apply_delta(
            user_id, delta, parsed["note"], judged_at=judged_at, judge_id=judge_id
        )
        self.engine.reset_batch(user_id, judged_at)
        return {"status": "ok", "delta": delta, "note": parsed["note"], "judge_id": judge_id}


def build_favorability_block(
    engine: BatchEngine, user_id: str, include_rule: bool = False
) -> str:
    """渲染好感度块文本(无记录=陌生,无注记)。

    include_rule=True 时按当前等级注入对应一条规则,置于块最前(联调决定:
    5 级全量注入改为按等级单条注入,保证缓存命中率与 token 经济性)。
    """

    row = engine.get_level(user_id)
    if row is None:
        level_name, score, note = "陌生", 0, ""
    else:
        level_name = LEVELS[row["level"]]
        score = row["score"]
        note = row["note"]
    lines: list[str] = []
    if include_rule:
        rule = engine.config.level_rule_by_name(level_name)
        if rule:
            lines.append(f"[好感度] 规则「{level_name}」:{rule}。")
    body = f"[好感度] {user_id}:等级「{level_name}」(累计 {score})"
    if note:
        body += f",注记:{note}" + ("" if note.rstrip()[-1] in "。.!！?？" else "。")
    else:
        body += "。"
    lines.append(body)
    return "\n".join(lines)
