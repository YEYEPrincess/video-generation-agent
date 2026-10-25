"""产物读写：所有阶段产物的唯一存取入口。

按阶段隔离目录：`artifacts/{task_id}/{STAGE}/v{n}/{stage}.json`
媒体文件放 `{STAGE}/media/`，形态与 JSON 分离——JSON 是事实，媒体是附件。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from app.core.idem import short_hash
from app.core.state_machine import Stage
from app.db import iso
from app.settings import Settings


def stage_dir(settings: Settings, task_id: str, stage: Stage) -> Path:
    return Path(settings.artifacts_dir) / task_id / stage.value


def latest_relative(conn: sqlite3.Connection, task_id: str, stage: Stage) -> str | None:
    row = conn.execute(
        "SELECT path FROM artifacts WHERE task_id = ? AND stage = ? ORDER BY version DESC LIMIT 1",
        (task_id, stage.value),
    ).fetchone()
    return str(row["path"]) if row else None


def load(conn: sqlite3.Connection, settings: Settings, task_id: str, stage: Stage) -> dict:
    """读取某阶段最新产物；不存在返回 {}。"""
    rel = latest_relative(conn, task_id, stage)
    if not rel:
        return {}
    path = Path(settings.artifacts_dir) / rel
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write(
    conn: sqlite3.Connection, settings: Settings, task_id: str, stage: Stage, data: dict
) -> tuple[str, str]:
    """落盘并登记。返回 (artifact_id, 相对路径)。"""
    rel_dir = Path(task_id) / stage.value
    abs_dir = Path(settings.artifacts_dir) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)

    version = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM artifacts WHERE task_id = ? AND stage = ?",
            (task_id, stage.value),
        ).fetchone()["c"]
    ) + 1

    rel_path = (rel_dir / f"v{version}" / f"{stage.value.lower()}.json").as_posix()
    abs_path = Path(settings.artifacts_dir) / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    text = json.dumps(data, ensure_ascii=False, indent=2)
    abs_path.write_text(text, encoding="utf-8")

    artifact_id = "a_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO artifacts(id, task_id, stage, version, path, hash, kind, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (artifact_id, task_id, stage.value, version, rel_path, short_hash(text), "json", iso()),
    )
    return artifact_id, rel_path
