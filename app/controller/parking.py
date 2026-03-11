from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from app.db import get_db

parking_bp = Blueprint("parking", __name__, url_prefix="/api")


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
