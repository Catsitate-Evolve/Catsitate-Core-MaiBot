"""戳一戳引擎测试:主动戳前置校验/冷却。"""

from datetime import datetime, timedelta

from catsitate_core.config import PokeSection
from catsitate_core.poke import PokeEngine
from catsitate_core.storage import JsonSnapshot

NOW = datetime(2026, 8, 14, 12, 0, 0)


def make_engine(tmp_path):
    snapshot = JsonSnapshot(tmp_path / "poke_cooldown.json")
    engine = PokeEngine(snapshot, PokeSection())
    return engine


def test_can_poke_no_cooldown_record(tmp_path):
    engine = make_engine(tmp_path)
    ok, _ = engine.can_poke("u1", now=lambda: NOW)
    assert ok is True
    engine.mark_poked("u1", now=lambda: NOW)
    ok2, reason2 = engine.can_poke("u1", now=lambda: NOW)
    assert ok2 is False
    assert "冷却" in reason2


def test_can_poke_cooldown_expires(tmp_path):
    engine = make_engine(tmp_path)
    engine.mark_poked("u1", now=lambda: NOW)
    later = NOW + timedelta(seconds=601)
    assert engine.can_poke("u1", now=lambda: later)[0] is True
