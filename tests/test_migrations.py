"""数据迁移体系测试:版本表 + 步骤注册表 + 链式执行。"""

import logging

from catsitate_core import migrations
from catsitate_core.migrations import LATEST_DB_VERSION, _write_db_version, read_db_version, run_migrations
from catsitate_core.storage import SQLiteStore

_logger = logging.getLogger("catsitate.test.migrations")


def _prime_version(store: SQLiteStore, version: int) -> None:
    """测试辅助:预置版本表(模拟既有库的版本状态)。"""

    store.execute("CREATE TABLE IF NOT EXISTS _schema_version (version INTEGER NOT NULL)")
    _write_db_version(store, version)


def test_fresh_db_runs_baseline_step_to_v1(tmp_path):
    # 新库(无任何表):基线步骤把版本推到 1
    store = SQLiteStore(tmp_path / "fresh.db")
    assert read_db_version(store) == 0
    steps = run_migrations(store, _logger)
    assert steps == 1
    assert LATEST_DB_VERSION == 1
    assert read_db_version(store) == 1
    store.close()


def test_legacy_db_with_tables_migrates_to_v1(tmp_path):
    # 旧库(已有表但无版本行):跑完版本=1,既有数据不动
    store = SQLiteStore(tmp_path / "legacy.db")
    store.execute("CREATE TABLE IF NOT EXISTS memo (id INTEGER PRIMARY KEY, content TEXT)")
    store.execute("INSERT INTO memo (content) VALUES ('既有数据')")
    steps = run_migrations(store, _logger)
    assert steps == 1
    assert read_db_version(store) == 1
    assert store.query("SELECT content FROM memo") == [("既有数据",)]
    store.close()


def test_already_v1_skips_and_returns_zero(tmp_path):
    # 版本已是 1:跳过迁移,返回 0,版本保持 1
    store = SQLiteStore(tmp_path / "v1.db")
    _prime_version(store, 1)
    assert run_migrations(store, _logger) == 0
    assert read_db_version(store) == 1
    store.close()


def test_future_chain_executes_step_by_step(tmp_path, monkeypatch):
    # 多步链(模拟未来 v2 步骤):从 v0 起按注册表逐级执行,handler 副作用可见
    store = SQLiteStore(tmp_path / "chain.db")
    executed: list[str] = []

    def _baseline(s):
        executed.append("v0->v1")

    def _to_v2(s):
        executed.append("v1->v2")
        s.execute("CREATE TABLE IF NOT EXISTS v2_mark (k TEXT)")

    monkeypatch.setattr(migrations, "MIGRATION_STEPS", [
        (0, 1, "v1.0.0 基线:标记已有数据库版本", _baseline),
        (1, 2, "模拟 v2:演练多步链", _to_v2),
    ])
    monkeypatch.setattr(migrations, "LATEST_DB_VERSION", 2)
    steps = run_migrations(store, _logger)
    assert steps == 2
    assert read_db_version(store) == 2
    assert executed == ["v0->v1", "v1->v2"]
    assert store.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='v2_mark'"
    ) == [("v2_mark",)]
    store.close()


def test_failing_handler_warns_and_keeps_version(tmp_path, monkeypatch, caplog):
    # 单步失败:告警 + 版本不推进 + 返回已执行步数 0(不抛异常,不阻断加载)
    store = SQLiteStore(tmp_path / "fail.db")

    def _boom(s):
        raise RuntimeError("模拟迁移失败")

    monkeypatch.setattr(migrations, "MIGRATION_STEPS", [(0, 1, "会失败的步骤", _boom)])
    with caplog.at_level(logging.WARNING):
        steps = run_migrations(store, _logger)
    assert steps == 0
    assert read_db_version(store) == 0
    assert "迁移步骤失败" in caplog.text
    store.close()
