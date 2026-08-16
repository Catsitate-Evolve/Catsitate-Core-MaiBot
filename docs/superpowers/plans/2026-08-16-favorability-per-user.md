# 好感度按人重构 + 特别等级独占 + 主动问候统一 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把好感度体系从「(用户, 流) 分流」重构为「按人(QQ 号)唯一标识」,给「特别」等级加独占约束,并把主动问候统一为「仅特别者 + 私聊通道 + greeting 窗口起点触发」。

**Architecture:** favorability 单行按人(favorability.window_start 作人级结算窗口);batch_counter 保留 (user, stream) 行仅作活跃度记录;结算/衰减/注入/门槛全部按人;apply_delta 统一入口做特别独占钳制;greeting 窗口触发改为单一路径(_greet_exclusive),删除 2.1 群流问候与每日一次限制。

**Tech Stack:** Python 3.13、SQLite(自管 catsitate.db)、maibot-plugin-sdk 2.7.1、pytest。

**Spec:** docs/superpowers/specs/2026-08-15-phase2-design.md(全局决策 #7/#8/#9 + §3.1/§3.3/§3.5 已同步本计划实现内容)

## Global Constraints

- 不得修改 MaiBot 主程序代码(本仓库是独立插件 git repo,直接提交 main)
- 错误必须显式暴露:禁止静默 fallback;失败路径写显式日志或返回显式错误
- 用户可见文本与代码注释用简体中文
- 禁止纯概率行为
- 时间格式 `%Y-%m-%dT%H:%M:%S`(窗口时间 `%Y-%m-%dT%H:%M` 保持不变)
- 等级制:陌生(0-9)/ 熟悉(10-29)/ 亲近(30-59)/ 挚友(60-99)/ 特别(≥100,LEVELS 下标 4)
- 开发期不做旧数据迁移:ensure_schema 检测旧形状直接重建表(裁定 Q1)
- 签名稳定性:插件内跨模块函数以本计划的签名(含位置参数)为准,不得另行发明
- 每次任务完成后运行 `python3 -m pytest tests/ -q` 全绿才提交

---

### Task 1: favorability 表按人重建 + apply_delta 特别独占钳制

**Files:**
- Modify: `catsitate_core/favorability.py`(ensure_schema / apply_delta / get_level / is_exclusive_holder)
- Test: `tests/test_favorability.py`(重写涉及流键的用例,新增独占用例)

**Interfaces:**
- Produces(后续任务依赖,签名以此为准):
  - `BatchEngine.ensure_schema() -> None`:建表按人;旧形状(PRAGMA table_info(favorability) 含 stream_id 列)→ DROP 三表重建
  - `BatchEngine.apply_delta(user_id: str, delta: int, note: str, judged_at: str, judge_id: str | None = None) -> str`:返回状态 `"ok"` 或 `"clamped_exclusive"`(升特别但位被占 → 钳 99 分/挚友)
  - `BatchEngine.get_level(user_id: str) -> dict | None`(单参数,删除 stream_id)
  - `BatchEngine.is_exclusive_holder(user_id: str) -> bool`:存在 level>=4 且 user_id != 该人
  - 删除 `get_best_level_for_user`(poke 调用点 Task 6 改用 get_level)
  - 常量 `EXCLUSIVE_LEVEL: int = 4`

- [ ] **Step 1: 写失败测试**

`tests/test_favorability.py` 新增/重写(用临时 SQLite 文件构造 BatchEngine,`engine.ensure_schema()` 后断言):

```python
def test_schema_per_user_and_rebuild(tmp_path):
    engine = make_engine(tmp_path)  # 沿用现有测试的构造方式
    engine.ensure_schema()
    engine.store.execute("INSERT INTO favorability (user_id, level, score, note, window_start, judged_at) "
                         "VALUES ('111', 4, 100, '', '', '')")
    engine.ensure_schema()  # 新形状幂等,不重建
    row = engine.get_level("111")
    assert row["score"] == 100
    # 旧形状重建:手工插入含 stream_id 的旧表后再次 ensure_schema
    engine.store.execute("DROP TABLE favorability")
    engine.store.execute("CREATE TABLE favorability (user_id TEXT, stream_id TEXT, level INTEGER, score INTEGER, "
                         "note TEXT, window_start TEXT, judged_at TEXT, PRIMARY KEY (user_id, stream_id))")
    engine.ensure_schema()
    assert engine.get_level("111") is None  # 旧表已重建,数据清空(开发期裁定)

def test_exclusive_clamp(tmp_path):
    engine = make_engine(tmp_path)
    engine.ensure_schema()
    engine.apply_delta("A", 100, "唯一", "2026-08-16T12:00:00", judge_id="t1")
    assert engine.get_level("A")["level"] == 4
    status = engine.apply_delta("B", 100, "也想上位", "2026-08-16T12:01:00", judge_id="t2")
    assert status == "clamped_exclusive"
    b = engine.get_level("B")
    assert b["score"] == 99 and b["level"] == 3
    # 独占者本人继续加分不受限
    assert engine.apply_delta("A", 5, "更近一步", "2026-08-16T12:02:00", judge_id="t3") == "ok"
    # 独占者掉出后他人可升
    assert engine.apply_delta("A", -10, "降温", "2026-08-16T12:03:00", judge_id="t4") == "ok"
    assert engine.get_level("A")["level"] == 3
    assert engine.apply_delta("B", 1, "补位", "2026-08-16T12:04:00", judge_id="t5") == "ok"
    assert engine.get_level("B")["level"] == 4
```

- [ ] **Step 2: 运行确认失败**

```bash
python3 -m pytest tests/test_favorability.py -q
```
预期:AttributeError/KeyError(旧签名)与新用例 FAIL。

- [ ] **Step 3: 实现**

`catsitate_core/favorability.py`:

```python
EXCLUSIVE_LEVEL: int = 4  # LEVELS 下标:「特别」

def ensure_schema(self) -> None:
    # 开发期裁定:检测旧形状(含 stream_id 列)直接重建,不做数据迁移
    cols = {r[1] for r in self.store.query("PRAGMA table_info(favorability)")}
    if "stream_id" in cols:
        self.store.execute("DROP TABLE favorability")
        self.store.execute("DROP TABLE favorability_log")
        self.store.execute("DROP TABLE batch_counter")
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
            PRIMARY KEY (user_id, stream_id)
        )
        """
    )

def is_exclusive_holder(self, user_id: str) -> bool:
    """「特别」之位是否被他人占据(全表最多 1 人,规格全局决策 #8)。"""

    rows = self.store.query(
        "SELECT 1 FROM favorability WHERE level >= ? AND user_id != ? LIMIT 1",
        (EXCLUSIVE_LEVEL, user_id),
    )
    return bool(rows)

def apply_delta(self, user_id, delta, note, judged_at, judge_id=None) -> str:
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
            level = excluded.level, score = excluded.score, note = excluded.note,
            window_start = excluded.window_start, judged_at = excluded.judged_at
        """,
        (user_id, level, score, trimmed_note, current, current),
    )
    self.store.execute(
        "INSERT OR IGNORE INTO favorability_log (judge_id, user_id, delta, note, judged_at) VALUES (?, ?, ?, ?, ?)",
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
    return {"user_id": r[0], "level": r[1], "score": r[2], "note": r[3],
            "window_start": r[4], "judged_at": r[5]}
```

同步删除 `get_best_level_for_user`。`apply_delta` 的 judge_id 默认前缀逻辑不变(early-)。

- [ ] **Step 4: 运行确认通过**

```bash
python3 -m pytest tests/test_favorability.py -q
```
注意:同文件既有用例中所有 `get_level/apply_delta(…, stream_id, …)` 旧签名调用点全部按新签名改写(含 `test_apply_delta_upsert`、`test_get_level_missing` 等),断言按人语义更新。

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/favorability.py tests/test_favorability.py
git commit -m "refactor(favorability): 好感度表按人重建 + 特别等级独占钳制"
```

---

### Task 2: 批次/触发按人(活跃记录保留流维度)

**Files:**
- Modify: `catsitate_core/favorability.py`(count_message / check_trigger / reset_batch / iter_today_active / has_daily_settle_today)
- Test: `tests/test_favorability.py`

**Interfaces:**
- Consumes: Task 1 的 get_level/apply_delta
- Produces:
  - `count_message(user_id, stream_id, now=None) -> dict`:仅作活跃账本 bump(batch_counter (user, stream) 行 count+1/last_bump),返回 `{"messages": <该流计数>}`
  - `check_trigger(user_id, now=None) -> str | None`:`"early"` 或 None;总计数 = 该人跨流 SUM(count);early_settled_today 按人查 favorability_log(`judge_id LIKE 'early-%' AND user_id = ? AND judged_at LIKE 今天%`)
  - `reset_batch(user_id, judged_at) -> None`:该人所有流批次清零(不再写 window_start)
  - `iter_today_active(now=None) -> list[str]`:今天有消息的人(user_id 去重)
  - `has_daily_settle_today(user_id, now=None) -> bool`:log 按人查 `daily-今天%`

- [ ] **Step 1: 写失败测试**(按新签名调用即失败)

```python
def test_batch_per_user(tmp_path):
    engine = make_engine(tmp_path)
    engine.ensure_schema()
    now = datetime(2026, 8, 16, 12, 0, 0)
    engine.count_message("111", "s1", now=lambda: now)
    engine.count_message("111", "s2", now=lambda: now)
    engine.count_message("111", "s1", now=lambda: now)
    assert engine.check_trigger("111", now=lambda: now) is None  # 总数 3 < 阈值(默认 20)
    assert engine.iter_today_active(now=lambda: now) == ["111"]
    engine.apply_delta("111", 5, "测试", now.strftime(_ISO), judge_id=f"daily-{now.strftime('%Y-%m-%dT%H:%M:%S')}")
    assert engine.has_daily_settle_today("111", now=lambda: now)
    engine.reset_batch("111", now.strftime(_ISO))
    assert engine.check_trigger("111", now=lambda: now) is None  # 清零后归零
    assert engine.iter_today_active(now=lambda: now) == []  # count=0 不再活跃
```

- [ ] **Step 2: 运行确认失败**

```bash
python3 -m pytest tests/test_favorability.py::test_batch_per_user -q
```

- [ ] **Step 3: 实现**

`count_message` 保留 bump 逻辑(去掉 early_today 查询,返回仅 `{"messages": <该流计数>}`);`check_trigger`:

```python
def check_trigger(self, user_id: str, now: Callable[[], datetime] | None = None) -> str | None:
    now_fn = now or datetime.now
    current = now_fn()
    rows = self.store.query("SELECT SUM(count) FROM batch_counter WHERE user_id = ?", (user_id,))
    total = rows[0][0] or 0
    early_today = len(self.store.query(
        "SELECT 1 FROM favorability_log WHERE user_id = ? AND judge_id LIKE 'early-%' AND judged_at LIKE ?",
        (user_id, f"{current.strftime('%Y-%m-%d')}%"),
    ))
    if total >= self.config.early_settle_threshold and early_today < self.config.daily_max_early_settle:
        return "early"
    return None
```

`reset_batch(user_id, judged_at)`:`UPDATE batch_counter SET count = 0 WHERE user_id = ?`(judged_at 参数保留签名、实际不再写列)。`iter_today_active`:`SELECT DISTINCT user_id FROM batch_counter WHERE count > 0 AND last_bump LIKE ?` 返回单列列表。`has_daily_settle_today`:`WHERE user_id = ? AND judge_id LIKE ?`(`daily-{day}%`)。

- [ ] **Step 4: 运行确认通过**

```bash
python3 -m pytest tests/test_favorability.py -q
```

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/favorability.py tests/test_favorability.py
git commit -m "refactor(favorability): 批次与触发判定按人(batch_counter 仅作活跃账本)"
```

---

### Task 3: 结算素材跨流聚合(build_material 按人)

**Files:**
- Modify: `catsitate_core/favorability.py`(build_material)
- Test: `tests/test_favorability.py`

**Interfaces:**
- Consumes: Task 1 的 get_level(人级 window_start)
- Produces: `build_material(user_id: str, history: list[dict]) -> list[str]`,history 元素:
  `{role: "user"|"bot", user_id: str, stream_id: str, is_group: bool, addressed: bool | None, text: str, seq: int, ts: str}`
  - `is_group`/`addressed` 由 Task 6 的 `_fetch_recent_for_history` 生产;本任务只消费
  - 规则:窗口过滤(ts > favorability.window_start,按人)→ 全流混合按 (ts, seq) 排序 →
    锚点 = 该人自己的消息(任意流,取最后 material_max_messages 条)→ 每条锚点消息在其**所在流**内取前后各 1 条邻居 →
    bot 消息随附:私聊流全部 bot 消息;群聊流仅 `addressed=True`(quote/@ 了该人)的 bot 消息
  - 素材行格式不变:`[user_id](用户/群聊/私聊…) text`——私聊流消息标 `(私聊)`,群聊流标 `(群聊)`,便于 LLM 分辨语境

- [ ] **Step 1: 写失败测试**

```python
def test_material_aggregates_streams(tmp_path):
    engine = make_engine(tmp_path)
    engine.ensure_schema()
    engine.apply_delta("111", 10, "老朋友", "2026-08-16T08:00:00", judge_id="daily-1")
    history = [
        # 私聊流 p1:用户发言 + bot 回复(全部随附)
        {"role": "user", "user_id": "111", "stream_id": "p1", "is_group": False, "addressed": None,
         "text": "今天好吗", "seq": 1, "ts": "2026-08-16T09:00:00"},
        {"role": "bot", "user_id": "999", "stream_id": "p1", "is_group": False, "addressed": True,
         "text": "好呀", "seq": 2, "ts": "2026-08-16T09:00:05"},
        # 群聊流 g1:该人发言 + bot 未 quote 他(bot 消息不得随附)+ bot quote 他(随附)
        {"role": "user", "user_id": "111", "stream_id": "g1", "is_group": True, "addressed": None,
         "text": "群里的我", "seq": 3, "ts": "2026-08-16T09:01:00"},
        {"role": "bot", "user_id": "999", "stream_id": "g1", "is_group": True, "addressed": False,
         "text": "回应别人", "seq": 4, "ts": "2026-08-16T09:01:05"},
        {"role": "bot", "user_id": "999", "stream_id": "g1", "is_group": True, "addressed": True,
         "text": "回应你", "seq": 5, "ts": "2026-08-16T09:01:10"},
    ]
    material = engine.build_material("111", history)
    text = "\n".join(material)
    assert "今天好吗" in text and "好呀" in text      # 私聊全随附
    assert "群里的我" in text
    assert "回应别人" not in text                     # 群聊未 quote 该人的 bot 消息不随附
    assert "回应你" in text
    assert "(私聊)" in text and "(群聊)" in text
```

- [ ] **Step 2: 运行确认失败**

```bash
python3 -m pytest tests/test_favorability.py::test_material_aggregates_streams -q
```

- [ ] **Step 3: 实现**

```python
def build_material(self, user_id: str, history: list[dict]) -> list[str]:
    """结算素材(按人跨流聚合;规格全局决策 #7)。"""

    row = self.get_level(user_id)
    window_start = (row or {}).get("window_start") or ""
    fresh = [m for m in history if not window_start or m.get("ts", "") > window_start]
    fresh.sort(key=lambda m: (m.get("ts") or "", m.get("seq") or 0))
    target = [m for m in fresh if m["role"] == "user" and m["user_id"] == user_id]
    if not target:
        return []
    anchor = target[-self.config.material_max_messages:]
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
    for msg in fresh:  # bot 消息随附:私聊全收,群聊仅 quote/@ 该人
        if msg["role"] == "bot" and (not msg["is_group"] or msg.get("addressed")):
            selected[(msg["stream_id"], msg["seq"])] = msg
    material: list[str] = []
    for msg in sorted(selected.values(), key=lambda m: (m.get("ts") or "", m.get("seq") or 0)):
        ctx = "群聊" if msg["is_group"] else "私聊"
        text = msg["text"]
        if len(text) > self.config.material_message_max_chars:
            text = text[: self.config.material_message_max_chars] + "…"
        material.append(f"[{msg['user_id']}]({ctx}·{msg['role']}) {text}")
    return material
```

(素材行格式由 `(角色)` 改为 `(语境·角色)`,同步调整 Task 4 的 target_count 判定仍按 `f"[{user_id}]" in m` 不受影响;既有 build_material 用例按新格式更新。)

- [ ] **Step 4: 运行确认通过**

```bash
python3 -m pytest tests/test_favorability.py -q
```

- [ ] **Step 5: 提交**

```bash
git add catsitate_core/favorability.py tests/test_favorability.py
git commit -m "refactor(favorability): 结算素材跨流聚合(群聊仅随附 quote/@ 该人的 bot 消息)"
```

---

### Task 4: SettleExecutor 按人结算

**Files:**
- Modify: `catsitate_core/favorability.py`(SettleExecutor.settle)
- Test: `tests/test_favorability.py`、`tests/test_integration.py`(既有 settle 用例签名更新)

**Interfaces:**
- Consumes: Task 1 apply_delta 返回状态、Task 3 build_material
- Produces: `SettleExecutor.settle(user_id: str, history: list[dict], kind: str, model: str = "", persona: str = "") -> dict`,返回含 `"exclusive_clamped": bool`(status=="clamped_exclusive" 时 True)

- [ ] **Step 1: 写失败测试**

```python
def test_settle_per_user_and_clamp_status(tmp_path):
    engine = make_engine(tmp_path)
    engine.ensure_schema()
    engine.apply_delta("A", 100, "唯一", "2026-08-16T12:00:00", judge_id="t1")
    executor = SettleExecutor(engine, fake_llm(delta=3, note="更好了"))
    history = [{"role": "user", "user_id": "B", "stream_id": "p1", "is_group": False, "addressed": None,
                "text": "聊聊", "seq": 1, "ts": "2026-08-16T12:01:00"}]
    result = asyncio.run(executor.settle("B", history, "early"))
    assert result["status"] == "ok"
    assert result["exclusive_clamped"] is True   # B 想升特别被钳制
    assert engine.get_level("B")["score"] == 99
```

(fake_llm 沿用现有测试里的构造;settle 内部 `reset_batch` 断言按 Task 2 新签名。)

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**(signature/调用点按 Interfaces;`judge_id = f"{kind}-{judged_at}"`;`apply_delta(user_id, delta, note, judged_at, judge_id)`;`reset_batch(user_id, judged_at)`;`target_count` 统计不变)
- [ ] **Step 4: 运行确认通过**(全量 `python3 -m pytest tests/ -q`;test_integration 中 settle 相关签名全部按新接口更新)
- [ ] **Step 5: 提交**

```bash
git add catsitate_core/favorability.py tests/
git commit -m "refactor(favorability): SettleExecutor 按人结算,返回独占钳制状态"
```

---

### Task 5: 衰减按人(decay.py + plugin._daily_decay)

**Files:**
- Modify: `catsitate_core/decay.py`(scan_and_apply)、`plugin.py`(_daily_decay 候选构造)
- Test: `tests/test_decay.py`、`tests/test_integration.py`

**Interfaces:**
- Consumes: Task 1 apply_delta/get_level
- Produces: `DecayExecutor.scan_and_apply(candidates: list[tuple[str, str]], persona: str) -> list[dict]`(候选 = (user_id, interaction_ts),无 is_group);返回元素含 `"exclusive_clamped": bool`

- [ ] **Step 1: 写失败测试**(test_decay.py:候选元组去掉 is_group;`test_decay_applies_negative_delta` 等签名更新;新增:钳制状态透传用例——A 为特别,B 衰减 delta=0 不触发钳制,delta 无关,只透传状态即可,简化为断言调用不抛错)
- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**

decay.py 循环体:

```python
for user_id, interaction_ts in candidates:
    ...(现有逻辑,去 is_group)...
    status = self.engine.apply_delta(
        user_id, delta, parsed_note, judged_at=judged_at,
        judge_id=f"decay-{judged_at}-{user_id}",
    )
    results.append({"user_id": user_id, "delta": delta,
                    "exclusive_clamped": status == "clamped_exclusive"})
```

plugin.py `_daily_decay` 候选构造(替换本会话已做一半的流级行隔离):

```python
rows = self.store.query("SELECT DISTINCT user_id FROM favorability WHERE score > 0")
for (user_id,) in rows:
    row = self.fav_engine.get_level(user_id)
    if row is None or row["score"] <= 0:
        continue
    best: str = ""
    stream_rows = self.store.query("SELECT DISTINCT stream_id FROM batch_counter WHERE user_id = ?", (user_id,))
    for (stream_id,) in stream_rows:
        await self._refresh_stream_cache()
        if stream_id not in self._stream_cache:
            self.ctx.logger.warning("衰减候选流不存在(user=%s,stream=%s),跳过该流", user_id, stream_id)
            continue
        try:
            recent = await self._fetch_recent(stream_id, 50)
        except Exception:
            self.ctx.logger.warning("衰减候选取消息失败(user=%s,stream=%s),跳过该流", user_id, stream_id)
            continue
        t = last_bot_interaction_time(
            recent, user_id, str(self.config.favorability.bot_user_id or ""),
            stream_is_group=self._stream_is_group(stream_id),
        )
        if t and (not best or t > best):
            best = t
    decay_rows = self.store.query(
        "SELECT judged_at FROM favorability_log WHERE user_id = ? AND judge_id LIKE 'decay-%' "
        "ORDER BY judged_at DESC LIMIT 1", (user_id,),
    )
    decay_ts = decay_rows[0][0] if decay_rows else ""
    candidates.append((user_id, max(best or "", decay_ts)))
results = await self.decay.scan_and_apply(candidates, persona=await self._persona())
for r in results:
    self.ctx.logger.info("好感度衰减 %s:delta=%s", r["user_id"], r["delta"])
    if r.get("exclusive_clamped"):
        self.ctx.logger.warning("衰减升特别被独占钳制(user=%s)", r["user_id"])
```

注意:`_persona()` 签名沿用现有调用写法(以仓库现有为准,不要发明)。

- [ ] **Step 4: 运行确认通过**(全量 pytest)
- [ ] **Step 5: 提交**

```bash
git add catsitate_core/decay.py plugin.py tests/
git commit -m "refactor(decay): 衰减按人跨流取最近互动,流消亡/取消息失败逐行隔离"
```

---

### Task 6: plugin 接线(结算/注入/触发/问候统一) + 配置字段清理

**Files:**
- Modify: `plugin.py`(check_trigger 调用、_settle_and_log、_daily_settle、inject 块、_active_streams_over、_schedule_tick、_try_private_greet→_greet_exclusive、poke 调用点、统计查询、_fetch_recent_for_history)、`catsitate_core/config.py`(删 greet/private_threshold_level 字段)
- Modify: `config.toml`(删两行)、容器 `/MaiMBot/plugins/catsitate_core_maibot/config.toml`(部署时同步)
- Test: `tests/test_integration.py`、`tests/test_config.py`

**Interfaces:**
- Consumes: Task 1-5 全部新签名
- Produces(内部方法,以本签名为准):
  - `_settle_and_log(self, user_id: str, kind: str) -> None`
  - `_fetch_recent_for_history(self, stream_id: str, limit: int) -> list[dict]`,元素含 `is_group: bool` 与 `addressed: bool | None`(bot 消息才有意义;`message_info.user_info.user_id == bot_user_id` 判 role=bot;群聊 bot 消息 `addressed = user_id in reply_to 或 raw_message 存在 type=at 且 target_user_id == user_id`)
  - `_greet_exclusive(self, day: str, win: dict) -> bool`(主动问候;替代 _try_private_greet;`_greet_sent` 属性与引用全部删除)
  - `_active_streams_over(self, day: str) -> list[dict]`(去掉 threshold 参数,daily 窗口专用,门槛固定 `speak_threshold_level`)

- [ ] **Step 1: 改动清单(逐点)**

  1. 消息 hook(约 line 476):`trigger = self.fav_engine.check_trigger(user_id, stream_id)` → `check_trigger(user_id)`;`_spawn_background_task(self._settle_and_log(user_id, stream_id, kind="early"))` → `_settle_and_log(user_id, kind="early")`
  2. `_daily_settle`(约 line 869):`for user_id, stream_id in self.fav_engine.iter_today_active(): if self.fav_engine.has_daily_settle_today(user_id, stream_id)` → `for user_id in ...: if has_daily_settle_today(user_id)`;`_settle_and_log(user_id, kind="daily")`
  3. `_settle_and_log`:

```python
async def _settle_and_log(self, user_id: str, kind: str) -> None:
    """按人结算(规格全局决策 #7):聚合该人所有流的消息,一次 LLM 判定。"""

    streams = [r[0] for r in self.store.query("SELECT DISTINCT stream_id FROM batch_counter WHERE user_id = ?", (user_id,))]
    history: list[dict] = []
    for stream_id in streams:
        history.extend(await self._fetch_recent_for_history(stream_id, 50))
    result = await self.settle_executor.settle(
        user_id, history, kind, model=self.config.favorability.llm_model,
        persona=await self._persona(),
    )
    if result.get("status") == "ok":
        self.ctx.logger.info("好感度结算 %s:%s delta=%s", user_id, kind, result.get("delta"))
        if result.get("exclusive_clamped"):
            self.ctx.logger.warning("结算升特别被独占钳制(user=%s)", user_id)
    else:
        self.ctx.logger.warning("好感度结算失败 %s:%s %s", user_id, kind, result.get("error") or result.get("reason"))
```

  4. 注入块(约 line 714):`build_favorability_block(self.fav_engine, user_id, include_rule=…)`(原第三参 stream_id 删除)
  5. `_active_streams_over`:签名去 threshold;`row = self.fav_engine.get_level(user_id, stream_id)` → `get_level(user_id)`;`threshold_met(level_name, self.config.schedule.speak_threshold_level)`
  6. `_schedule_tick` greeting 分支:

```python
        if win.get("kind") == "greeting":
            await self._greet_exclusive(day, win)  # 主动问候:仅特别者+私聊通道,无 2.1 群流路径
            self._schedule_tick_fired[day] = mark
            return
        await self._window_trigger(day, win)
        self._schedule_tick_fired[day] = mark
```

  7. `_try_private_greet` 整体替换为:

```python
    async def _greet_exclusive(self, day: str, win: dict) -> bool:
        """主动问候(规格 §3.5):仅「特别」等级者 + 必须存在私聊流;greeting 窗口起点触发,无每日一次限制。"""

        if self._speak_counts.get(day, 0) >= self.config.schedule.daily_speak_limit:
            return False
        rows = self.store.query("SELECT user_id, note FROM favorability WHERE level >= 4 LIMIT 1")
        if not rows:
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
```

  8. poke 工具(约 line 346):`get_best_level_for_user` → `get_level`
  9. 统计汇总(约 line 1178):`SELECT user_id, MAX(level), MAX(score) FROM favorability GROUP BY user_id …` → `SELECT user_id, level, score, note FROM favorability ORDER BY level DESC, score DESC`(按人单行)
  10. `_fetch_recent_for_history`(约 line 1338):按 Interfaces 增 `is_group`/`addressed`,bot 消息判 role=bot(`message_info.user_info.user_id == config.favorability.bot_user_id`);`addressed` 判定:bot 消息 `reply_to` 含 user_id,或 `raw_message` 存在 `type=="at"` 且 `data.target_user_id == user_id`
  11. `catsitate_core/config.py`:删除 `ScheduleSection.greet_threshold_level` 与 `private_threshold_level` 字段与文档字符串;`config.toml` 删 `greet_threshold_level = "亲近"`、`private_threshold_level = "挚友"` 两行
  12. `_greet_sent` 属性、`_prune_day_keys` 中 `_greet_sent` 清理逻辑(如有)一并删除

- [ ] **Step 2: 写失败测试**

`tests/test_integration.py`(沿用现有 FakeCtx 构造):更新全部旧签名调用;新增:
- `_greet_exclusive`:无特别者 → 不 trigger;特别者无私聊流 → 不 trigger 且有日志;有私聊流 → trigger 且 speak_counts+1;达 daily_speak_limit → 不 trigger;连续两个 greeting 窗口都触发(无每日一次限制)
- `check_trigger`/`_settle_and_log` 按人调用不抛错

- [ ] **Step 3: 运行确认失败 → Step 4: 实现 → Step 5: 全量通过**

```bash
python3 -m pytest tests/ -q
```

- [ ] **Step 6: 提交**

```bash
git add plugin.py catsitate_core/config.py config.toml tests/
git commit -m "feat(greet): 主动问候统一(仅特别者+私聊通道) + 好感度按人接线 + 配置字段清理"
```

---

### Task 7: 部署 + 实机验收 + 文档收尾

**Files:**
- Modify: `docs/acceptance-checklist.md`(按人重构验收条目)
- 部署:容器 tar + restart;同步容器 config.toml(删两行)

- [ ] **Step 1: 部署**

```bash
tar -cf - plugin.py catsitate_core/favorability.py catsitate_core/decay.py config.toml tests/ | docker exec -i maim-bot-core tar -xf - -C /MaiMBot/plugins/catsitate_core_maibot
docker restart maim-bot-core
```

- [ ] **Step 2: 实机验收清单**

  1. 重启后 `ensure_schema` 重建表(旧数据清空,日志无异常);
  2. 提分一人到 100 → 该人为特别;再提分另一人到 100 → 钳制 99/挚友(查库 + 日志警告);
  3. greeting 窗口(挪到近期):特别者存在且无私聊流 → 不触发(日志「无私聊流」);有私聊流 → 触发 `主动问候触发[day] -> user`,QQ 收到主动私聊;
  4. daily 窗口:熟悉级用户被 trigger(2.1 回归);
  5. 日常聊天互动 → 按人结算日志出现一次(跨流合并);
  6. 衰减按人:未互动 7 天的用户衰减(实机等待或改 decay_after_days 临时验证)。

- [ ] **Step 3: 更新 `docs/acceptance-checklist.md`** 勾选/补记上述条目。

- [ ] **Step 4: 提交**

```bash
git add docs/acceptance-checklist.md
git commit -m "docs: 验收清单同步按人重构"
```

---

### Task 8: 全项目审查(设计冲突 + 可配置项一致性,SDD 最终审查范围)

**Files:**
- 审查对象:全仓库(重点 `plugin.py`、`catsitate_core/*.py`、`config.toml`、`prompt_templates/`、`docs/superpowers/specs/2026-08-15-phase2-design.md`、`docs/acceptance-checklist.md`)
- 由控制器派发独立审查子代理(most capable model),产审报告;发现 Critical/Important 逐条修复后 scoped re-review

**审查维度:**

1. **设计冲突**(spec 为权威,对照全文):
   - 特别独占(决策 #8)与衰减/结算/主动戳/注入块各路径是否有矛盾(如:特别者被钳制路径之外还能否绕过;独占者掉出后空位释放是否生效);
   - 主动问候(决策 #9)与 `daily_speak_limit`、`_schedule_tick` 窗口标记、睡眠静默的关系是否一致(睡眠期不问候、同窗口不重复触发);
   - 按人(决策 #7)后是否残留任何 (user, stream) 分流语义(旧签名、旧查询、旧注释);
   - 2.1 daily 窗口门槛(按人 `speak_threshold_level`)与注入块/trigger intent 中等级注记是否按人;
   - 备忘 remind(§3.4)、睡眠(§3.2)、日程(§3.3)与本轮改动无交互冲突。
2. **可配置项一致性**:
   - `config.toml` ↔ `catsitate_core/config.py` 字段一一对应(无孤儿字段、无缺失字段、默认值一致);
   - 已删除的 `greet_threshold_level`/`private_threshold_level` 在代码/注释/文档/模板中零残留;
   - 新增逻辑(特别独占、问候)是否引入了硬编码而非配置项——如应可配置需列出并裁定;
   - `_manifest.json` capabilities 与插件实际调用能力一致(本轮不新增能力调用)。
3. **规格/文档同步**:spec 与本计划实现一致;`docs/acceptance-checklist.md` 补记本轮验收项;`CONTEXT.md` 术语表如需更新。

- [ ] **Step 1**:控制器派发最终审查子代理(review package 覆盖 Task 1-7 全部 diff + 全仓 grep 检查),输出 Critical/Important/Minor
- [ ] **Step 2**:Critical/Important 逐条修复(单次 fix dispatch + scoped re-review,残留逐条裁定入 ledger)
- [ ] **Step 3**:更新验收清单与文档,提交

---

## Self-Review

1. **Spec coverage**:全局决策 #7(按人)→ Task 1/2/4/5/6;#8(特别独占)→ Task 1/4/5(透传钳制状态)+ Task 7 实机;#9(问候统一/无每日一次/不群内替代)→ Task 6;§3.1 衰减按人跨流 → Task 5;§3.3 greeting 无 2.1 路径 → Task 6;§3.5 测试(特别者无私聊流/多窗口触发)→ Task 6/7。✓
2. **Placeholder scan**:无 TBD/占位;关键代码全部给出。✓
3. **Type consistency**:`get_level(user_id)`/`apply_delta(→str)`/`reset_batch(user_id, judged_at)`/`iter_today_active()→list[str]`/`check_trigger(user_id)`/`settle(user_id, history, kind, …)`/`scan_and_apply([(user_id, ts)])` 跨 Task 1-6 一致;history 元素键(role/user_id/stream_id/is_group/addressed/text/seq/ts)Task 3 与 Task 6 一致。✓
