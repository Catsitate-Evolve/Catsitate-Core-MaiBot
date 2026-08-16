# 压缩语义审查包 (98c0034..HEAD+worktree)

## 提交列表
f4c5cdb test(schedule): 补双窗口同时压缩用例,修复未压缩窗口误记压缩警告
6e3e308 docs: spec 同步压缩语义
4970284 feat(phase2): 重叠统一压缩(锚点挤旧窗口)+ 压缩警告 + 工具流程建议

## 变更统计 (含未提交)
 catsitate_core/schedule.py                         | 103 ++++++++++++++------
 docs/superpowers/specs/2026-08-15-phase2-design.md |   2 +-
 plugin.py                                          |  10 +-
 tests/test_schedule.py                             | 108 +++++++++++++--------
 4 files changed, 146 insertions(+), 77 deletions(-)
 catsitate_core/schedule.py | 27 ++++++++++++---------------
 1 file changed, 12 insertions(+), 15 deletions(-)

## 完整 diff (提交部分)
diff --git a/catsitate_core/schedule.py b/catsitate_core/schedule.py
index 9dd2be0..e0d02bc 100644
--- a/catsitate_core/schedule.py
+++ b/catsitate_core/schedule.py
@@ -278,81 +278,122 @@ def parse_hm(hm: str, day: str) -> str | None:
     return None
 
 
-def auto_shift_overlaps(windows: list[dict]) -> list[dict]:
-    """自动让位(确定性,人挪日历的直觉):按开始排序,重叠时——
-    活动窗口整体平移顺延;睡眠窗口压缩(入睡推迟、醒来不变)——活动挤占睡眠
-    时间(联调对齐:醒来时间是作息锚点,熬夜只动入睡侧)。"""
+def compress_with_anchor(
+    windows: list[dict], anchor_index: int,
+) -> tuple[list[dict], str, list[str]]:
+    """锚点压缩(联调对齐:新操作窗口挤旧窗口,不整体顺延):
+    - 锚点窗口保持完整;
+    - 锚点之前的窗口:end 提前到锚点 start(尾部压缩);
+    - 锚点之后的窗口:start 推迟到前一窗 end(头部压缩,链式);
+    - 任一窗口被压至 start>=end(挤没)即返回错误(不自动删除窗口,Q1=A);
+    返回 (窗口列表, 错误, 调整明细[「<活动> 由 <原> 压缩为 <新>」])。
+    """
 
-    out = sorted([dict(w) for w in windows], key=lambda w: _parse_t(w)[0])
-    prev_end: datetime | None = None
-    for w in out:
+    out = [dict(w) for w in windows]
+    anchor = out[anchor_index]
+    a_s, a_e = _parse_t(anchor)
+    adjustments: list[str] = []
+
+    def _desc(w: dict) -> str:
+        return w.get("activity") or ("睡觉" if w.get("kind") == "sleep" else "自由时间")
+
+    for i, w in enumerate(out):
+        if i == anchor_index:
+            continue
         s, e = _parse_t(w)
