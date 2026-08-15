"""好感度 v3 批次结算制(规格 §4.3):纯计数触发、日终兜底、顺延不丢弃。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .config import FavorabilitySection
from .storage import SQLiteStore

LEVELS: list[str] = ["陌生", "熟悉", "亲近", "挚友", "特别"]
LEVEL_INDEX: dict[str, int] = {name: i for i, name in enumerate(LEVELS)}
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
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability (
                user_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                window_start TEXT NOT NULL,
                judged_at TEXT NOT NULL,
                PRIMARY KEY (user_id, stream_id)
            )
            """
        )
        self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS favorability_log (
                judge_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
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
        """记录一条用户消息,返回该批次统计。"""

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
        messages = rows[0][0]
        early_today = len(
            self.store.query(
                """
                SELECT 1 FROM favorability_log
                WHERE user_id = ? AND stream_id = ?
                  AND judge_id LIKE 'early-%' AND judged_at LIKE ?
                """,
                (user_id, stream_id, f"{current.strftime('%Y-%m-%d')}%"),
            )
        )
        return {
            "messages": messages,
            "reached_early_threshold": messages >= self.config.early_settle_threshold,
            "early_settled_today": early_today,
        }

    def check_trigger(
        self, user_id: str, stream_id: str, now: Callable[[], datetime] | None = None
    ) -> str | None:
        """返回触发类型 "early" 或 None(日终兜底/顺延在 Task 9 调度侧判定)。"""

        stat = self.count_message(user_id, stream_id, now=now)
        if (
            stat["reached_early_threshold"]
            and stat["early_settled_today"] < self.config.daily_max_early_settle
        ):
            return "early"
        return None

    def reset_batch(self, user_id: str, stream_id: str, judged_at: str) -> None:
        """结算后开新批次:计数清零、window_start 更新为新批次起点(judged_at)。"""

        self.store.execute(
            """
            UPDATE batch_counter SET count = 0, window_start = ?
            WHERE user_id = ? AND stream_id = ?
            """,
            (judged_at, user_id, stream_id),
        )

    def apply_delta(
        self,
        user_id: str,
        stream_id: str,
        delta: int,
        note: str,
        judged_at: str,
        judge_id: str | None = None,
    ) -> None:
        """结算结果落库:累加分数、重算等级、注记强制截断、写判定日志。

        judge_id: 判定日志幂等键;None 时默认 early-{judged_at}(日终结算须显式传 daily- 前缀)。
        """

        row = self.get_level(user_id, stream_id)
        score = (row["score"] if row else 0) + delta
        level = _level_for_score(score)
        trimmed_note = note.strip()[: self.config.note_max_chars]
        current = judged_at or datetime.now().strftime(_ISO)
        log_id = judge_id or f"early-{current}"
        self.store.execute(
            """
            INSERT INTO favorability (user_id, stream_id, level, score, note, window_start, judged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, stream_id) DO UPDATE SET
                level = excluded.level,
                score = excluded.score,
                note = excluded.note,
                window_start = excluded.window_start,
                judged_at = excluded.judged_at
            """,
            (user_id, stream_id, level, score, trimmed_note, current, current),
        )
        self.store.execute(
            """
            INSERT OR IGNORE INTO favorability_log (judge_id, user_id, stream_id, delta, note, judged_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (log_id, user_id, stream_id, delta, trimmed_note, current),
        )

    def get_level(self, user_id: str, stream_id: str) -> dict | None:
        rows = self.store.query(
            "SELECT user_id, stream_id, level, score, note, window_start, judged_at FROM favorability WHERE user_id = ? AND stream_id = ?",
            (user_id, stream_id),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "user_id": r[0], "stream_id": r[1], "level": r[2], "score": r[3],
            "note": r[4], "window_start": r[5], "judged_at": r[6],
        }

    def get_best_level_for_user(self, user_id: str) -> dict | None:
        """跨流取最高等级(主动戳工具门槛用)。"""

        rows = self.store.query(
            "SELECT user_id, stream_id, level, score, note, window_start, judged_at FROM favorability WHERE user_id = ? ORDER BY level DESC, score DESC LIMIT 1",
            (user_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "user_id": r[0], "stream_id": r[1], "level": r[2], "score": r[3],
            "note": r[4], "window_start": r[5], "judged_at": r[6],
        }

    def build_material(self, user_id: str, stream_id: str, history: list[dict]) -> list[str]:
        """构造结算素材(时间正序;群聊以目标用户消息为锚,bot 发言与紧邻上下文随附)。

        history 元素:{role: "user"|"bot", user_id: str, stream_id: str, text: str, seq: int, ts: str}
        只取 ts > 当前批次 window_start 的消息(规格"批次内",已结算旧消息不进入素材)。
        """

        rows = self.store.query(
            "SELECT window_start FROM batch_counter WHERE user_id = ? AND stream_id = ?",
            (user_id, stream_id),
        )
        window_start = rows[0][0] if rows else ""
        in_stream = [
            m for m in history
            if m["stream_id"] == stream_id and (not window_start or m.get("ts", "") > window_start)
        ]
        in_stream.sort(key=lambda m: m["seq"])
        target_msgs = [m for m in in_stream if m["role"] == "user" and m["user_id"] == user_id]
        if not target_msgs:
            return []
        anchor = target_msgs[-self.config.material_max_messages :]
        selected: dict[int, dict] = {}
        pos_by_seq = {m["seq"]: i for i, m in enumerate(in_stream)}
        for msg in anchor:
            selected[msg["seq"]] = msg
            # 紧邻上下文:同流前后各 1 条(群聊上下文判断 bot 是否回应 ta)
            pos = pos_by_seq[msg["seq"]]
            for neighbor in (in_stream[pos - 1], in_stream[pos + 1] if pos + 1 < len(in_stream) else None):
                if neighbor is not None:
                    selected[neighbor["seq"]] = neighbor
        # bot 在该流的发言随附(与锚点消息同批次窗口内)
        for msg in in_stream:
            if msg["role"] == "bot":
                selected[msg["seq"]] = msg
        material: list[str] = []
        for msg in sorted(selected.values(), key=lambda m: m["seq"]):
            role_label = "用户" if msg["role"] == "user" else "bot"
            text = msg["text"]
            if len(text) > self.config.material_message_max_chars:
                text = text[: self.config.material_message_max_chars] + "…"
            material.append(f"[{msg['user_id']}]({role_label}) {text}")
        return material
