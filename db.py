"""
SQLite kalıcılık: cihazlar, durum, keyword listeleri.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import os

DB_PATH = os.environ.get(
    "SMARTHOME_DB",
    str(Path(__file__).resolve().parent / "smarthome.db"),
)

DEFAULT_PIN_POOL = [17, 27, 22, 23, 24, 25, 5, 6, 13, 19, 26]


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '🏠',
                color TEXT NOT NULL DEFAULT '#38bdf8',
                pin INTEGER NOT NULL DEFAULT 0,
                state INTEGER NOT NULL DEFAULT 0,
                keywords_json TEXT NOT NULL DEFAULT '[]',
                mqtt_topic TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        try:
            db.execute("ALTER TABLE devices ADD COLUMN mqtt_topic TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        db.commit()


def _row_to_device(row: sqlite3.Row) -> dict[str, Any]:
    kws = json.loads(row["keywords_json"] or "[]")
    if not isinstance(kws, list):
        kws = []
    return {
        "id": row["id"],
        "label": row["name"],
        "icon": row["icon"],
        "color": row["color"],
        "pin": int(row["pin"]),
        "state": bool(row["state"]),
        "keywords": [str(x).lower().strip() for x in kws if str(x).strip()],
        "mqtt_topic": row["mqtt_topic"] or "",
    }


def list_devices_dict() -> dict[str, dict[str, Any]]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM devices ORDER BY created_at"
        ).fetchall()
    return {row["id"]: _row_to_device(row) for row in rows}


def get_device(device_id: str) -> dict[str, Any] | None:
    with _conn() as db:
        row = db.execute(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
    return _row_to_device(row) if row else None


def _next_free_pin() -> int:
    with _conn() as db:
        used = {
            int(r[0])
            for r in db.execute("SELECT pin FROM devices WHERE pin > 0").fetchall()
        }
    for p in DEFAULT_PIN_POOL:
        if p not in used:
            return p
    return 0


def create_device(
    name: str,
    icon: str,
    color: str,
    pin: int | None,
    keywords: list[str],
    mqtt_topic: str = "",
) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Cihaz adı boş olamaz")
    dev_id = str(uuid.uuid4())
    if pin is None or pin <= 0:
        pin = _next_free_pin()
    now = datetime.now(timezone.utc).isoformat()
    kws = json.dumps(keywords, ensure_ascii=False)
    with _conn() as db:
        db.execute(
            """
            INSERT INTO devices (id, name, icon, color, pin, state, keywords_json, mqtt_topic, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (dev_id, name, icon or "🏠", color or "#38bdf8", pin, kws, mqtt_topic.strip(), now),
        )
        db.commit()
    return dev_id


def update_device(
    device_id: str,
    *,
    name: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    pin: int | None = None,
    keywords: list[str] | None = None,
    mqtt_topic: str | None = None,
) -> bool:
    fields: list[str] = []
    vals: list[Any] = []
    if name is not None:
        fields.append("name = ?")
        vals.append(name.strip())
    if icon is not None:
        fields.append("icon = ?")
        vals.append(icon)
    if color is not None:
        fields.append("color = ?")
        vals.append(color)
    if pin is not None:
        fields.append("pin = ?")
        vals.append(int(pin))
    if keywords is not None:
        fields.append("keywords_json = ?")
        vals.append(json.dumps(keywords, ensure_ascii=False))
    if mqtt_topic is not None:
        fields.append("mqtt_topic = ?")
        vals.append(mqtt_topic.strip())
    if not fields:
        return True
    vals.append(device_id)
    with _conn() as db:
        cur = db.execute(
            f"UPDATE devices SET {', '.join(fields)} WHERE id = ?",
            vals,
        )
        db.commit()
        return cur.rowcount > 0


def delete_device(device_id: str) -> bool:
    with _conn() as db:
        cur = db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        db.commit()
        return cur.rowcount > 0


def set_device_state(device_id: str, state: bool) -> bool:
    with _conn() as db:
        cur = db.execute(
            "UPDATE devices SET state = ? WHERE id = ?",
            (1 if state else 0, device_id),
        )
        db.commit()
        return cur.rowcount > 0


def get_keywords(device_id: str) -> list[str]:
    d = get_device(device_id)
    return d["keywords"] if d else []
