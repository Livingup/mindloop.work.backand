from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from agent_runtime.file_resource.models import (
    FileResourceArchive,
    FileResourceMetadata,
    ImageResourceMetadata,
    PdfResourceMetadata,
)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS file_resource_archives (
    resource_id TEXT PRIMARY KEY,
    resource_content_hash TEXT NOT NULL UNIQUE,
    resource_category TEXT NOT NULL,
    resource_mime_type TEXT NOT NULL,
    resource_size_in_bytes INTEGER NOT NULL,
    local_storage_path TEXT NOT NULL,
    object_storage_url TEXT NOT NULL,
    resource_metadata_json TEXT NOT NULL,
    resource_created_at_iso8601 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_resource_archives_content_hash
    ON file_resource_archives(resource_content_hash);
"""


class SqliteFileResourceArchiveRepository:
    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir).expanduser()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._data_dir / "file_resource.db"),
            timeout=30,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_archive_by_id(self, resource_id: str) -> FileResourceArchive | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT resource_id,
                       resource_content_hash,
                       resource_category,
                       resource_mime_type,
                       resource_size_in_bytes,
                       local_storage_path,
                       object_storage_url,
                       resource_metadata_json,
                       resource_created_at_iso8601
                FROM file_resource_archives
                WHERE resource_id = ?
                """,
                (resource_id,),
            ).fetchone()
        return _row_to_archive(row) if row is not None else None

    def get_archive_by_content_hash(
        self,
        content_hash: str,
    ) -> FileResourceArchive | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT resource_id,
                       resource_content_hash,
                       resource_category,
                       resource_mime_type,
                       resource_size_in_bytes,
                       local_storage_path,
                       object_storage_url,
                       resource_metadata_json,
                       resource_created_at_iso8601
                FROM file_resource_archives
                WHERE resource_content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        return _row_to_archive(row) if row is not None else None

    def save_archive(self, archive: FileResourceArchive) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO file_resource_archives (
                    resource_id,
                    resource_content_hash,
                    resource_category,
                    resource_mime_type,
                    resource_size_in_bytes,
                    local_storage_path,
                    object_storage_url,
                    resource_metadata_json,
                    resource_created_at_iso8601
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(resource_id) DO UPDATE SET
                    resource_content_hash = excluded.resource_content_hash,
                    resource_category = excluded.resource_category,
                    resource_mime_type = excluded.resource_mime_type,
                    resource_size_in_bytes = excluded.resource_size_in_bytes,
                    local_storage_path = excluded.local_storage_path,
                    object_storage_url = excluded.object_storage_url,
                    resource_metadata_json = excluded.resource_metadata_json,
                    resource_created_at_iso8601 = excluded.resource_created_at_iso8601
                """,
                (
                    archive.resource_id,
                    archive.resource_content_hash,
                    archive.resource_category,
                    archive.resource_mime_type,
                    archive.resource_size_in_bytes,
                    archive.local_storage_path,
                    archive.object_storage_url,
                    json.dumps(_metadata_to_dict(archive.resource_metadata), ensure_ascii=False),
                    archive.resource_created_at_iso8601,
                ),
            )
            self._conn.commit()

    def delete_archive(self, resource_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM file_resource_archives WHERE resource_id = ?",
                (resource_id,),
            )
            self._conn.commit()


def _row_to_archive(row: Any) -> FileResourceArchive:
    metadata_data = json.loads(row[7])
    return FileResourceArchive(
        resource_id=str(row[0]),
        resource_content_hash=str(row[1]),
        resource_category=str(row[2]),
        resource_mime_type=str(row[3]),
        resource_size_in_bytes=int(row[4]),
        local_storage_path=str(row[5]),
        object_storage_url=str(row[6]),
        resource_metadata=_metadata_from_dict(metadata_data),
        resource_created_at_iso8601=str(row[8]),
    )


def _metadata_to_dict(metadata: FileResourceMetadata) -> dict[str, Any]:
    return asdict(metadata)


def _metadata_from_dict(data: dict[str, Any]) -> FileResourceMetadata:
    category = data.get("resource_category")
    if category == "image":
        return ImageResourceMetadata(image_width=int(data["image_width"]), image_height=int(data["image_height"]))
    if category == "pdf":
        return PdfResourceMetadata(pdf_page_count=int(data["pdf_page_count"]))
    raise ValueError(f"不支持的文件资源元数据类型: {category}")