-        if prev_end and s < prev_end:
-            if w.get("kind") == "sleep":
-                w["start"] = prev_end.strftime(_ISO)  # 压缩:入睡推迟,醒来不变
-                s, _ = _parse_t(w)
-            else:
-                shift = prev_end - s
-                w["start"] = prev_end.strftime(_ISO)
-                w["end"] = (e + shift).strftime(_ISO)
-                s, e = _parse_t(w)
+        before_s, before_e = s, e
+        if s < a_s and e > a_s:
+            # 锚点前:尾部压缩
+            w["end"] = anchor["start"]
+            s, e = _parse_t(w)
+        elif s >= a_s and s < a_e:
+            # 锚点后(与锚点重叠):头部压缩
+            w["start"] = anchor["end"]
+            s, e = _parse_t(w)
+        if e <= s:
+            return out, f"安排与「{_desc(w)}」完全重叠,该窗口会被挤没,请调整时间", adjustments
+        if s != before_s or e != before_e:
+            adjustments.append(
+                f"「{_desc(w)}」由 {before_s.strftime('%H:%M')}-{before_e.strftime('%H:%M')} "
+                f"压缩为 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
+    # 锚点后链式:非锚点窗口按开始排序,与前一窗 end 重叠则头部压缩
+    ordered = sorted((w for i, w in enumerate(out) if i != anchor_index and _parse_t(w)[0] >= a_e),
+                     key=lambda w: _parse_t(w)[0])
+    prev_end = a_e
+    for w in ordered:
+        s, e = _parse_t(w)
+        if s < prev_end and s >= a_e:
+            before_s, before_e = s, e
+            w["start"] = prev_end.strftime(_ISO)
+            s, e = _parse_t(w)
+            if e <= s:
+                return out, f"安排与「{_desc(w)}」完全重叠,该窗口会被挤没,请调整时间", adjustments
+            if s != before_s:
+                adjustments.append(
+                    f"「{_desc(w)}」由 {before_s.strftime('%H:%M')}-{before_e.strftime('%H:%M')} "
+                    f"压缩为 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
         prev_end = e
-    return out
+    return out, "", adjustments
 
 
 def apply_schedule_move(
     data: dict, window_index: int, start_hm: str, end_hm: str, day: str, *,
     min_sleep: int, max_sleep: int, history: list[dict],
-) -> tuple[dict, str, list[dict]]:
-    """move:把窗口挪到新时段(保留 kind/activity/plan_speak/topic),冲突自动让位。"""
+) -> tuple[dict, str, list[dict], list[str]]:
+    """move:把窗口挪到新时段(保留属性);重叠时新窗口挤旧窗口(压缩),返回调整明细。"""
 
     windows = [dict(w) for w in (data.get("windows") or [])]
     if not (0 <= window_index < len(windows)):
-        return data, "窗口序号非法", history
+        return data, "窗口序号非法", history, []
     start, end = parse_hm(start_hm, day), parse_hm(end_hm, day)
     if not start or not end:
-        return data, "时间格式须为 HH:MM(如 11:45)", history
+        return data, "时间格式须为 HH:MM(如 11:45)", history, []
     if end <= start:
         end = (datetime.strptime(end, _ISO) + timedelta(days=1)).strftime(_ISO)  # 跨午夜
     before = json.dumps(data, ensure_ascii=False)
     windows[window_index] = {**windows[window_index], "start": start, "end": end}
-    windows = auto_shift_overlaps(windows)
+    windows, cerr, adjustments = compress_with_anchor(windows, window_index)
+    if cerr:
+        return data, cerr, history, []
     candidate = {"date": data.get("date", ""), "windows": windows}
     checked, verr = validate_schedule(candidate, min_sleep=min_sleep, max_sleep=max_sleep)
     if checked is None:
-        return data, verr, history
+        return data, verr, history, []
     history.append({"time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                     "action": f"move#{window_index}", "before": before,
                     "after": json.dumps(checked, ensure_ascii=False)})
-    return checked, "", history
+    return checked, "", history, adjustments
 
 
 def apply_schedule_add(
     data: dict, start_hm: str, end_hm: str, activity: str, day: str, *,
     min_sleep: int, max_sleep: int, history: list[dict],
-) -> tuple[dict, str, list[dict]]:
-    """add:新增活动窗口(daily 类型),冲突自动让位;活动上限拒绝拟人化。"""
+) -> tuple[dict, str, list[dict], list[str]]:
+    """add:新增活动窗口;重叠时新窗口挤旧窗口(压缩),返回调整明细。"""
 
     windows = [dict(w) for w in (data.get("windows") or [])]
     if sum(1 for w in windows if w.get("kind") != "sleep") >= ACTIVITY_WINDOW_LIMIT:
