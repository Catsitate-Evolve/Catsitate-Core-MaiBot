"""日程引擎(二期 3.3):数据模型/校验/生成/执行判定/工具修改。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .favorability import LEVEL_INDEX
from .llm_provider import build_side_prompt

_ISO = "%Y-%m-%dT%H:%M"

DEFAULT_TEMPLATE_SCHEDULE: dict = {
    "date": "",
    "windows": [
        {"kind": "sleep", "start": "23:00", "end": "07:30", "activity": "", "plan_speak": False, "topic": ""},
        {"kind": "daily", "start": "09:00", "end": "12:00", "activity": "发呆", "plan_speak": False, "topic": ""},
        {"kind": "daily", "start": "15:00", "end": "18:00", "activity": "随便做点什么", "plan_speak": False, "topic": ""},
        {"kind": "greeting", "start": "22:00", "end": "23:00", "activity": "洗漱准备睡", "plan_speak": False, "topic": ""},
    ],
}


def _parse_t(w: dict) -> tuple[datetime, datetime]:
    return datetime.strptime(w["start"], _ISO), datetime.strptime(w["end"], _ISO)


def validate_schedule(data: dict, *, min_sleep: int, max_sleep: int) -> tuple[dict | None, str]:
    """校验日程结构:恰好 1 睡眠窗口、活动 1~8、不重叠、睡眠时长在 [min,max]、kind 合法。"""

    if not isinstance(data, dict):
        return None, "日程不是对象"
    windows = data.get("windows")
    if not isinstance(windows, list):
        return None, "windows 缺失"
    sleep_windows = [w for w in windows if isinstance(w, dict) and w.get("kind") == "sleep"]
    if len(sleep_windows) != 1:
        return None, f"睡眠窗口必须恰好 1 个(当前 {len(sleep_windows)})"
    acts = [w for w in windows if isinstance(w, dict) and w.get("kind") != "sleep"]
    if not (1 <= len(acts) <= 8):
        return None, f"活动窗口须 1~8 个(当前 {len(acts)})"
    spans: list[tuple[datetime, datetime]] = []
    for w in windows:
        try:
            s, e = _parse_t(w)
        except (KeyError, ValueError, TypeError):
            return None, f"窗口时间缺失或非法: {w}"
        if e <= s:
            return None, f"窗口结束须晚于开始: {w}"
        if w.get("kind") not in ("sleep", "daily", "greeting"):
            return None, f"非法 kind: {w.get('kind')}"
        for s2, e2 in spans:
            if s < e2 and s2 < e:
                return None, f"窗口重叠: {w}"
        spans.append((s, e))
    sleep = sleep_windows[0]
    s, e = _parse_t(sleep)
    duration = (e - s).total_seconds() / 60
    if duration < min_sleep:
        return None, f"睡眠不足最短 {min_sleep} 分钟"
    if duration > max_sleep:
        return None, f"睡眠超过最长 {max_sleep} 分钟"
    return data, ""


def fix_schedule(data: dict, *, min_sleep: int, max_sleep: int) -> dict:
    """确定性钳制修复(校验失败兜底):多余窗口裁到上限、重叠顺延、睡眠时长钳边界。"""

    windows = [w for w in (data.get("windows") or []) if isinstance(w, dict)]
    sleep = [w for w in windows if w.get("kind") == "sleep"]
    acts = [w for w in windows if w.get("kind") != "sleep"]
    keep = sleep[:1] + acts[:8] + sleep[1:]  # 恰好 1 睡眠 + 活动裁到 8
    # 睡眠时长钳制
    if sleep:
        s, e = _parse_t(sleep[0])
        dur = (e - s).total_seconds() / 60
        if dur < min_sleep:
            sleep[0]["end"] = (s + timedelta(minutes=min_sleep)).strftime(_ISO)
        elif dur > max_sleep:
            sleep[0]["end"] = (s + timedelta(minutes=max_sleep)).strftime(_ISO)
    # 重叠顺延:按开始排序,后窗起点 = max(自身起点, 前窗终点)
    keep.sort(key=lambda w: _parse_t(w)[0])
    prev_end: datetime | None = None
    for w in keep:
        s, e = _parse_t(w)
        if prev_end and s < prev_end:
            shift = prev_end - s
            w["start"] = prev_end.strftime(_ISO)
            w["end"] = (e + shift).strftime(_ISO)
            s, e = _parse_t(w)
        prev_end = e
    return {"date": data.get("date", ""), "windows": keep}


def schedule_from_json(text: str) -> tuple[dict | None, str]:
    """解析 LLM 日程 JSON(容忍 markdown 围栏/裸花括号)。"""

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
        return None, "日程 JSON 解析失败"
    return (data, "") if isinstance(data, dict) else (None, "日程非对象 JSON")


def current_window(data: dict, now_iso: str) -> dict | None:
    """当前所处窗口(空白时间返回 None=自由时间)。"""

    now = datetime.strptime(now_iso[:16], _ISO)
    for w in data.get("windows") or []:
        try:
            s, e = _parse_t(w)
        except (KeyError, ValueError, TypeError):
            continue
        if s <= now < e:
            return w
    return None


def next_window(data: dict, now_iso: str) -> dict | None:
    """下一个未开始的窗口。"""

    now = datetime.strptime(now_iso[:16], _ISO)
    upcoming = []
    for w in data.get("windows") or []:
        try:
            s, _ = _parse_t(w)
        except (KeyError, ValueError, TypeError):
            continue
        if s > now:
            upcoming.append((s, w))
    return min(upcoming, key=lambda x: x[0])[1] if upcoming else None


def _materialize_template(template: dict, date_str: str) -> dict:
    """默认作息模板补全日期(睡眠窗口 end 跨午夜 +1 天)。"""

    from datetime import datetime as _dt
    day = _dt.strptime(date_str, "%Y-%m-%d")
    out = {"date": date_str, "windows": []}
    for w in template["windows"]:
        w = dict(w)
        w["start"] = f"{date_str}T{w['start']}"
        w["end"] = f"{date_str}T{w['end']}"
        if w["end"] <= w["start"]:
            w["end"] = (day + timedelta(days=1)).strftime("%Y-%m-%d") + w["end"][10:]
        out["windows"].append(w)
    return out


def build_schedule_generate_prompt(
    persona: str, today_review: str, weather_text: str, fav_summary: str,
    due_memos: list[str], min_sleep: int, max_sleep: int, target_date: str,
) -> tuple[list[dict], str]:
    """日程生成 prompt:稳定段=system 模板+人设;变量尾=回顾/天气/好感度/备忘/约束/目标日。"""

    stable = [f"bot 人设:{persona}"] if persona.strip() else []
    tail = [
        f"今天回顾:{today_review or '无'}",
        f"明天天气/节日:{weather_text or '无数据'}",
        f"重要用户好感度:{fav_summary or '无'}",
        f"到期备忘:{'; '.join(due_memos) if due_memos else '无'}",
        f"睡眠约束:最短 {min_sleep} 分钟,最长 {max_sleep} 分钟",
        f"生成目标日:{target_date}",
    ]
    return build_side_prompt("schedule_generate", stable, tail)


class ScheduleGenerator:
    """日程生成:LLM 生成 → 校验 → 重生成(N 次)→ 钳制修复;LLM 失败用默认模板并返回显式错误。"""

    def __init__(self, llm_call, config_schedule, config_sleep) -> None:
        self.llm_call = llm_call
        self.cfg = config_schedule
        self.sleep_cfg = config_sleep

    async def generate(
        self, *, persona: str, today_review: str, weather_text: str,
        fav_summary: str, due_memos: list[str], target_date: str = "",
    ) -> tuple[dict, str]:
        if not target_date:
            target_date = datetime.now().strftime("%Y-%m-%d")
        messages, _ = build_schedule_generate_prompt(
            persona, today_review, weather_text, fav_summary, due_memos,
            self.sleep_cfg.min_sleep_minutes, self.sleep_cfg.max_sleep_minutes, target_date,
        )
        attempts = max(1, self.cfg.max_regenerate) + 1
        last_err = ""
        data = None
        for _ in range(attempts):
            try:
                result = await self.llm_call(messages, self.cfg.schedule_llm_model)
            except Exception as exc:  # noqa: BLE001
                return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date), f"日程生成 LLM 异常: {exc}"
            if not isinstance(result, dict) or not result.get("success"):
                return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date), f"日程生成 LLM 失败: {str(result)[:200]}"
            data, parse_err = schedule_from_json(str(result.get("response") or ""))
            if data is None:
                last_err = parse_err
                continue
            checked, verr = validate_schedule(data, min_sleep=self.sleep_cfg.min_sleep_minutes, max_sleep=self.sleep_cfg.max_sleep_minutes)
            if checked is not None:
                return checked, ""
            last_err = verr
        # 重生成耗尽 → 确定性钳制修复
        try:
            return fix_schedule(data if data is not None else _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date),
                                min_sleep=self.sleep_cfg.min_sleep_minutes, max_sleep=self.sleep_cfg.max_sleep_minutes), ""
        except Exception as exc:  # noqa: BLE001
            return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date), f"日程钳制修复异常: {exc}"


def threshold_met(level_name: str, threshold_level: str) -> bool:
    """等级名比较(陌生<熟悉<亲近<挚友<特别)。"""

    return LEVEL_INDEX.get(level_name, 0) >= LEVEL_INDEX.get(threshold_level, 99)


def build_proactive_intent(window: dict, stream: dict, day_overview: str) -> str:
    """主动发言指示 prompt(trigger 的 intent):日程事实 + 目标流好感度,话术交主程序。"""

    plan = "是" if window.get("plan_speak") else "否"
    topic = f",主题:{window.get('topic')}" if window.get("topic") else ""
    return (
        f"现在是你的日程「{window.get('activity') or '自由时间'}」时间(计划发言:{plan}{topic})。"
        f"全天概览:{day_overview}。"
        f"对方(流 {stream.get('stream_id')},用户 {stream.get('user_id')})与你的关系:等级「{stream.get('level_name', '陌生')}」"
        f",注记:{stream.get('note') or '无'}。"
        "请结合日程与你们的关系,自然决定是否主动发起聊天;想说话就用自己的方式说,不想说就保持沉默。"
    )


ACTIVITY_WINDOW_LIMIT = 8
_EDIT_LIMIT_REASON = "今天的日程已经排得满满当当了,再排下去会累坏的,明天再安排吧。"


def apply_schedule_edit(
    data: dict, action: str, window_index: int | None, new_window: dict | None,
    history: list[dict], *, min_sleep: int, max_sleep: int,
) -> tuple[dict, str, list[dict]]:
    """update_schedule 工具修改:add/update/delete;返回 (日程, 错误或"", 修改历史)。

    约束:活动窗口 1~8(超限返回拟人化拒绝文案);睡眠窗口不可删;时间修改
    受 [min_sleep, max_sleep] 校验;修改历史记录 {time, action, before, after}。
    """

    windows = [dict(w) for w in (data.get("windows") or [])]
    before = json.dumps(data, ensure_ascii=False)
    err = ""
    if action == "add":
        if not isinstance(new_window, dict) or new_window.get("kind") == "sleep":
            return data, "新增仅支持活动窗口", history
        if sum(1 for w in windows if w.get("kind") != "sleep") >= ACTIVITY_WINDOW_LIMIT:
            return data, _EDIT_LIMIT_REASON, history
        windows.append(dict(new_window))
    elif action == "update":
        if window_index is None or not (0 <= window_index < len(windows)) or not isinstance(new_window, dict):
            return data, "窗口序号非法", history
        if windows[window_index].get("kind") == "sleep" and new_window.get("kind") != "sleep":
            return data, "睡眠窗口不可变更为活动窗口", history
        windows[window_index] = dict(new_window)
    elif action == "delete":
        if window_index is None or not (0 <= window_index < len(windows)):
            return data, "窗口序号非法", history
        if windows[window_index].get("kind") == "sleep":
            return data, "睡眠窗口不可删除", history
        windows.pop(window_index)
    else:
        return data, f"未知操作: {action}", history
    candidate = {"date": data.get("date", ""), "windows": windows}
    checked, verr = validate_schedule(candidate, min_sleep=min_sleep, max_sleep=max_sleep)
    if checked is None:
        return data, verr, history
    history.append({
        "time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action, "before": before,
        "after": json.dumps(checked, ensure_ascii=False),
    })
    return checked, "", history
