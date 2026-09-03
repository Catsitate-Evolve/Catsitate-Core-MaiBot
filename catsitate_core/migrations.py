"""数据库迁移体系:版本表 + 步骤注册表 + 链式执行。

参照主程序迁移体系模式(按 from→to 步骤链逐步执行),版本号存
`_schema_version` 表(全参数绑定读写;PRAGMA user_version 不支持参数绑定,
拼接有安全扫描风险,改用表语义等价)。

时序:on_load 中各 store 的 ensure_schema 先跑(CREATE IF NOT EXISTS /
ALTER TABLE ADD COLUMN 自愈补缺),迁移链后跑——承担 ensure_schema 做不了的
列改名/类型变更/行级数据变换/清理。

纪律(错误显式暴露):
- 版本号只在 handler 成功后推进:单步失败→告警+停止,版本停留,下次启动
  重试(不阻断插件加载,数据不丢);
- handler 必须幂等:store 按语句自动提交,失败重跑会重新执行已提交语句。
"""

from __future__ import annotations

from typing import Callable

from catsitate_core.storage import SQLiteStore

# 当前代码对应的数据库版本(v1.0.0 = 1);未来版本在此递增并注册步骤
LATEST_DB_VERSION = 1


def _noop(store: SQLiteStore) -> None:
    """空操作:v1.0.0 无实际数据变换(ensure_schema 已自愈全部历史差异)。"""

    del store


# 迁移步骤注册表:(from_ver, to_ver, 描述, handler)——只增不改历史步骤
MIGRATION_STEPS: list[tuple[int, int, str, Callable[[SQLiteStore], None]]] = [
    (0, 1, "v1.0.0 基线:标记已有数据库版本", _noop),
]


def read_db_version(store: SQLiteStore) -> int:
    """读版本表(无行视为 0;启动摘要与测试共用)。"""

    store.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
    rows = store.query("SELECT version FROM _schema_version")
    return int(rows[0][0]) if rows else 0


def _write_db_version(store: SQLiteStore, version: int) -> None:
    """写版本表(参数绑定;版本号非法直接抛出)。"""

    if version < 0:
        raise ValueError(f"数据库版本不能为负数: {version}")
    store.execute("DELETE FROM _schema_version")
    store.execute("INSERT INTO _schema_version (version) VALUES (?)", (version,))


def run_migrations(store: SQLiteStore, logger) -> int:
    """按注册表把数据库版本推进到 LATEST_DB_VERSION。

    当前版本 >= LATEST 时直接返回 0;否则逐链执行(当前版本 v → 取
    from_ver == v 的步骤):handler 成功后写新版本号。返回本次实际执行步数;
    单步失败告警+停止(不阻断插件加载,版本停留待下次重试)。
    """

    version = read_db_version(store)
    steps_done = 0
    while version < LATEST_DB_VERSION:
        step = next((s for s in MIGRATION_STEPS if s[0] == version), None)
        if step is None:
            logger.warning(
                "迁移注册表缺少 v%d 起始的步骤(LATEST=v%d),停止迁移——请检查 MIGRATION_STEPS 链完整性",
                version, LATEST_DB_VERSION,
            )
            break
        from_ver, to_ver, desc, handler = step
        if to_ver <= from_ver:
            logger.warning(
                "迁移步骤 v%d→v%d 不推进版本(%s),停止迁移以防死循环", from_ver, to_ver, desc
            )
            break
        try:
            handler(store)
            _write_db_version(store, to_ver)
        except Exception as exc:  # noqa: BLE001 - 单步失败显式告警,不阻断插件加载
            logger.warning(
                "迁移步骤失败 v%d→v%d(%s):%s——版本保持 v%d,下次启动重试",
                from_ver, to_ver, desc, exc, from_ver,
            )
            break
        logger.info("迁移步骤完成: v%d→v%d(%s)", from_ver, to_ver, desc)
        version = to_ver
        steps_done += 1
    return steps_done
