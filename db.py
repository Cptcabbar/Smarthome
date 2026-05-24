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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS automations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL,
                days_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS action_log (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL,
                days_json TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
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


# ── Automation CRUD ───────────────────────────────────────────────────────────

def _row_to_automation(row: sqlite3.Row) -> dict[str, Any]:
    days = json.loads(row["days_json"] or "[0,1,2,3,4,5,6]")
    if not isinstance(days, list):
        days = list(range(7))
    return {
        "id": row["id"],
        "name": row["name"],
        "device_id": row["device_id"],
        "action": row["action"],
        "hour": int(row["hour"]),
        "minute": int(row["minute"]),
        "days": days,
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def get_automations() -> list[dict[str, Any]]:
    with _conn() as db:
        rows = db.execute("SELECT * FROM automations ORDER BY created_at").fetchall()
    return [_row_to_automation(r) for r in rows]


def get_automation(auto_id: str) -> dict[str, Any] | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM automations WHERE id = ?", (auto_id,)).fetchone()
    return _row_to_automation(row) if row else None


def create_automation(data: dict[str, Any]) -> str:
    auto_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as db:
        db.execute(
            """
            INSERT INTO automations (id, name, device_id, action, hour, minute, days_json, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                auto_id,
                (data.get("name") or "Otomasyon").strip(),
                data["device_id"],
                data["action"],
                int(data["hour"]),
                int(data["minute"]),
                json.dumps(data.get("days", list(range(7)))),
                now,
            ),
        )
        db.commit()
    return auto_id


def update_automation(auto_id: str, data: dict[str, Any]) -> bool:
    fields: list[str] = []
    vals: list[Any] = []
    for key in ("name", "device_id", "action"):
        if key in data:
            fields.append(f"{key} = ?")
            vals.append(data[key])
    if "hour" in data:
        fields.append("hour = ?")
        vals.append(int(data["hour"]))
    if "minute" in data:
        fields.append("minute = ?")
        vals.append(int(data["minute"]))
    if "days" in data:
        fields.append("days_json = ?")
        vals.append(json.dumps(data["days"]))
    if "enabled" in data:
        fields.append("enabled = ?")
        vals.append(1 if data["enabled"] else 0)
    if not fields:
        return True
    vals.append(auto_id)
    with _conn() as db:
        cur = db.execute(f"UPDATE automations SET {', '.join(fields)} WHERE id = ?", vals)
        db.commit()
        return cur.rowcount > 0


def delete_automation(auto_id: str) -> bool:
    with _conn() as db:
        cur = db.execute("DELETE FROM automations WHERE id = ?", (auto_id,))
        db.commit()
        return cur.rowcount > 0


# ── Action log ─────────────────────────────────────────────────────────────────

def log_action(device_id: str, action: str, source: str = "manual") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as db:
        db.execute(
            "INSERT INTO action_log (id, device_id, action, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), device_id, action, source, now),
        )
        db.commit()


def get_action_log(since_iso: str | None = None, exclude_sources: list[str] | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM action_log"
    conds: list[str] = []
    vals: list[Any] = []
    if since_iso:
        conds.append("created_at >= ?")
        vals.append(since_iso)
    if exclude_sources:
        placeholders = ",".join("?" for _ in exclude_sources)
        conds.append(f"source NOT IN ({placeholders})")
        vals.extend(exclude_sources)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at"
    with _conn() as db:
        rows = db.execute(q, vals).fetchall()
    return [
        {"id": r["id"], "device_id": r["device_id"], "action": r["action"],
         "source": r["source"], "created_at": r["created_at"]}
        for r in rows
    ]


def prune_action_log(before_iso: str) -> int:
    with _conn() as db:
        cur = db.execute("DELETE FROM action_log WHERE created_at < ?", (before_iso,))
        db.commit()
        return cur.rowcount


# ── Suggestions ──────────────────────────────────────────────────────────────

def _row_to_suggestion(row: sqlite3.Row) -> dict[str, Any]:
    days = json.loads(row["days_json"] or "[0,1,2,3,4,5,6]")
    if not isinstance(days, list):
        days = list(range(7))
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "action": row["action"],
        "hour": int(row["hour"]),
        "minute": int(row["minute"]),
        "days": days,
        "reason": row["reason"] or "",
        "status": row["status"],
        "created_at": row["created_at"],
    }


def get_suggestions(status: str = "pending") -> list[dict[str, Any]]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM suggestions WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    return [_row_to_suggestion(r) for r in rows]


def get_suggestion(sug_id: str) -> dict[str, Any] | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM suggestions WHERE id = ?", (sug_id,)).fetchone()
    return _row_to_suggestion(row) if row else None


def create_suggestion(data: dict[str, Any]) -> str:
    sug_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as db:
        db.execute(
            """
            INSERT INTO suggestions (id, device_id, action, hour, minute, days_json, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                sug_id,
                data["device_id"],
                data["action"],
                int(data["hour"]),
                int(data["minute"]),
                json.dumps(data.get("days", list(range(7)))),
                (data.get("reason") or "").strip(),
                now,
            ),
        )
        db.commit()
    return sug_id


def update_suggestion_status(sug_id: str, status: str) -> bool:
    with _conn() as db:
        cur = db.execute("UPDATE suggestions SET status = ? WHERE id = ?", (status, sug_id))
        db.commit()
        return cur.rowcount > 0


def delete_suggestion(sug_id: str) -> bool:
    with _conn() as db:
        cur = db.execute("DELETE FROM suggestions WHERE id = ?", (sug_id,))
        db.commit()
        return cur.rowcount > 0


# ── Settings (key-value) ───────────────────────────────────────────────────────

def get_setting(key: str) -> str | None:
    with _conn() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _conn() as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        db.commit()
