from flask import Blueprint, jsonify, render_template, request

from app.db import get_db

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
admin_api_bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")


def _sync_all_lots_to_capacity(db):
   
    lots = db.execute("SELECT id, capacity FROM parking_lots").fetchall()
    for lot in lots:
        existing = db.execute(
            "SELECT slot_number FROM parking_slots WHERE lot_id = ? ORDER BY slot_number",
            (lot["id"],),
        ).fetchall()
        existing_names = {row["slot_number"] for row in existing}
        missing = lot["capacity"] - len(existing_names)
        if missing <= 0:
            continue
        counter = 1
        created = 0
        while created < missing:
            candidate = f"L{lot['id']}-S{counter:02d}"
            counter += 1
            if candidate in existing_names:
                continue
            db.execute(
                """
                INSERT INTO parking_slots(lot_id, slot_number, status, hourly_rate)
                VALUES (?, ?, 'available', 5.0)
                """,
                (lot["id"], candidate),
            )
            created += 1
    db.commit()


@admin_bp.route("/")
def dashboard():
    db = get_db()
    _sync_all_lots_to_capacity(db)

    metrics = db.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM parking_lots) AS total_lots,
            (SELECT COUNT(*) FROM parking_slots) AS total_slots,
            (SELECT COUNT(*) FROM parking_slots WHERE status = 'available') AS slots_available,
            (SELECT COUNT(*) FROM parking_slots WHERE status = 'occupied') AS slots_occupied,
            (SELECT COUNT(*) FROM parking_sessions WHERE status = 'active') AS active_sessions,
            COALESCE(
                (SELECT SUM(amount) FROM payments WHERE date(created_at) = date('now')),
                0
            ) AS revenue_today
        """
    ).fetchone()

    lots = db.execute(
        """
        SELECT
            l.id,
            l.name,
            l.location,
            l.capacity,
            COUNT(s.id) AS total_slots,
            SUM(CASE WHEN s.status = 'available' THEN 1 ELSE 0 END) AS available,
            SUM(CASE WHEN s.status = 'occupied' THEN 1 ELSE 0 END) AS occupied,
            SUM(CASE WHEN s.status = 'maintenance' THEN 1 ELSE 0 END) AS maintenance
        FROM parking_lots l
        LEFT JOIN parking_slots s ON s.lot_id = l.id
        GROUP BY l.id, l.name, l.location, l.capacity
        ORDER BY l.id
        """
    ).fetchall()

    active_sessions = db.execute(
        """
        SELECT
            s.id,
            s.slot_id,
            s.vehicle_plate,
            s.driver_name,
            s.check_in,
            sl.slot_number,
            l.name AS lot_name
        FROM parking_sessions s
        JOIN parking_slots sl ON sl.id = s.slot_id
        JOIN parking_lots l ON l.id = sl.lot_id
        WHERE s.status = 'active'
        ORDER BY s.check_in DESC
        """
    ).fetchall()

    available_slots = db.execute(
        """
        SELECT s.id, s.slot_number, l.name AS lot_name
        FROM parking_slots s
        JOIN parking_lots l ON l.id = s.lot_id
        WHERE s.status = 'available'
        ORDER BY l.id, s.slot_number
        LIMIT 50
        """
    ).fetchall()

    slots = db.execute(
        """
        SELECT s.id, s.slot_number, s.status, s.hourly_rate, l.name AS lot_name
        FROM parking_slots s
        JOIN parking_lots l ON l.id = s.lot_id
        ORDER BY l.id, s.slot_number
        """
    ).fetchall()

    recent_payments = db.execute(
        """
        SELECT
            p.id,
            p.amount,
            p.currency,
            p.status,
            p.created_at,
            p.session_id
        FROM payments p
        ORDER BY p.created_at DESC
        LIMIT 10
        """
    ).fetchall()

    return render_template(
        "admin_dashboard.html",
        metrics=metrics,
        lots=lots,
        active_sessions=active_sessions,
        available_slots=available_slots,
        slots=slots,
        recent_payments=recent_payments,
    )


@admin_api_bp.post("/lots")
def create_lot():
    payload = request.get_json(silent=True) or {}
    required = ["name", "location", "capacity"]
    missing = [f for f in required if f not in payload]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    try:
        capacity = int(payload.get("capacity"))
    except (TypeError, ValueError):
        return jsonify({"error": "capacity must be an integer"}), 400
    if capacity < 0:
        return jsonify({"error": "capacity must be non-negative"}), 400

    name = str(payload.get("name", "")).strip()
    location = str(payload.get("location", "")).strip()
    slots_to_create = payload.get("slots")
    try:
        slots_to_create = int(slots_to_create) if slots_to_create is not None else 0
    except (TypeError, ValueError):
        return jsonify({"error": "slots must be an integer if provided"}), 400
    if slots_to_create < 0:
        return jsonify({"error": "slots must be non-negative"}), 400

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        result = db.execute(
            "INSERT INTO parking_lots(name, location, capacity) VALUES (?, ?, ?)",
            (name, location, capacity),
        )
        lot_id = result.lastrowid

        for idx in range(1, slots_to_create + 1):
            db.execute(
                """
                INSERT INTO parking_slots(lot_id, slot_number, status, hourly_rate)
                VALUES (?, ?, 'available', 5.0)
                """,
                (lot_id, f"L{lot_id}-S{idx:02d}"),
            )

        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify({"lot_id": lot_id, "slots_created": slots_to_create}), 201


@admin_api_bp.post("/slots")
def create_slot():
    payload = request.get_json(silent=True) or {}
    required = ["lot_id", "slot_number"]
    missing = [f for f in required if f not in payload]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    try:
        lot_id = int(payload.get("lot_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "lot_id must be an integer"}), 400

    slot_number = str(payload.get("slot_number", "")).strip()
    if not slot_number:
        return jsonify({"error": "slot_number cannot be empty"}), 400

    status = payload.get("status", "available")
    if status not in ("available", "occupied", "maintenance"):
        return jsonify({"error": "status must be available, occupied, or maintenance"}), 400

    try:
        hourly_rate = float(payload.get("hourly_rate", 5.0))
    except (TypeError, ValueError):
        return jsonify({"error": "hourly_rate must be a number"}), 400
    if hourly_rate <= 0:
        return jsonify({"error": "hourly_rate must be positive"}), 400

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")

        lot_exists = db.execute("SELECT 1 FROM parking_lots WHERE id = ? LIMIT 1", (lot_id,)).fetchone()
        if not lot_exists:
            db.rollback()
            return jsonify({"error": "lot_id not found"}), 404

        db.execute(
            """
            INSERT INTO parking_slots(lot_id, slot_number, status, hourly_rate)
            VALUES (?, ?, ?, ?)
            """,
            (lot_id, slot_number, status, hourly_rate),
        )

        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify({"message": "slot created", "lot_id": lot_id, "slot_number": slot_number}), 201


@admin_api_bp.post("/lots/<int:lot_id>/sync_slots")
def sync_slots_to_capacity(lot_id: int):
    """
    Create available slots up to the lot capacity, using predictable names L{lot}-S{nn}.
    """
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        lot = db.execute(
            "SELECT id, capacity FROM parking_lots WHERE id = ?",
            (lot_id,),
        ).fetchone()
        if lot is None:
            db.rollback()
            return jsonify({"error": "lot not found"}), 404

        current = db.execute(
            "SELECT slot_number FROM parking_slots WHERE lot_id = ? ORDER BY slot_number",
            (lot_id,),
        ).fetchall()
        existing = {row["slot_number"] for row in current}
        missing = lot["capacity"] - len(existing)
        created = 0
        counter = 1
        while created < missing:
            candidate = f"L{lot_id}-S{counter:02d}"
            counter += 1
            if candidate in existing:
                continue
            db.execute(
                """
                INSERT INTO parking_slots(lot_id, slot_number, status, hourly_rate)
                VALUES (?, ?, 'available', 5.0)
                """,
                (lot_id, candidate),
            )
            created += 1

        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify({"message": "slots synced", "created": created, "lot_id": lot_id})


@admin_api_bp.post("/slots/<int:slot_id>/status")
def update_slot_status(slot_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in ("available", "occupied", "maintenance"):
        return jsonify({"error": "status must be available, occupied, or maintenance"}), 400

    db = get_db()
    try:
        db.execute(
            "UPDATE parking_slots SET status = ? WHERE id = ?",
            (status, slot_id),
        )
        if db.total_changes == 0:
            return jsonify({"error": "slot not found"}), 404
        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify({"message": "status updated", "slot_id": slot_id, "status": status})
