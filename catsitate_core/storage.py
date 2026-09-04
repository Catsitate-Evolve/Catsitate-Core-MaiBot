"""存储层:sqlite3 薄封装 + JSON 快照(原子写)。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)


class SQLiteStore:
    """sqlite3 薄封装(插件 data 目录单库,WAL 模式)。"""

    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path: str = str(db_path)
        parent = Path(self.db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        # 库文件权限 0600(库内含 QQ 号/消息文本等隐私,仅属主可读——安全复审)
        os.chmod(self.db_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """执行写语句并提交;异常直接抛出(不静默)。"""

        with self._connect() as conn:
            conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """查询并返回元组列表。"""

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [tuple(row) for row in rows]

    def close(self) -> None:
        """无长连接,空实现保留接口。"""


class JsonSnapshot:
    """轻量 JSON 快照(冷却/限频状态),原子写入。"""

    def __init__(self, file_path: str | os.PathLike[str]) -> None:
        self.file_path: str = str(file_path)

    def load(self) -> dict[str, Any]:
        try:
            with open(self.file_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        # JSON 非法(ValueError 含 JSONDecodeError)与权限/磁盘类 OSError 都按空处理:
        # 损坏内容已忽略,下次 save 覆盖重建;必须显式告警而非静默吞掉或上抛炸穿读取方。
        except (OSError, ValueError) as exc:
            logger.warning(
                "快照文件读取失败,按空处理(损坏内容已忽略,下次 save 覆盖重建):%s,原因:%s",
                self.file_path,
                exc,
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "快照文件内容不是 JSON 对象,按空处理(损坏内容已忽略,下次 save 覆盖重建):%s",
                self.file_path,
            )
            return {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        parent = Path(self.file_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