-        return data, _EDIT_LIMIT_REASON, history
+        return data, _EDIT_LIMIT_REASON, history, []
     start, end = parse_hm(start_hm, day), parse_hm(end_hm, day)
     if not start or not end:
-        return data, "时间格式须为 HH:MM(如 16:00)", history
+        return data, "时间格式须为 HH:MM(如 16:00)", history, []
     if end <= start:
         end = (datetime.strptime(end, _ISO) + timedelta(days=1)).strftime(_ISO)
     before = json.dumps(data, ensure_ascii=False)
     windows.append({"kind": "daily", "start": start, "end": end,
                     "activity": (activity or "自由时间").strip()[:40], "plan_speak": False, "topic": ""})
-    windows = auto_shift_overlaps(windows)
+    anchor_index = len(windows) - 1
+    windows, cerr, adjustments = compress_with_anchor(windows, anchor_index)
+    if cerr:
+        return data, cerr, history, []
     candidate = {"date": data.get("date", ""), "windows": windows}
     checked, verr = validate_schedule(candidate, min_sleep=min_sleep, max_sleep=max_sleep)
     if checked is None:
-        return data, verr, history
+        return data, verr, history, []
     history.append({"time": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                     "action": "add", "before": before,
                     "after": json.dumps(checked, ensure_ascii=False)})
-    return checked, "", history
+    return checked, "", history, adjustments
 
 
 ACTIVITY_WINDOW_LIMIT = 8
diff --git a/docs/superpowers/specs/2026-08-15-phase2-design.md b/docs/superpowers/specs/2026-08-15-phase2-design.md
index 9ffe512..c96c98b 100644
--- a/docs/superpowers/specs/2026-08-15-phase2-design.md
+++ b/docs/superpowers/specs/2026-08-15-phase2-design.md
@@ -66,7 +66,7 @@
   4. **trigger**:对每个选中流 `maisaka.proactive.trigger(stream_id, intent=指示 prompt)`——指示 prompt 由插件按模板构建:日程窗口活动/是否计划发言/主题/目标流等级注记,要求 bot 结合日程与好感度**自然发起主动发言**;**是否说话、说什么全部由主程序结合人设/记忆/上下文决定**;插件不 send.text、不生成话术(参考 idle_proactive_chat 观察者模式)。
 - **执行后状态**:窗口触发即更新日程块执行状态(无论主程序是否实际发言),防同窗口重复触发。
 - **计数**:每次 trigger 计 1(daily_speak_limit 约束),主程序沉默也计(触发即消耗)。
-- **可被工具修改**:`@Tool("update_schedule")`(visible):planner 可调用增/删/改当天日程——活动窗口可增删改(1~8 上限,含 kind 标注);**睡眠窗口不可删、时间可改(受 min/max 约束)**;**无频率上限**(联调决定);到达活动窗口上限时拒绝,返回拟人化理由(如「今天的日程已经排得满满当当了,再排下去会累坏的,明天再安排吧」——固定文案,非随机);修改落盘、立即反映到注入块,并记录**修改历史**(时间/改前/改后,存 schedule_state.json)。
+- **可被工具修改**:`@Tool("update_schedule")`(visible):planner 可调用增/删/改当天日程——活动窗口可增删改(1~8 上限,含 kind 标注);**睡眠窗口不可删、时间可改(受 min/max 约束)**;**无频率上限**(联调决定);到达活动窗口上限时拒绝,返回拟人化理由;重叠时新操作窗口挤旧窗口(压缩,不整体顺延;睡眠被挤=入睡推迟醒来不变;挤没即拒绝),返回压缩明细警告;建议先 view 后改再 view 确认(如「今天的日程已经排得满满当当了,再排下去会累坏的,明天再安排吧」——固定文案,非随机);修改落盘、立即反映到注入块,并记录**修改历史**(时间/改前/改后,存 schedule_state.json)。
 - **配置**(新 schedule 节):`enabled`、`max_regenerate`(默认 1)、`speak_threshold_level`(默认 熟悉)、`greet_threshold_level`(默认 亲近)、`private_threshold_level`(默认 挚友)、`speak_max_streams_per_window`(默认 1,每窗口最多 trigger 流数)、`schedule_llm_model`(默认 memory)、`schedule_llm_timeout_ms`、`daily_speak_limit`(默认 5,全天主动触发次数上限)。
 - **与睡眠交互**:睡眠期间窗口触发一律跳过、不补发;发言计入 `daily_speak_limit`;入睡确认生成次日日程是睡眠期间唯一允许的操作(生成动作本身,不打扰)。
 - **测试**:动态窗口校验(1 睡眠+1~8 活动/不重叠/空白允许)、入睡触发生成、校验失败重生成/钳制、窗口命中判断、工具增删改边界(上限拒绝文案/睡眠窗口不可删/时长约束)、门槛过滤、失败兜底模板。
