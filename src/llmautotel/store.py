"""受限权限 SQLite 存储，公开记录不包含模型密钥。"""

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llmautotel.models import AppSettings, CallRecord


class Store:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_dir.chmod(0o700)
        self.path = data_dir / "app.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, body TEXT NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        # DELETE journal avoids persistent WAL files containing old credentials.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA secure_delete=ON")
        return connection

    async def get_settings(self) -> AppSettings:
        def read() -> AppSettings:
            with self._connect() as connection:
                row = connection.execute("SELECT body FROM settings WHERE id = 1").fetchone()
            return AppSettings.model_validate_json(row[0]) if row else AppSettings()

        return await asyncio.to_thread(read)

    async def save_settings(self, settings: AppSettings) -> AppSettings:
        # Calls are serialized by the API lock, so omitted credentials cannot race.
        current = await self.get_settings()
        for stage in ("asr", "llm", "tts"):
            incoming = getattr(settings, stage)
            if "api_key" not in incoming.model_fields_set:
                incoming.api_key = getattr(current, stage).api_key

        def write() -> None:
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO settings (id, body) VALUES (1, ?)",
                    (json.dumps(settings.private(), ensure_ascii=False),),
                )

        await asyncio.to_thread(write)
        return settings

    async def save_call(self, call: CallRecord) -> None:
        def write() -> None:
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO calls (id, started_at, body) VALUES (?, ?, ?)",
                    (call.id, call.started_at, call.model_dump_json()),
                )

        await asyncio.to_thread(write)

    async def get_call(self, call_id: str) -> CallRecord | None:
        def read() -> CallRecord | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT body FROM calls WHERE id = ?", (call_id,)
                ).fetchone()
            return CallRecord.model_validate_json(row[0]) if row else None

        return await asyncio.to_thread(read)

    async def list_calls(self) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT body FROM calls ORDER BY started_at DESC"
                ).fetchall()
            records = [CallRecord.model_validate_json(row[0]) for row in rows]
            return [
                record.model_dump(mode="json", exclude={"transcript", "settings"})
                | {
                    "goal": record.settings["sales"]["goal"],
                    "message_count": len(record.transcript),
                }
                for record in records
            ]

        return await asyncio.to_thread(read)

    async def delete_call(self, call_id: str) -> bool:
        def delete() -> bool:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM calls WHERE id = ?", (call_id,))
                return cursor.rowcount > 0

        return await asyncio.to_thread(delete)

    async def finalize_unfinished_calls(self) -> None:
        """进程意外终止后，旧会话不能继续显示为通话中。"""

        def finalize() -> None:
            with self._connect() as connection:
                rows = connection.execute("SELECT id, body FROM calls").fetchall()
                for call_id, body in rows:
                    record = CallRecord.model_validate_json(body)
                    if record.ended_at is None:
                        record.status = "failed"
                        record.ended_at = datetime.now(UTC).isoformat()
                        record.end_reason = "server_restarted"
                        connection.execute(
                            "UPDATE calls SET body = ? WHERE id = ?",
                            (record.model_dump_json(), call_id),
                        )

        await asyncio.to_thread(finalize)
