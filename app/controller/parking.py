from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from app.db import get_db

parking_bp = Blueprint("parking", __name__, url_prefix="/api")
from flask import render_template


def parse_iso_datetime(value, field_name):
    if not value:
        raise ValueError(f"'{field_name}' is required")

    try:
        dt_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"'{field_name}' must be a valid ISO datetime") from exc

    if dt_value.tzinfo is not None:
        dt_value = dt_value.astimezone(timezone.utc).replace(tzinfo=None)

    return dt_value.replace(microsecond=0)


@parking_bp.get("/lots")
def list_lots():
    db = get_db()
    lots = db.execute(
        """
        SELECT id, name, location, capacity
        FROM parking_lots
        ORDER BY id
        """
    ).fetchall()
    return jsonify([dict(lot) for lot in lots])


@parking_bp.get("/slots/available")
def list_available_slots():
    lot_id = request.args.get("lot_id", type=int)
    start_time_raw = request.args.get("start_time")
    end_time_raw = request.args.get("end_time")

    if (start_time_raw and not end_time_raw) or (end_time_raw and not start_time_raw):
        return jsonify({"error": "Provide both start_time and end_time together"}), 400

    if start_time_raw and end_time_raw:
        try:
            start_time = parse_iso_datetime(start_time_raw, "start_time")
            end_time = parse_iso_datetime(end_time_raw, "end_time")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        start_time = datetime.utcnow().replace(microsecond=0)
        end_time = start_time + timedelta(minutes=1)

    if start_time >= end_time:
        return jsonify({"error": "start_time must be earlier than end_time"}), 400

    db = get_db()
    slots = db.execute(
        """
        SELECT
            s.id,
            s.slot_number,
            s.lot_id,
            l.name AS lot_name,
            l.location
        FROM parking_slots s
        INNER JOIN parking_lots l ON l.id = s.lot_id
        WHERE (? IS NULL OR s.lot_id = ?)
          AND s.status = 'available'
          AND NOT EXISTS (
              SELECT 1
              FROM reservations r
              WHERE r.slot_id = s.id
                AND r.status = 'reserved'
                AND r.start_time < ?
                AND r.end_time > ?
          )
        ORDER BY s.lot_id, s.slot_number
        """,
        (
            lot_id,
            lot_id,
            end_time.isoformat(timespec="seconds"),
            start_time.isoformat(timespec="seconds"),
        ),
    ).fetchall()

    return jsonify(
        {
            "window": {
                "start_time": start_time.isoformat(timespec="seconds"),
                "end_time": end_time.isoformat(timespec="seconds"),
            },
            "count": len(slots),
            "slots": [dict(slot) for slot in slots],
        }
    )


