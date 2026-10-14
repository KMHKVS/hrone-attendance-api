# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
echo "MONGO_URI=mongodb://localhost:27017" > .env && echo "MONGO_DB=attendance_db" >> .env
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (7.0 recommended). Local: `docker run -d -p 27017:27017 --name mongo mongo:7`.
Indexes are created at startup. Interactive docs: http://localhost:8000/docs

## Tests (optional)
```bash
pip install -r requirements-dev.txt
pytest tests/test_rules.py -q      # rules R1-R5, rounding, validation (no database needed)
# integration + analytics-vs-oracle + explain checks: use a throw-away database whose name ends in _test
MONGO_DB=attendance_test uvicorn app.main:app --port 8000      # terminal 1
MONGO_DB=attendance_test pytest tests/test_integration.py -q   # terminal 2
```

## Notes
- Every endpoint of `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.
- Analytics read the stored derived values. Final rounding is half-up and done in Python, because MongoDB's `$round` is banker's rounding.
- `moving_avg_7d` averages the unrounded daily rates and rounds the result once.
- Concurrent corrections: the losing `PATCH` gets 409 and can simply be retried.
- A `PATCH` that does not change status or punch times is rejected with 422.
- Aggregations and the explain endpoint need MongoDB 5.0+ (`$setWindowFields`, `$rank`).
