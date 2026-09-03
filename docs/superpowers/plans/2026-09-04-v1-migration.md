# v1.0.0 数据迁移体系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为插件数据库建立版本号+迁移脚本体系(参照主程序模式),使 v0.3.2 及更早用户的数据可自动迁移到 v1.0.0,未来版本间也有标准迁移通道。

**Architecture:** `PRAGMA user_version` 存版本号(SQLite 原生,零额外表);迁移步骤注册表(列表,按 version_from→version_to 链);on_load 时 ensure_schema 先跑(补缺表/列)再跑迁移(做 ensure_schema 做不了的:列改名/类型变更/数据变换/清理)。

## v0.3.2 → v1.0.0 实际数据差异盘点

| 变更类型 | 内容 | ensure_schema 能处理? |
|---|---|---|
| 新表 | qzone_feeds / qzone_comments / qzone_fav_events / qzone_likes / llm_usage | ✅(CREATE IF NOT EXISTS) |
| 新列 | memo.extra_user_ids / favorability.window_start / qzone_feeds.message_id + author_nickname + interacted + injected_at / qzone_comments.retry_count + pending_retry + friend_uin | ✅(ALTER TABLE ADD COLUMN) |
| JSON 快照新增 | schedule.json / sleep_state.json / poke_cooldown.json / qzone_*.json | N/A(首次写入自动创建) |
| 删除字段(仅 schema) | config 中 comment_poll_interval_minutes / max_retries / speaker_lookup_hours / poke.enabled | N/A(配置非数据) |
| 数据变换 | 无(没有列改名/类型变更/行级重写) | N/A |

**结论**:v0.3.2→v1.0.0 无需数据迁移脚本——ensure_schema 自愈全部覆盖。迁移体系的价值在于**打上版本号基线**+为**未来版本**提供标准通道。

## Global Constraints

- 只改插件目录;简体中文;错误显式暴露;全量 561 绿不得回退。
- `PRAGMA user_version` 由 SQLite 原生管理,不用额外表。
- 迁移在 on_load 的 ensure_schema **之后**执行(先补缺再迁移)。
- 每步迁移事务性(单步失败回滚+告警,不阻断插件加载——数据不丢,下次重试)。

---

### Task 1: 迁移框架 + v1 基线标记

**Files:** 新 `catsitate_core/migrations.py`、`plugin.py`(on_load 调用)、`tests/test_migrations.py`

**要求:**
1. `migrations.py`:
   - `LATEST_DB_VERSION = 1`(v1.0.0 对应 DB 版本 1)
   - `MIGRATION_STEPS: list[tuple[int, int, str, Callable]]` 注册表:每项 (from_ver, to_ver, 描述, handler);handler 签名 `def handler(store: SQLiteStore) -> None`。当前只有一条:`(0, 1, "v1.0.0 基线:标记已有数据库版本", no_op)`。
   - `run_migrations(store: SQLiteStore, logger) -> int`:读 `PRAGMA user_version` → 若 >= LATEST 返回 0 → 按链逐条执行 handler → 事务内更新 `PRAGMA user_version` → 返回执行步数。失败单步告警+停止(不阻断加载)。
2. plugin.py on_load:在 ensure_schema 完成后调用 `run_migrations(self.store, self.ctx.logger)`;返回值 >0 时 info 日志「数据迁移完成: v{旧}→v{新}, {N} 步」。
3. 测试:①新库(无表)→跑完版本=1;②已有表但 user_version=0→跑完版本=1;③版本=1→跳过返回 0;④未来版本(模拟 v2 步骤)→按链执行;⑤handler 抛异常→告警+版本不变。
