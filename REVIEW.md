# REVIEW.md

Review of the starter `app/main.py`. "Notice" says how the defect shows up; "Fix" is what the submitted code does.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Shift start is built with `punch_in.replace(hour=...)`, i.e. in the punch-in's own zone (UTC or naive), not IST. | Shift 09:30, punch 10:05 IST (04:35Z): start becomes 09:30Z, result is 0 instead of 35. | Shift start is built in IST on the attendance date (`at_shift_time`). |
| 2 | `compute_late_minutes` | Grace is applied to floored minutes: `minutes > 10`. | 09:40:01 gives 10 whole minutes, `10 > 10` is false, so 0 (R2 says 10). Sub-seconds are not truncated either. | Compare whole seconds: late only if `seconds > 600`, then `seconds // 60`; punch is truncated to whole seconds first. |
| 3 | `compute_late_minutes` | Overnight shifts ignored: start is taken on the punch's own calendar day. | Shift 22:00-06:00, punch 01:00 next morning: gives 0 instead of 180. | Uses the R1 attendance date, so the start is 22:00 on the day the shift began. |
| 4 | `compute_work_hours` | `round()` is float/banker's rounding, not half-up; sub-seconds not truncated. | 54 s = 0.015 h gives 0.01 (R4 wants 0.02). | `Decimal(seconds) / 3600` quantized `ROUND_HALF_UP` on whole seconds. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). | Punch-out 10 min after shift end returns 10, expected 0. | Floor to minutes, return it only if `>= 30`. |
| 6 | `compute_overtime` | Subtracts an aware IST datetime from a naive Mongo datetime, and the end is always on the same date. | `TypeError` -> 500 on punch-out; overnight end (next day) is wrong. | `tz_aware` client, everything aware; overnight shift end is on the next day. |
| 7 | `create_employee` | `find_one` then `insert_one`, no unique index. | 10 parallel creates of the same `emp_code` give several 201s and duplicate rows. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 8 | `create_employee` / `EmployeeIn` | No validation: `emp_code` pattern, email, HH:MM, `shift_start == shift_end`, `joined_on`, lengths. | `shift_start: "25:99"` or `joined_on: "2026-02-30"` returns 201. | Pydantic patterns and validators -> 422. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive local time and is returned as a datetime, not epoch ms. | Value depends on server time zone; response shows an ISO string. | `now_utc()` stored; serializer returns epoch milliseconds. |
| 10 | `list_employees` | `skip = page * page_size` (page starts at 1). | `page=1` returns the second page. | `skip = (page - 1) * page_size`. |
| 11 | `list_employees` | `total = count_documents({})` ignores the department filter. | `?department=Eng` shows the total of all employees. | `count_documents` uses the same filter. |
| 12 | `list_employees` | No sort; `page` / `page_size` unbounded. | Order is undefined; `page_size=100000` or `page=0` accepted. | Sorted by `emp_code`; `page >= 1`, `1 <= page_size <= 100` (422 otherwise). |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises. | `POST` with an unknown `emp_code` -> 500 (must be 404). | Explicit 404. |
| 14 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` is local naive time with float seconds; date is the local date; no overnight rule. | Result depends on the server's TZ; 00:30 IST gets the wrong date; 09:40:00.900 is not truncated. | `ms_to_dt` (integer maths, UTC, whole seconds) and `attendance_date` (IST, overnight rule). |
| 15 | `punch_in` | Check-then-insert with no unique (`emp_code`, `date`) index. | Two parallel punch-ins both return 201. | Unique index; `DuplicateKeyError` -> 409. |
| 16 | `punch_in` / `PunchInIn` | `status` is any string; `punched_at` is unchecked. | `status: "ABSENT"` or `"x"` accepted; `punched_at` in seconds creates a 1970 record; a string crashes with 500. | `Literal` presence statuses; strict integer epoch-ms within the allowed range -> 422. |
| 17 | `punch_in` | Response exposes `id` (the ObjectId string) and raw datetimes instead of epoch ms. | `id` in the body; `punch_in` is not an integer. | `serialize_attendance`: no `_id`, epoch ms, legacy defaults. |
| 18 | `list_attendance` | `list(db.attendance_logs.find(q))` loads everything and sorts in Python; no indexes. | Seconds per call and high memory at 100k records; COLLSCAN in explain. | Sort, skip and limit run in MongoDB on pinned indexes. |
| 19 | `list_attendance` | Sorted only by `date` desc, no `emp_code` tie-break. | Pages repeat or skip rows within one date. | Sort `(date desc, emp_code asc)`, covered by an index. |
| 20 | `list_attendance` | No validation: dates, `date_from > date_to`, `status` enum, `page <= 0`, unbounded `page_size`; legacy documents lack `half_day` / `history`. | `page=0` makes a negative slice; `status=BOGUS` returns an empty list instead of 422. | Query validation -> 422; serializer applies the defaults. |
| 21 | `health` | Never touches MongoDB. | Returns 200 with the database down (should be 503). | `ping`; failure -> 503. |
| 22 | module setup | Client is not `tz_aware`; no indexes are created. | Naive datetimes; every list or analytics query is a COLLSCAN. | `tz_aware=True`; idempotent `ensure_indexes()` at startup (retried by `/health`). |

## Looked at and decided it is fine

- `load_dotenv()` does not override variables that are already set, which is what the environment contract asks for.
- The endpoints are plain `def`, so FastAPI runs them in its thread pool; blocking PyMongo calls are fine there.
- Creating the `MongoClient` at import time is fine: PyMongo connects lazily.
- 409 for a duplicate `emp_code` / duplicate punch-in is the right status; only the way it was detected was wrong.
- Default `shift_start` 09:30 and `shift_end` 18:30 match the contract.
