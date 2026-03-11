# 451-project

Smart Parking Management MVP built with Flask.

## Implemented first feature
- Real-time availability lookup for parking slots
- Advance slot reservation
- Double-booking protection for overlapping reservations

## Run
```bash
python3 run.py
```

## API quick test
```bash
# List demo lots
curl http://127.0.0.1:5000/api/lots

# Check available slots in a time window
curl "http://127.0.0.1:5000/api/slots/available?lot_id=1&start_time=2026-03-10T10:00:00&end_time=2026-03-10T11:00:00"

# Reserve a slot
curl -X POST http://127.0.0.1:5000/api/reservations \
  -H "Content-Type: application/json" \
  -d '{
    "slot_id": 1,
    "driver_name": "Josh",
    "vehicle_plate": "7ABC123",
    "start_time": "2026-03-10T10:00:00",
    "end_time": "2026-03-10T11:00:00"
  }'
```
