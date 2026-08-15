"""日程引擎(二期 3.3):数据模型/校验/生成/执行判定/工具修改。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

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
