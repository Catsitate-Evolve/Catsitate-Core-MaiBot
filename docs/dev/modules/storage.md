# 存储层(storage.py)

> 源码:`catsitate_core/storage.py`(92 行)。全文只有两个类:`SQLiteStore` 与 `JsonSnapshot`。

## 一、模块职责与生命周期

存储层回答一个问题:**插件的状态落在哪里**。全部持久化只走两条路:

- 结构化数据(备忘、好感度、空间去重表)进 **SQLite**——单库单文件,由 `SQLiteStore` 薄封装;
- 轻量状态(冷却、睡眠状态机、缓冲)进 **JSON 快照**——由 `JsonSnapshot` 原子读写。

存储层没有任何业务语义:不建表、不校验、不重试。建表(schema)由各使用方在自己的 `ensure_schema()` 里完成,SQL 一律由调用方传入——本模块只是"连接 + 执行 + 提交"与"读文件 + 原子写文件"两件事。

**生命周期**:无全局单例。`plugin.py` 的 `on_load` 里 `SQLiteStore(data_dir / "catsitate.db")` 建出唯一的库实例,再把它注入各服务(`MemoService`、`BatchEngine`、`SeenStore`……);各 `JsonSnapshot` 也在 `on_load` 按文件路径逐一实例化。`on_unload` 调 `store.close()`——它是空实现(见限制清单)。存储实例随插件加载/卸载而生灭,不跨重载共享。

数据目录来自 `ctx.paths.data_dir`(由插件 id `catsitate.core` 决定;生产容器为 `/data/plugins/catsitate.core/`),`on_load` 会先 `mkdir(parents=True, exist_ok=True)`。

## 二、完整逻辑

### SQLiteStore

**构造**(`__init__`):

1. `Path(db_path).parent.mkdir(parents=True, exist_ok=True)` 确保父目录存在;
2. 开一个连接执行 `PRAGMA journal_mode=WAL`——写不阻塞读,崩溃后自动恢复;
3. `os.chmod(db_path, 0o600)`——库内含 QQ 号、消息文本等隐私,收紧为仅属主可读写。

**连接策略**:`_connect()` 每次 `sqlite3.connect(self.db_path, check_same_thread=False)` 并设 `row_factory = sqlite3.Row`(列可按名取)。**每个操作新建连接,用完即弃,没有长连接**。

**两个操作方法**:

- `execute(sql, params)`——写路径。`with self._connect() as conn: conn.execute(...)`:`with` 块正常退出即提交事务,异常即回滚;异常**直接向外抛**,不捕获、不静默。
- `query(sql, params)`——读路径。`fetchall()` 后逐行 `tuple(row)` 返回 `list[tuple]`(调用方按位置下标取列)。
- `close()`——空实现,仅为保留接口对称。

事务粒度 = **单条语句**。调用方的多步操作(如 `ensure_schema` 的建表 + `ALTER TABLE` 迁移)是多次独立 `execute`,不是一个事务。

### JsonSnapshot

**`load() -> dict`**:

- `open` + `json.load`;`FileNotFoundError` → 返回 `{}`(文件未创建是正常首启状态,不算错误);
- 文件存在但 JSON 非法(`JSONDecodeError`,属 `ValueError`)或权限/磁盘类 `OSError` → **告警后返回 `{}`**(日志含文件路径与「损坏内容已忽略,下次 save 覆盖重建」)——快照都是低价值可再生状态,炸穿读取方(如冷却判定、入睡链)比丢一次状态危害大,但告警保证不静默;
- 顶层不是 dict(如被写成 list/str)→ 同样**告警后返回 `{}`**;
- 正常 dict 原样返回。

空安全的含义是"调用方拿到的永远是 dict,可以直接 `.get()`";坏文件按空处理但必有告警日志,不静默吞掉也不上抛。

**`save(data) -> None`**(原子写):

1. 父目录 `mkdir(parents=True, exist_ok=True)`;
2. `tempfile.mkstemp(dir=父目录, suffix=".tmp")` 在**同目录**(保证同文件系统)创建临时文件——`mkstemp` 新建文件天然 0600;
3. `os.fdopen(fd, "w", encoding="utf-8")` 写入 `json.dump(data, ensure_ascii=False, indent=2)`;
4. `os.replace(tmp_path, file_path)` 原子改名替换目标(读方永远看到完整旧文件或完整新文件,不存在半截 JSON);
5. 任一步异常:尝试 `unlink` 清理临时文件(清理失败忽略)后**重抛**。

### 使用方一览

**SQLite(全部落在同一个 `catsitate.db`,共 9 张表)**:

