import sqlite3
from pathlib import Path

from flask import current_app, g


def get_db():
    if "db" not in g:
        db_path = current_app.config["DATABASE_PATH"]
        g.db = sqlite3.connect(db_path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS parking_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            capacity INTEGER NOT NULL CHECK (capacity >= 0)
        );

        CREATE TABLE IF NOT EXISTS parking_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id INTEGER NOT NULL,
            slot_number TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'available'
                CHECK (status IN ('available', 'occupied', 'maintenance')),
            hourly_rate REAL NOT NULL DEFAULT 5.0,
            FOREIGN KEY (lot_id) REFERENCES parking_lots(id) ON DELETE CASCADE,
            UNIQUE (lot_id, slot_number)
        );

        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL,
            driver_name TEXT NOT NULL,
            vehicle_plate TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved'
                CHECK (status IN ('reserved', 'in_use', 'completed', 'cancelled', 'no_show')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (start_time < end_time),
            FOREIGN KEY (slot_id) REFERENCES parking_slots(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS parking_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL,
            vehicle_plate TEXT NOT NULL,
            driver_name TEXT,
            check_in TEXT NOT NULL,
            check_out TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'cancelled')),
            reservation_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (slot_id) REFERENCES parking_slots(id) ON DELETE CASCADE,
            FOREIGN KEY (reservation_id) REFERENCES reservations(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'paid', 'failed', 'refunded')),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES parking_sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_reservations_slot_time
            ON reservations(slot_id, start_time, end_time, status);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_slot_active
            ON parking_sessions(slot_id) WHERE status = 'active';

        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_vehicle_active
            ON parking_sessions(vehicle_plate) WHERE status = 'active';
        """
    )
    seed_demo_data(db)
    db.commit()


def seed_demo_data(db):
    lot_count = db.execute("SELECT COUNT(*) AS total FROM parking_lots").fetchone()["total"]
    if lot_count > 0:
        return

    db.execute(
        """
        INSERT INTO parking_lots(name, location, capacity)
        VALUES (?, ?, ?)
        """,
        ("Main Campus Lot", "North Gate", 10),
    )
    lot_id = db.execute("SELECT id FROM parking_lots LIMIT 1").fetchone()["id"]

    for slot_num in range(1, 11):
        db.execute(
            """
            INSERT INTO parking_slots(lot_id, slot_number, status, hourly_rate)
            VALUES (?, ?, 'available', 5.0)
            """,
            (lot_id, f"A-{slot_num:02d}"),
        )


def init_app(app):
    database_path = Path(app.root_path).parent / "parking.db"
    app.config.setdefault("DATABASE_PATH", str(database_path))
    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()