@parking_bp.post("/reservations")
def create_reservation():
    payload = request.get_json(silent=True) or {}
    required_fields = ["slot_id", "driver_name", "vehicle_plate", "start_time", "end_time"]
    missing_fields = [field for field in required_fields if field not in payload]

    if missing_fields:
        return jsonify({"error": f"Missing fields: {', '.join(missing_fields)}"}), 400

    try:
        slot_id = int(payload["slot_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "slot_id must be an integer"}), 400

    driver_name = str(payload["driver_name"]).strip()
    vehicle_plate = str(payload["vehicle_plate"]).strip().upper()

    if not driver_name:
        return jsonify({"error": "driver_name cannot be empty"}), 400
    if not vehicle_plate:
        return jsonify({"error": "vehicle_plate cannot be empty"}), 400

    try:
        start_time = parse_iso_datetime(payload["start_time"], "start_time")
        end_time = parse_iso_datetime(payload["end_time"], "end_time")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if start_time >= end_time:
        return jsonify({"error": "start_time must be earlier than end_time"}), 400

    start_iso = start_time.isoformat(timespec="seconds")
    end_iso = end_time.isoformat(timespec="seconds")

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")

        slot = db.execute(
            """
            SELECT id, lot_id, slot_number, status
            FROM parking_slots
            WHERE id = ?
            """,
            (slot_id,),
        ).fetchone()

        if slot is None:
            db.rollback()
            return jsonify({"error": "Slot not found"}), 404

        if slot["status"] != "available":
            db.rollback()
            return jsonify({"error": f"Slot {slot['slot_number']} is not available for booking"}), 409

        overlap = db.execute(
            """
            SELECT 1
            FROM reservations
            WHERE slot_id = ?
              AND status = 'reserved'
              AND start_time < ?
              AND end_time > ?
            LIMIT 1
            """,
            (slot_id, end_iso, start_iso),
        ).fetchone()

        if overlap:
            db.rollback()
            return jsonify({"error": "Slot is already reserved during that time window"}), 409

        result = db.execute(
            """
            INSERT INTO reservations(slot_id, driver_name, vehicle_plate, start_time, end_time, status)
            VALUES (?, ?, ?, ?, ?, 'reserved')
            """,
            (slot_id, driver_name, vehicle_plate, start_iso, end_iso),
        )
        reservation_id = result.lastrowid
        db.commit()
    except Exception:
        db.rollback()
        raise

    return (
        jsonify(
            {
                "reservation_id": reservation_id,
                "slot_id": slot_id,
                "slot_number": slot["slot_number"],
                "driver_name": driver_name,
                "vehicle_plate": vehicle_plate,
                "start_time": start_iso,
                "end_time": end_iso,
                "status": "reserved",
            }
        ),
        201,
    )


def compute_fee(start_dt, end_dt, hourly_rate, grace_minutes=10, rounding_minutes=15, min_fee=None):
    """
    Calculate fee using a grace period and rounded billing blocks.
    """
    grace = timedelta(minutes=grace_minutes)
    rounding = timedelta(minutes=rounding_minutes)
    duration = max(timedelta(0), end_dt - start_dt - grace)

    if duration <= timedelta(0):
        billable = timedelta(0)
    else:
        blocks = -(-duration // rounding)  # ceiling division
        billable = blocks * rounding

    amount = (billable.total_seconds() / 3600) * hourly_rate
    if min_fee is not None:
        amount = max(amount, min_fee)
    return round(amount, 2)


@parking_bp.post("/sessions/checkin")
def checkin():
    payload = request.get_json(silent=True) or {}
    required = ["slot_id", "vehicle_plate"]
    missing = [f for f in required if f not in payload]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    try:
        slot_id = int(payload.get("slot_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "slot_id must be an integer"}), 400

    vehicle_plate = str(payload.get("vehicle_plate", "")).upper().strip()
    driver_name = str(payload.get("driver_name", "")).strip() or None
    reservation_id = payload.get("reservation_id")

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")

        slot = db.execute(
            "SELECT id, slot_number, status, hourly_rate FROM parking_slots WHERE id = ?",
            (slot_id,),
        ).fetchone()
        if slot is None:
            db.rollback()
            return jsonify({"error": "Slot not found"}), 404
        if slot["status"] != "available":
            db.rollback()
            return jsonify({"error": "Slot is not available"}), 409

        active_slot = db.execute(
            "SELECT 1 FROM parking_sessions WHERE slot_id = ? AND status = 'active' LIMIT 1",
            (slot_id,),
        ).fetchone()
        if active_slot:
            db.rollback()
            return jsonify({"error": "Slot already has an active session"}), 409

        active_vehicle = db.execute(
            "SELECT 1 FROM parking_sessions WHERE vehicle_plate = ? AND status = 'active' LIMIT 1",
            (vehicle_plate,),
        ).fetchone()
        if active_vehicle:
            db.rollback()
            return jsonify({"error": "Vehicle already has an active session"}), 409

        now_iso = datetime.utcnow().replace(microsecond=0).isoformat()
        res_id_to_store = None

        if reservation_id is not None:
            try:
                reservation_id_int = int(reservation_id)
            except (TypeError, ValueError):
                db.rollback()
                return jsonify({"error": "reservation_id must be an integer"}), 400

            res = db.execute(
                """
                SELECT id, start_time, end_time, status
                FROM reservations
                WHERE id = ? AND slot_id = ?
                """,
                (reservation_id_int, slot_id),
            ).fetchone()
            if res is None:
                db.rollback()
                return jsonify({"error": "Reservation not found for this slot"}), 404
            if res["status"] not in ("reserved", "in_use"):
                db.rollback()
                return jsonify({"error": "Reservation is not active"}), 409
            if not (res["start_time"] <= now_iso <= res["end_time"]):
                db.rollback()
                return jsonify({"error": "Check-in outside reservation window"}), 409
            res_id_to_store = reservation_id_int
            db.execute(
                "UPDATE reservations SET status = 'in_use' WHERE id = ?",
                (reservation_id_int,),
            )

        result = db.execute(
            """
            INSERT INTO parking_sessions(slot_id, vehicle_plate, driver_name, check_in, status, reservation_id)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (slot_id, vehicle_plate, driver_name, now_iso, res_id_to_store),
        )
        session_id = result.lastrowid
        db.execute(
            "UPDATE parking_slots SET status = 'occupied' WHERE id = ?",
            (slot_id,),
        )

        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify(
        {
            "session_id": session_id,
            "slot_id": slot_id,
            "vehicle_plate": vehicle_plate,
            "check_in": now_iso,
        }
    ), 201


@parking_bp.post("/sessions/checkout")
def checkout():
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("session_id")
    if session_id is None:
        return jsonify({"error": "session_id is required"}), 400

    try:
        session_id_int = int(session_id)
    except (TypeError, ValueError):
        return jsonify({"error": "session_id must be an integer"}), 400

    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")

        session = db.execute(
            """
            SELECT s.*, sl.hourly_rate, sl.slot_number, sl.lot_id, r.end_time AS res_end
            FROM parking_sessions s
            JOIN parking_slots sl ON sl.id = s.slot_id
            LEFT JOIN reservations r ON r.id = s.reservation_id
            WHERE s.id = ? AND s.status = 'active'
            """,
            (session_id_int,),
        ).fetchone()

        if session is None:
            db.rollback()
            return jsonify({"error": "Active session not found"}), 404

        check_in_dt = datetime.fromisoformat(session["check_in"])
        check_out_dt = datetime.utcnow().replace(microsecond=0)
        amount = compute_fee(check_in_dt, check_out_dt, session["hourly_rate"], min_fee=session["hourly_rate"])

        db.execute(
            "UPDATE parking_sessions SET status = 'completed', check_out = ? WHERE id = ?",
            (check_out_dt.isoformat(), session_id_int),
        )
        db.execute(
            "UPDATE parking_slots SET status = 'available' WHERE id = ?",
            (session["slot_id"],),
        )
        db.execute(
            """
            INSERT INTO payments(session_id, amount, currency, status)
            VALUES (?, ?, 'USD', 'paid')
            """,
            (session_id_int, amount),
        )
        if session["reservation_id"]:
            db.execute(
                "UPDATE reservations SET status = 'completed' WHERE id = ?",
                (session["reservation_id"],),
            )

        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify(
        {
            "session_id": session_id_int,
            "slot_id": session["slot_id"],
            "vehicle_plate": session["vehicle_plate"],
            "check_in": session["check_in"],
            "check_out": check_out_dt.isoformat(),
            "amount": amount,
            "currency": "USD",
        }
    )


@parking_bp.get("/ui")
def ui_home():
    return render_template("index.html")

@parking_bp.get("/ui/lots")
def ui_lots():
    return render_template("lots.html")

@parking_bp.get("/ui/slots")
def ui_slots():
    return render_template("slots.html")

@parking_bp.get("/ui/reserve")
def ui_reserve():
    return render_template("reserve.html")

@parking_bp.get("/ui/checkin")
def ui_checkin():
    return render_template("checkin.html")

@parking_bp.get("/ui/checkout")
def ui_checkout():
    return render_template("checkout.html")