| 使用方 | 表 | 内容 |
|---|---|---|
| `plugin.py`(`on_load` 建表) | `llm_usage` | 旁路 LLM 按日/按模块的调用次数与 token 记账 |
| `plugin.py`(`on_load` 建表;`_refresh_environment` 写) | `weather_snapshot` | 最近一次天气数据(单行,`id=1`,UPSERT) |
| `memo.py` → `MemoService` | `memo` | 备忘条目(内容/流/人/TTL/提醒时刻/附带 QQ;含 `ensure_schema` 的列迁移) |
| `favorability.py` → `BatchEngine` | `favorability` / `favorability_log` / `batch_counter` | 等级分值 / 变更流水 / 计数触发状态 |
| `decay.py` → `DecayExecutor` | (复用 favorability 表) | 自然衰减的读取素材与回写 |
| `qzone/seen_store.py` → `SeenStore` | `qzone_feeds` | 动态去重(`state=queued/seen`,附注入时的 `message_id`) |
| `qzone/comment_seen.py` → `CommentSeenStore` | `qzone_comments` / `qzone_fav_events` | 评论去重 / 好感度显式事件 |
| `qzone/like_seen.py` → `LikeSeenStore` | `qzone_likes` | 赞事件去重(键 = 点赞者+说说) |

**JsonSnapshot(每个文件一个实例,共 8 个)**:

| 文件 | 使用方 | 内容 |
|---|---|---|
| `msg_react_cooldown.json` | `msg_react.py` | 贴表情每流冷却时间戳 |
| `poke_cooldown.json` | `poke.py` | 戳一戳每用户冷却时间戳 |
| `sleep_state.json` | `sleep.py` | 睡眠状态机(状态 + 入睡/醒来时刻;重启恢复) |
| `sleep_review_buffer.json` | `plugin.py` | 睡眠期被拦截消息的缓冲(醒后回顾素材;以 `{"messages": [...]}` 包装——`load` 只接受 dict) |
| `qzone_pending_diary.json` | `plugin.py` | 入睡时发布的日记正文(醒后回注虚拟流) |
| `qzone_digest.json` | `plugin.py` | 当日空间见闻摘要(日期 + 文本) |
| `qzone_cookies.json` | `qzone/client.py` → `CookieManager` | 空间 cookie(登录凭据;on_load 对存量文件补 `chmod 0600`) |
| `remind_fired.json` | `plugin.py` | 备忘提醒已触发标记(防重启后重复注入) |

选择规则经验上很清晰:**要按条件查询/聚合的数据进 SQLite**(备忘按人或流查、好感度按等级聚合、空间表按时间去重修剪),**只有"整体读、整体写"的小状态进 JSON**(冷却、状态机、缓冲)。

## 三、限制与回退清单

| 限制 | 为什么存在 | 触发条件 | 行为 |
|---|---|---|---|
| 无长连接,每操作新建连接 | 单文件低频读写,连接池的复杂度不划算;也顺带规避了跨任务共享连接的事务纠缠 | 每次 `execute` / `query` | 连接即建即弃,`close()` 为空实现,调用与否无差别 |
| 无事务批量 | 全部使用方都是单语句语义,不存在"多语句要么全成要么全不成"的需求 | 调用方连续多次 `execute`(如 `ensure_schema` 迁移) | 每条独立提交;中途失败会留下部分写入(表结构迁移是幂等的 `IF NOT EXISTS`/先查列,重跑即修复) |
| 单线程假设,无锁 | 所有存储调用都发生在插件进程的**同一个 asyncio 事件循环**里,单线程内天然串行,不需要锁 | 任何 `execute` / `query` / `save` | 直接执行;若未来引入线程池或多进程并发访问同一文件,`SQLiteStore` 不提供保护(`check_same_thread=False` 只是允许跨线程使用连接对象,不是并发安全) |
| 同步 IO 跑在事件循环内 | SQLite 与 JSON 的读写都是同步阻塞调用(async 函数里直接调);本地小数据量下阻塞为微秒级 | 高频钩子内的 `memo.read`、冷却判定等 | 可接受;若某表涨到需要索引调优或查询变慢,才需要考虑 `asyncio.to_thread` |
| `JsonSnapshot.load` 坏文件按空处理并告警 | 首启无文件是正常态;坏 JSON/非 dict 属于真实故障但快照均为低价值可再生状态,炸穿读取方危害更大 | 文件存在但内容损坏(JSON 非法/非 dict/读失败) | 告警(含路径与「下次 save 覆盖重建」)后返回 `{}`;调用方约定用 `{"key": ...}` 包装一切非 dict 数据 |
| `JsonSnapshot.save` 失败重抛 | 写不进盘(磁盘满、权限)必须让调用方知道 | 任何 IO 异常 | 清理临时文件后抛出原异常,调用方告警处理;目标文件保持旧内容不被破坏 |
| 库文件权限 0600 | 库内含 QQ 号、消息文本等隐私 | `SQLiteStore` 构造时一次性设置 | 后续新建的 WAL/SHM 附属文件由 SQLite 按库文件权限派生;凭据类 JSON(`qzone_cookies.json`)由 `mkstemp` 的 0600 天然保证,on_load 再对存量文件补收紧 |
| WAL 模式在构造时设置一次 | WAL 是库级持久属性 | 首次构造 | 之后所有连接自动继承;生产容器内删除 `catsitate.db` 后随下次构造重建 |