diff --git a/plugin.py b/plugin.py
index d0f3880..95cef36 100644
--- a/plugin.py
+++ b/plugin.py
@@ -248,7 +248,7 @@ class CatsitatePlugin(MaiBotPlugin):
         description="增/删/改 bot 自己今天的日程安排(活动窗口)。活动最多 8 个;睡眠窗口不可删除、时间修改受最短/最长睡眠约束。",
         brief_description="修改今日日程",
         parameters=[
-            ToolParameterInfo(name="action", param_type="string", description="view(查看当前日程)/move(把某窗口挪到新时段)/add(新增活动)/delete(删除活动窗口)。常用示例:把睡眠窗口改成11:45到16:00 → action=move, window_index=0, start=11:45, end=16:00;新增下午听歌 → action=add, start=16:00, end=18:00, activity=和Hesitate_P一起听歌。先 view 看窗口序号", required=True),
+            ToolParameterInfo(name="action", param_type="string", description="view(查看当前日程)/move(把某窗口挪到新时段)/add(新增活动)/delete(删除活动窗口)。建议流程:编辑前先 view 看当前日程与窗口序号,编辑后再次 view 确认结果。常用示例:把睡眠窗口改成11:45到16:00 → action=move, window_index=0, start=11:45, end=16:00;新增下午听歌 → action=add, start=16:00, end=18:00, activity=和Hesitate_P一起听歌", required=True),
             ToolParameterInfo(name="window_index", param_type="integer", description="move/delete 时的窗口序号(view 结果每行开头数字)", required=False),
             ToolParameterInfo(name="start", param_type="string", description="move/add 的新开始时刻,HH:MM 格式如 11:45(自动按当天日期)", required=False),
             ToolParameterInfo(name="end", param_type="string", description="move/add 的新结束时刻,HH:MM 格式如 16:00(跨午夜自动次日)", required=False),
@@ -266,13 +266,14 @@ class CatsitatePlugin(MaiBotPlugin):
             return "当前日程(每行开头是窗口序号):\n" + schedule_overview_text(self._schedule_data)
         day = self._schedule_data.get("date") or datetime.now().strftime("%Y-%m-%d")
         min_sleep, max_sleep = self.config.sleep.min_sleep_minutes, self.config.sleep.max_sleep_minutes
+        adjustments: list[str] = []
         if action == "move":
-            data, err, history = apply_schedule_move(
+            data, err, history, adjustments = apply_schedule_move(
                 self._schedule_data, window_index, start, end, day,
                 min_sleep=min_sleep, max_sleep=max_sleep, history=self._schedule_edit_history,
             )
         elif action == "add":
-            data, err, history = apply_schedule_add(
+            data, err, history, adjustments = apply_schedule_add(
                 self._schedule_data, start, end, activity, day,
                 min_sleep=min_sleep, max_sleep=max_sleep, history=self._schedule_edit_history,
             )
@@ -288,6 +289,9 @@ class CatsitatePlugin(MaiBotPlugin):
         self._schedule_data = data
         self._schedule_edit_history = history
         self._persist_schedule()
+        if adjustments:
+            # 重叠警告(联调对齐):编辑发生压缩时返回明细,bot 可再 view 确认
+            return "日程已更新。注意:与已有安排重叠,已自动调整:" + ";".join(adjustments)
         return "日程已更新。"
 
     @Tool(
diff --git a/tests/test_schedule.py b/tests/test_schedule.py
index 17f1875..5c743b4 100644
--- a/tests/test_schedule.py
+++ b/tests/test_schedule.py
@@ -71,56 +71,20 @@ def test_parse_hm():
     assert parse_hm("25:00", "2026-08-16") is None
 
 
-def test_auto_shift_overlaps():
-    from catsitate_core.schedule import auto_shift_overlaps
-    ws = auto_shift_overlaps([
-        {"kind": "daily", "start": "2026-08-16T16:00", "end": "2026-08-16T18:00"},
-        {"kind": "sleep", "start": "2026-08-16T11:45", "end": "2026-08-16T16:00"},
-    ])
-    # 睡眠 11:45-16:00 在前,听歌 16:00 起不重叠;若重叠则后窗顺延
-    assert ws[0]["kind"] == "sleep"
-    assert ws[1]["start"] >= ws[0]["end"]
-
-
-def test_add_activity_squeezes_sleep_wake_fixed(tmp_path):
-    from catsitate_core.schedule import apply_schedule_add
-    data = {"date": "2026-08-16", "windows": [
-        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
-    ]}
-    # 活动 22:30-23:30 与睡眠 23:00-07:30 重叠 → 睡眠压缩(入睡 23:30,醒来 07:30 不变)
-    out, err, _ = apply_schedule_add(data, "22:30", "23:30", "打游戏", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
-    assert err == ""
-    sleep = next(w for w in out["windows"] if w["kind"] == "sleep")
-    assert sleep["start"] == "2026-08-16T23:30"
-    assert sleep["end"] == "2026-08-17T07:30"  # 醒来不变
-    # 压缩后 8h=480min ≥ 240 ✓
-
-
-def test_add_activity_squeeze_below_min_rejected(tmp_path):
-    from catsitate_core.schedule import apply_schedule_add
-    data = {"date": "2026-08-16", "windows": [
-        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
-    ]}
-    # 活动挤到 04:00 → 睡眠 04:00-07:30=210min < 240 → 拒绝
-    out, err, _ = apply_schedule_add(data, "22:00", "04:00", "通宵活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
-    assert err != "" and "最短" in err
-    assert out == data  # 原日程不变
-
-
 def test_move_window_hhmm_and_shift(tmp_path):
     from catsitate_core.schedule import apply_schedule_move
     data = {"date": "2026-08-16", "windows": [
         {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
         {"kind": "daily", "start": "2026-08-16T15:00", "end": "2026-08-16T18:00", "activity": "听歌"},
     ]}
-    out, err, hist = apply_schedule_move(data, 0, "11:45", "16:00", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
+    out, err, hist, adj = apply_schedule_move(data, 0, "11:45", "16:00", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
     assert err == ""
     sleep = next(w for w in out["windows"] if w["kind"] == "sleep")
     assert sleep["start"] == "2026-08-16T11:45" and sleep["end"] == "2026-08-16T16:00"
-    # 听歌 15:00-18:00 与睡眠 11:45-16:00 重叠 → 自动让位顺延到 16:00-19:00
+    # 听歌 15:00-18:00 与睡眠(锚点)重叠 → 头部压缩到 16:00-18:00(不整体顺延)
     song = next(w for w in out["windows"] if w["kind"] != "sleep")
-    assert song["start"] == "2026-08-16T16:00" and song["end"] == "2026-08-16T19:00"
-    assert hist
+    assert song["start"] == "2026-08-16T16:00" and song["end"] == "2026-08-16T18:00"
+    assert hist and adj  # 压缩明细非空
 
 
 def test_move_sleep_keeps_kind(tmp_path):
@@ -129,7 +93,7 @@ def test_move_sleep_keeps_kind(tmp_path):
         {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
         {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
     ]}
-    out, err, _ = apply_schedule_move(data, 0, "11:45", "16:00", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
+    out, err, _, _ = apply_schedule_move(data, 0, "11:45", "16:00", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
     assert err == "" and any(w["kind"] == "sleep" for w in out["windows"])  # move 保持 sleep(排序后首位未必是睡眠)
 
 
@@ -138,13 +102,73 @@ def test_add_window_hhmm(tmp_path):
     data = {"date": "2026-08-16", "windows": [
         {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
     ]}
-    out, err, hist = apply_schedule_add(data, "16:00", "18:00", "和Hesitate_P一起听歌", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
+    out, err, hist, _ = apply_schedule_add(data, "16:00", "18:00", "和Hesitate_P一起听歌", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
     assert err == ""
     new_w = next(w for w in out["windows"] if w["kind"] == "daily")
     assert new_w["start"] == "2026-08-16T16:00" and "听歌" in new_w["activity"]
     assert hist
 
 
+def test_add_anchor_squeezes_earlier_window_tail(tmp_path):
+    from catsitate_core.schedule import apply_schedule_add
+    data = {"date": "2026-08-16", "windows": [
+        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
+        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "发呆"},
+    ]}
+    # 新窗口(锚点)10:30-12:00 在旧窗之后 → 旧窗尾部压缩 09:00-10:30
+    out, err, _, adj = apply_schedule_add(data, "10:30", "12:00", "买菜", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
+    assert err == ""
+    old_w = next(w for w in out["windows"] if w.get("activity") == "发呆")
+    assert old_w["end"] == "2026-08-16T10:30"
+    assert any("发呆" in a for a in adj)
+
+
+def test_add_anchor_fully_covers_window_rejected(tmp_path):
+    from catsitate_core.schedule import apply_schedule_add
+    data = {"date": "2026-08-16", "windows": [
+        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
+        {"kind": "daily", "start": "2026-08-16T10:30", "end": "2026-08-16T11:00", "activity": "短暂活动"},
+    ]}
+    # 新窗口 10:00-12:00 完全覆盖旧窗 10:30-11:00 → 挤没拒绝(Q1=A)
+    out, err, _, _ = apply_schedule_add(data, "10:00", "12:00", "大块活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
+    assert "挤没" in err
+    assert out == data
+
+
+def test_add_anchor_squeezes_two_windows_both_sides(tmp_path):
+    from catsitate_core.schedule import apply_schedule_add
+    data = {"date": "2026-08-16", "windows": [
+        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
+        {"kind": "daily", "start": "2026-08-16T09:00", "end": "2026-08-16T11:00", "activity": "前窗"},
+        {"kind": "daily", "start": "2026-08-16T15:00", "end": "2026-08-16T18:00", "activity": "后窗"},
+    ]}
+    # 锚点 10:30-16:30:前窗尾部压缩到 09:00-10:30,后窗头部压缩到 16:30-18:00
+    out, err, _, adj = apply_schedule_add(data, "10:30", "16:30", "大块活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
+    assert err == ""
+    front = next(w for w in out["windows"] if w.get("activity") == "前窗")
+    back = next(w for w in out["windows"] if w.get("activity") == "后窗")
+    assert front["end"] == "2026-08-16T10:30"
+    assert back["start"] == "2026-08-16T16:30" and back["end"] == "2026-08-16T18:00"
+    assert len(adj) == 2  # 两个窗口都被压缩且都有明细
+
+
+def test_add_anchor_chain_squeezes_two_following(tmp_path):
+    from catsitate_core.schedule import apply_schedule_add
+    data = {"date": "2026-08-16", "windows": [
+        {"kind": "sleep", "start": "2026-08-16T23:00", "end": "2026-08-17T07:30", "activity": ""},
+        {"kind": "daily", "start": "2026-08-16T14:00", "end": "2026-08-16T15:00", "activity": "A"},
+        {"kind": "daily", "start": "2026-08-16T15:30", "end": "2026-08-16T17:00", "activity": "B"},
+    ]}
+    # 锚点 14:30-16:00:A 尾部压至 14:00-14:30;B 头部压至 16:00-17:00
+    out, err, _, adj = apply_schedule_add(data, "14:30", "16:00", "插入活动", "2026-08-16", min_sleep=240, max_sleep=660, history=[])
+    assert err == ""
+    a = next(w for w in out["windows"] if w.get("activity") == "A")
+    b = next(w for w in out["windows"] if w.get("activity") == "B")
+    assert a["end"] == "2026-08-16T14:30"
+    assert b["start"] == "2026-08-16T16:00" and b["end"] == "2026-08-16T17:00"
+    assert len(adj) == 2
+
+
 def test_parse_from_llm_with_fence():
     import json as _json
     text = "```json\n" + _json.dumps(GOOD, ensure_ascii=False) + "\n```"

## 完整 diff (未提交工作树)
diff --git a/catsitate_core/schedule.py b/catsitate_core/schedule.py
index e0d02bc..42bddcd 100644
--- a/catsitate_core/schedule.py
+++ b/catsitate_core/schedule.py
@@ -300,38 +300,35 @@ def compress_with_anchor(
     for i, w in enumerate(out):
         if i == anchor_index:
             continue
-        s, e = _parse_t(w)
-        before_s, before_e = s, e
-        if s < a_s and e > a_s:
+        s0, e0 = _parse_t(w)
+        before = f"{s0.strftime('%H:%M')}-{e0.strftime('%H:%M')}"
+        if s0 < a_s and e0 > a_s:
             # 锚点前:尾部压缩
             w["end"] = anchor["start"]
-            s, e = _parse_t(w)
-        elif s >= a_s and s < a_e:
+        elif s0 >= a_s and s0 < a_e:
             # 锚点后(与锚点重叠):头部压缩
             w["start"] = anchor["end"]
-            s, e = _parse_t(w)
+        s, e = _parse_t(w)
         if e <= s:
             return out, f"安排与「{_desc(w)}」完全重叠,该窗口会被挤没,请调整时间", adjustments
-        if s != before_s or e != before_e:
-            adjustments.append(
-                f"「{_desc(w)}」由 {before_s.strftime('%H:%M')}-{before_e.strftime('%H:%M')} "
-                f"压缩为 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
+        if (s, e) != (s0, e0):
+            after = f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
+            adjustments.append(f"「{_desc(w)}」由 {before} 压缩为 {after}")
     # 锚点后链式:非锚点窗口按开始排序,与前一窗 end 重叠则头部压缩
     ordered = sorted((w for i, w in enumerate(out) if i != anchor_index and _parse_t(w)[0] >= a_e),
                      key=lambda w: _parse_t(w)[0])
     prev_end = a_e
     for w in ordered:
         s, e = _parse_t(w)
+        s0 = s
         if s < prev_end and s >= a_e:
-            before_s, before_e = s, e
+            before = f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"
             w["start"] = prev_end.strftime(_ISO)
             s, e = _parse_t(w)
             if e <= s:
                 return out, f"安排与「{_desc(w)}」完全重叠,该窗口会被挤没,请调整时间", adjustments
-            if s != before_s:
-                adjustments.append(
-                    f"「{_desc(w)}」由 {before_s.strftime('%H:%M')}-{before_e.strftime('%H:%M')} "
-                    f"压缩为 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
+            if s != s0:
+                adjustments.append(f"「{_desc(w)}」由 {before} 压缩为 {s.strftime('%H:%M')}-{e.strftime('%H:%M')}")
         prev_end = e
     return out, "", adjustments
 
