"""日程引擎:数据模型/校验/生成/执行判定/工具修改。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from .favorability import LEVEL_INDEX
from .llm_provider import build_side_prompt, rpc_error_brief

_ISO = "%Y-%m-%dT%H:%M"
_ISO_SEC = "%Y-%m-%dT%H:%M:%S"  # 容忍格式(仅解析用,落库一律归一化到 _ISO)

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
    """解析窗口起止时间;容忍 LLM 偶发带秒的格式(HH:MM:SS,实机 WARN「unconverted data remains: :00」)。"""

    out = []
    for v in (w["start"], w["end"]):
        try:
            out.append(datetime.strptime(v, _ISO))
        except ValueError:
            out.append(datetime.strptime(v, _ISO_SEC))
    return out[0], out[1]


def _normalize_window_times(windows: list[dict]) -> None:
    """窗口时间归一化到分钟精度(容忍秒后落库前统一,防 LLM 带秒污染后续解析)。"""

    for w in windows:
        try:
            s, e = _parse_t(w)
            w["start"] = s.strftime(_ISO)
            w["end"] = e.strftime(_ISO)
        except (KeyError, ValueError, TypeError):
            continue  # 非法时间交给 validate 报错,此处只做归一化


def sort_windows(windows: list[dict]) -> list[dict]:
    """按窗口开始时间排序(供 view 按时间顺序显示);解析失败的窗口排到末尾。"""

    def _key(w: dict) -> datetime:
        try:
            return _parse_t(w)[0]
        except (KeyError, ValueError, TypeError):
            return datetime.max
    return sorted(windows, key=_key)


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
        for attr in ("read_qzone", "send_qzone"):
            if w.get(attr) and w.get("kind") != "daily":
                return None, f"{attr} 属性仅允许 daily 窗口: {w}"
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
    """确定性钳制修复(校验失败兜底):恰好 1 睡眠窗口、活动裁到上限、重叠顺延、睡眠时长钳边界。

    0 个睡眠窗口 → 插入默认作息模板的睡眠段(按输入 date 补全时间);
    2+ 个睡眠窗口 → 只保留第一个,丢弃其余。
    """

    windows = [w for w in (data.get("windows") or []) if isinstance(w, dict)]
    sleep = [w for w in windows if w.get("kind") == "sleep"]
    acts = [w for w in windows if w.get("kind") != "sleep"]
    if not sleep:
        template = _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, data.get("date") or datetime.now().strftime("%Y-%m-%d"))
        default_sleep = next((w for w in template["windows"] if w.get("kind") == "sleep"), None)
        if default_sleep is not None:
            sleep = [default_sleep]
    keep = sleep[:1] + acts[:8]  # 恰好 1 睡眠 + 活动裁到 8
    for w in keep:  # 钳制:非 daily 窗口的读/发空间标记清除(含旧 qzone 键一并迁移清理,「校验一条」的兜底链)
        if w.get("kind") != "daily":
            for attr in ("qzone", "read_qzone", "send_qzone"):
                w.pop(attr, None)
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
    _normalize_window_times(keep)  # 秒格式归一到分钟(兜底链输出同样干净)
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


def schedule_overview_text(data: dict) -> str:
    """当前日程概览文本(update_schedule 工具 view/错误提示用):每窗口一行带序号。"""

    lines: list[str] = []
    for i, w in enumerate((data.get("windows") or []) if isinstance(data, dict) else []):
        if not isinstance(w, dict):
            continue
        kind_label = "睡眠" if w.get("kind") == "sleep" else ("问候" if w.get("kind") == "greeting" else "活动")
        time_range = f"{w.get('start', '?')[11:16]}-{w.get('end', '?')[11:16]}"
        activity = w.get("activity") or ("睡觉" if w.get("kind") == "sleep" else "自由时间")
        if w.get("read_qzone"):
            activity += "(刷空间)"
        if w.get("send_qzone"):
            activity += "(发说说)"
        lines.append(f"{i}. {kind_label} {time_range} {activity}")
    return "\n".join(lines) or "(空)"


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
    out["windows"] = sort_windows(out["windows"])
    return out


def build_schedule_generate_prompt(
    persona: str, behavior_style: str, today_review: str, weather_text: str, fav_summary: str,
    due_memos: list[str], min_sleep: int, max_sleep: int, target_date: str,
) -> tuple[list[dict], str]:
    """日程生成 prompt:稳定段=system 模板+人设+行为风格(顺序固定,保前缀缓存);变量尾=回顾/天气/好感度/备忘/约束/目标日。"""

    stable = ([f"bot 人设:{persona}"] if persona.strip() else []) + (
        [f"bot 行为风格:{behavior_style}"] if behavior_style.strip() else []
    )
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
        self, *, persona: str, behavior_style: str = "", today_review: str, weather_text: str,
        fav_summary: str, due_memos: list[str], target_date: str = "",
    ) -> tuple[dict, str]:
        if not target_date:
            target_date = datetime.now().strftime("%Y-%m-%d")
        messages, _ = build_schedule_generate_prompt(
            persona, behavior_style, today_review, weather_text, fav_summary, due_memos,
            self.sleep_cfg.min_sleep_minutes, self.sleep_cfg.max_sleep_minutes, target_date,
        )
        attempts = max(1, self.cfg.max_regenerate) + 1
        last_err = ""
        data = None
        for _ in range(attempts):
            try:
                result = await self.llm_call(messages, self.cfg.schedule_llm_model)
            except Exception as exc:  # noqa: BLE001
                # 仅记异常类型,不插值 exc 本体:LLM API 错误可能含请求体/PII
                return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date), f"日程生成 LLM 异常: {rpc_error_brief(exc)}"
            if not isinstance(result, dict) or not result.get("success"):
                # 不落响应原文:仅记失败形态
                if isinstance(result, dict):
                    detail = f"success={result.get('success')}"
                else:
                    detail = f"结果类型={type(result).__name__}"
                return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date), f"日程生成 LLM 失败({detail})"
            data, parse_err = schedule_from_json(str(result.get("response") or ""))
            if data is None:
                last_err = parse_err
                continue
            checked, verr = validate_schedule(data, min_sleep=self.sleep_cfg.min_sleep_minutes, max_sleep=self.sleep_cfg.max_sleep_minutes)
            if checked is not None:
                checked["windows"] = sort_windows(checked["windows"])
                _normalize_window_times(checked["windows"])
                return checked, ""
            last_err = verr
        # 重生成耗尽 → 确定性钳制修复;修复后仍无效 → 默认模板 + 显式错误(规格兜底链)
        try:
            fixed = fix_schedule(
                data if data is not None else _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date),
                min_sleep=self.sleep_cfg.min_sleep_minutes, max_sleep=self.sleep_cfg.max_sleep_minutes,
            )
            checked, verr = validate_schedule(fixed, min_sleep=self.sleep_cfg.min_sleep_minutes, max_sleep=self.sleep_cfg.max_sleep_minutes)
            if checked is not None:
                return checked, ""
            return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date), f"日程钳制修复后仍无效: {last_err or verr}"
        except Exception as exc:  # noqa: BLE001
            # 仅记异常类型,不插值 exc 本体:LLM API 错误可能含请求体/PII
            return _materialize_template(DEFAULT_TEMPLATE_SCHEDULE, target_date), f"日程钳制修复异常: {rpc_error_brief(exc)}"


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


def parse_hm(hm: str, day: str) -> str | None:
    """「HH:MM」宽松解析为当天 ISO;非法返回 None。"""

    t = (hm or "").strip()
    if len(t) == 5 and t[2] == ":":
        hh, mm = t[:2], t[3:]
        if hh.isdigit() and mm.isdigit() and int(hh) <= 23 and int(mm) <= 59:
            return f"{day}T{t}"
    return None


def compress_with_anchor(
    windows: list[dict], anchor_index: int,
) -> tuple[list[dict], str, list[str]]:
    """锚点压缩(新操作窗口挤旧窗口,不整体顺延):
    - 锚点窗口保持完整;
    - 锚点之前的窗口:end 提前到锚点 start(尾部压缩);窗口不可拆分,锚点后部分释放为自由时间;
    - 锚点之后的窗口:start 推迟到前一窗 end(头部压缩,链式);
    - 睡眠窗口特殊:与锚点重叠时入睡推迟到锚点 end、醒来时间不变;
    - 任一窗口被压至 start>=end(挤没)即返回错误(不自动删除窗口);
    返回 (窗口列表, 错误, 调整明细[「<活动> 由 <原> 压缩为 <新>」])。
    """

    if not (0 <= anchor_index < len(windows)):
        return [dict(w) for w in windows], "窗口序号非法", []
    out = [dict(w) for w in windows]
    anchor = out[anchor_index]
    a_s, a_e = _parse_t(anchor)
    adjustments: list[str] = []

    def _desc(w: dict) -> str:
        return w.get("activity") or ("睡觉" if w.get("kind") == "sleep" else "自由时间")

    for i, w in enumerate(out):
        if i == anchor_index:
            continue
        s0, e0 = _parse_t(w)
        before = f"{s0.strftime('%H:%M')}-{e0.strftime('%H:%M')}"
        if w.get("kind") == "sleep" and s0 < a_e and a_s < e0:
            # 睡眠与锚点重叠:入睡推迟到锚点 end,醒来时间不变
            w["start"] = anchor["end"]
        elif s0 < a_s and e0 > a_s:
            # 锚点前:尾部压缩
            w["end"] = anchor["start"]
        elif s0 >= a_s and s0 < a_e:
            # 锚点后(与锚点重叠):头部压缩
            w["start"] = anchor["end"]
        s, e = _parse_t(w)
        if e <= s:
            return out, f"安排与「{_desc(w)}」完全重叠,该窗口会被挤没,请调整时间", adjustments
        if (s, e) != (s0, e0):
            after = f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
            adjustments.append(f"「{_desc(w)}」由 {before} 压缩为 {after}")
    # 锚点后链式:非锚点窗口按开始排序,与前一窗 end 重叠则头部压缩
    ordered = sorted((w for i, w in enumerate(out) if i != anchor_index and _parse_t(w)[0] >= a_e),
                     key=lambda w: _parse_t(w)[0])
    prev_end = a_e
    for w in ordered:
        s, e = _parse_t(w)
        s0 = s
        if s < prev_end and s >= a_e:
            before = f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
            w["start"] = prev_end.strftime(_ISO)
            s, e = _parse_t(w)
            if e <= s:
                return out, f"安排与「{_desc(w)}」完全重叠,该窗口会被挤没,请调整时间", adjustments
            if s != s0:
                adjustments.append(f"「{_desc(w)}」由 {before} 压缩为 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
        prev_end = e
    return out, "", adjustments


def apply_schedule_move(
    data: dict, window_index: int, start_hm: str, end_hm: str, day: str, *,
    min_sleep: int, max_sleep: int, history: list[dict],
) -> tuple[dict, str, list[dict], list[str]]:
    """move:把窗口挪到新时段(保留属性);重叠时新窗口挤旧窗口(压缩),返回调整明细。"""

    windows = [dict(w) for w in (data.get("windows") or [])]
    if not (0 <= window_index < len(windows)):
        return data, "窗口序号非法", history, []
    start, end = parse_hm(start_hm, day), parse_hm(end_hm, day)
    if not start or not end:
        return data, "时间格式须为 HH:MM(如 11:45)", history, []
    if end <= start:
        end = (datetime.strptime(end, _ISO) + timedelta(days=1)).strftime(_ISO)  # 跨午夜
    before = json.dumps(data, ensure_ascii=False)
    windows[window_index] = {**windows[window_index], "start": start, "end": end}
    windows, cerr, adjustments = compress_with_anchor(windows, window_index)
    if cerr:
        return data, cerr, history, []
    candidate = {"date": data.get("date", ""), "windows": windows}
    checked, verr = validate_schedule(candidate, min_sleep=min_sleep, max_sleep=max_sleep)
    if checked is None:
        return data, verr, history, []
    checked["windows"] = sort_windows(checked["windows"])
    history.append({"time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "action": f"move#{window_index}", "before": before,
                    "after": json.dumps(checked, ensure_ascii=False)})
    return checked, "", history, adjustments


def apply_schedule_add(
    data: dict, start_hm: str, end_hm: str, activity: str, day: str, *,
    min_sleep: int, max_sleep: int, history: list[dict],
    read_qzone: bool = False, send_qzone: bool = False,
) -> tuple[dict, str, list[dict], list[str]]:
    """add:新增活动窗口;重叠时新窗口挤旧窗口(压缩),返回调整明细。

    read_qzone/send_qzone:QQ空间窗口标记(浏览/发布),仅在 True 时写入键——
    与日程生成器输出形态一致,消费方全部 .get() 判定,缺省即 False。"""

    windows = [dict(w) for w in (data.get("windows") or [])]
    if sum(1 for w in windows if w.get("kind") != "sleep") >= ACTIVITY_WINDOW_LIMIT:
        return data, _EDIT_LIMIT_REASON, history, []
    start, end = parse_hm(start_hm, day), parse_hm(end_hm, day)
    if not start or not end:
        return data, "时间格式须为 HH:MM(如 16:00)", history, []
    if end <= start:
        end = (datetime.strptime(end, _ISO) + timedelta(days=1)).strftime(_ISO)
    before = json.dumps(data, ensure_ascii=False)
    new_window = {"kind": "daily", "start": start, "end": end,
                  "activity": (activity or "自由时间").strip()[:40], "plan_speak": False, "topic": ""}
    if read_qzone:
        new_window["read_qzone"] = True
    if send_qzone:
        new_window["send_qzone"] = True
    windows.append(new_window)
    anchor_index = len(windows) - 1
    windows, cerr, adjustments = compress_with_anchor(windows, anchor_index)
    if cerr:
        return data, cerr, history, []
    candidate = {"date": data.get("date", ""), "windows": windows}
    checked, verr = validate_schedule(candidate, min_sleep=min_sleep, max_sleep=max_sleep)
    if checked is None:
        return data, verr, history, []
    checked["windows"] = sort_windows(checked["windows"])
    history.append({"time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "action": "add", "before": before,
                    "after": json.dumps(checked, ensure_ascii=False)})
    return checked, "", history, adjustments


ACTIVITY_WINDOW_LIMIT = 8
_EDIT_LIMIT_REASON = "今天的日程已经排得满满当当了,再排下去会累坏的,明天再安排吧。"


def apply_schedule_delete(
    data: dict, window_index: int, history: list[dict], *,
    min_sleep: int, max_sleep: int,
) -> tuple[dict, str, list[dict]]:
    """update_schedule 工具的 delete 专用实现;返回 (日程, 错误或"", 修改历史)。

    约束:睡眠窗口不可删;删除后整表校验(活动窗口仍须 1~8、不重叠);修改
    历史记录 {time, action, before, after}。新增/挪窗走 apply_schedule_add/
    apply_schedule_move(带锚点压缩),本函数不再承担(update_schedule 只路由
    view/move/add/delete 四动作,add/move 各有专用实现)。
    """

    windows = [dict(w) for w in (data.get("windows") or [])]
    before = json.dumps(data, ensure_ascii=False)
    if window_index is None or not (0 <= window_index < len(windows)):
        return data, "窗口序号非法", history
    if windows[window_index].get("kind") == "sleep":
        return data, "睡眠窗口不可删除", history
    windows.pop(window_index)
    candidate = {"date": data.get("date", ""), "windows": windows}
    checked, verr = validate_schedule(candidate, min_sleep=min_sleep, max_sleep=max_sleep)
    if checked is None:
        return data, verr, history
    checked["windows"] = sort_windows(checked["windows"])
    history.append({
        "time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "action": "delete", "before": before,
        "after": json.dumps(checked, ensure_ascii=False),
    })
    return checked, "", history
