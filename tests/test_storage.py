"""存储层测试。"""

import json
import sqlite3

from catsitate_core.storage import JsonSnapshot, SQLiteStore


def test_sqlite_store_execute_and_query(tmp_path):
    store = SQLiteStore(tmp_path / "test.db")
    store.execute(
        "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)"
    )
    store.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("a", 1))
    store.execute("INSERT INTO t (name, n) VALUES (?, ?)", ("b", 2))
    rows = store.query("SELECT name, n FROM t ORDER BY id")
    assert rows == [("a", 1), ("b", 2)]
    store.close()


def test_sqlite_store_data_persists_across_instances(tmp_path):
    path = tmp_path / "p.db"
    s1 = SQLiteStore(path)
    s1.execute("CREATE TABLE t (v TEXT)")
    s1.execute("INSERT INTO t VALUES ('持久')")
    s1.close()
    s2 = SQLiteStore(path)
    assert s2.query("SELECT v FROM t") == [("持久",)]
    s2.close()


def test_sqlite_store_foreign_key_and_wal(tmp_path):
    store = SQLiteStore(tmp_path / "w.db")
    store.execute("PRAGMA journal_mode")
    rows = store.query("PRAGMA journal_mode")
    assert rows[0][0] == "wal"
    store.close()


def test_json_snapshot_roundtrip(tmp_path):
    snap = JsonSnapshot(tmp_path / "s.json")
    assert snap.load() == {}
    snap.save({"a": [1, 2], "b": "中文"})
    assert snap.load() == {"a": [1, 2], "b": "中文"}


def test_json_snapshot_atomic_replace(tmp_path):
    path = tmp_path / "s2.json"
    snap = JsonSnapshot(path)
    snap.save({"k": 1})
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": 1}
