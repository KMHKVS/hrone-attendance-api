"""Pure unit tests: business rules R1-R5, rounding, conversions, request validation.
They need no MongoDB:   pytest tests/test_rules.py
"""
import bson
import pytest
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import main as m

IST = m.IST


def ist(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(timezone.utc)


# ---- R2 late -------------------------------------------------------------- #
@pytest.mark.parametrize(
    "h,mi,s,expected",
    [(9, 29, 0, 0), (9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 50, 0, 20), (11, 0, 0, 90)],
)
def test_late_minutes(h, mi, s, expected):
    assert m.compute_late_minutes(ist(2026, 7, 6, h, mi, s), date(2026, 7, 6), "09:30") == expected


def test_late_ignores_sub_second_part():
    # 09:40:00.900 counts as 09:40:00 -> not late (R1 truncation)
    assert m.ms_to_dt(int(ist(2026, 7, 6, 9, 40, 0).timestamp()) * 1000 + 900) == ist(2026, 7, 6, 9, 40, 0)


# ---- R1 attendance date / overnight ---------------------------------------- #
def test_attendance_date_day_shift_uses_ist_not_utc():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th -> still the 7th
    assert m.attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == date(2026, 7, 7)


def test_attendance_date_overnight():
    assert m.attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)
    assert m.attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == date(2026, 7, 6)  # before shift_end
    assert m.attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == date(2026, 7, 7)  # not earlier


def test_overnight_late_after_midnight_counts_from_previous_day_start():
    pin = ist(2026, 7, 7, 0, 30)  # belongs to the 6th, shift started 22:00 on the 6th
    assert m.compute_late_minutes(pin, date(2026, 7, 6), "22:00") == 150


# ---- R3 overtime ------------------------------------------------------------ #
def test_overtime_threshold_and_floor():
    d = date(2026, 7, 6)
    assert m.compute_overtime(ist(2026, 7, 6, 18, 59, 59), d, "09:30", "18:30") == 0  # 29 min -> below minimum
    assert m.compute_overtime(ist(2026, 7, 6, 19, 0, 0), d, "09:30", "18:30") == 30
    assert m.compute_overtime(ist(2026, 7, 6, 19, 10, 59), d, "09:30", "18:30") == 40
    assert m.compute_overtime(ist(2026, 7, 6, 17, 0, 0), d, "09:30", "18:30") == 0


def test_overtime_overnight_shift_ends_next_day():
    # sample 7: out 06:40 next day, shift end 06:00 -> 40
    assert m.compute_overtime(ist(2026, 7, 7, 6, 40), date(2026, 7, 6), "22:00", "06:00") == 40


# ---- R4 / R5 ------------------------------------------------------------------ #
def test_work_hours_half_up_not_bankers():
    t0 = ist(2026, 7, 6, 9, 0)
    assert m.compute_work_hours(t0, t0 + timedelta(seconds=18)) == 0.01  # 0.005 -> 0.01 (banker's would give 0.0)
    assert m.compute_work_hours(t0, t0 + timedelta(seconds=54)) == 0.02  # 0.015 -> 0.02
    assert m.compute_work_hours(t0, t0 + timedelta(seconds=36)) == 0.01
    assert m.compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12  # sample 1


def test_half_day_uses_rounded_hours():
    assert m.is_half_day(4.49) is True
    assert m.is_half_day(4.5) is False
    t0 = ist(2026, 7, 6, 9, 0)
    wh = m.compute_work_hours(t0, t0 + timedelta(seconds=16199))  # 4.49972... rounds to 4.50 -> not a half day
    assert wh == 4.5 and m.is_half_day(wh) is False


def test_derive_fields_samples():
    start, end, d = "09:30", "18:30", date(2026, 7, 6)
    out = m.derive_fields("PRESENT", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10), d, start, end)
    assert out == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    out = m.derive_fields("PRESENT", ist(2026, 7, 6, 10, 5), None, d, start, end)
    assert out == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}
    assert m.derive_fields("LEAVE", None, None, d, start, end) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# ---- helpers --------------------------------------------------------------------- #
def test_ms_conversion_roundtrip_and_truncation():
    assert m.dt_to_ms(m.ms_to_dt(1783312500999)) == 1783312500000
    assert m.ms_to_dt(1783312500000).tzinfo is not None


def test_round_half_up():
    assert str(m.round_half_up("0.125", 2)) == "0.13"
    assert str(m.round_half_up("0.03125", 4)) == "0.0313"
    assert str(m.round_half_up(m.clean_float(1 / 32), 4)) == "0.0313"


def test_count_weekdays():
    assert m.count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert m.count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15  # mid-month joiner
    assert m.count_weekdays(date(2026, 8, 1), date(2026, 8, 2)) == 0  # Sat+Sun
    assert m.count_weekdays(date(2026, 7, 31), date(2026, 7, 1)) == 0


def test_month_and_trend_range_validation():
    assert m.month_range("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    with pytest.raises(Exception):
        m.trend_range("2026-07-10", "2026-07-01")
    with pytest.raises(Exception):
        m.trend_range("2026-01-01", "2026-04-03")  # 93 days
    assert m.trend_range("2026-07-01", "2026-09-30")[1] == date(2026, 9, 30)  # 92 days


# ---- pipelines are valid BSON (no stray python objects) --------------------------- #
def test_pipelines_encode_to_bson():
    first, last = date(2026, 7, 1), date(2026, 7, 31)
    plans = [
        m.monthly_pipeline("EMP0001", first, last),
        m.summary_pipeline(first, last, None),
        m.summary_pipeline(first, last, "Sales"),
        m.leaderboard_pipeline(first, last, 5, "Sales"),
        m.trend_pipeline({"codes": ["EMP0001"], "base": 2, "joins": ["2026-07-13"]}, first, last),
    ]
    for pipeline, hint in plans:
        bson.encode({"pipeline": pipeline, "hint": hint})


# ---- request validation (no database is touched) ---------------------------------- #
@pytest.fixture(scope="module")
def http():
    return TestClient(m.app)  # no `with`: lifespan (and thus MongoDB) is not started


@pytest.mark.parametrize(
    "payload",
    [
        {"emp_code": "E1", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0001", "name": "A", "email": "nope", "department": "X", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-02-30"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-01",
         "shift_start": "09:00", "shift_end": "09:00"},
        {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-01",
         "shift_start": "25:00"},
    ],
)
def test_create_employee_validation_422(http, payload):
    assert http.post("/employees", json=payload).status_code == 422


@pytest.mark.parametrize("bad", [1783312500, 1783312500000.0, "1783312500000", True, -5, 10**15])
def test_bad_timestamps_422(http, bad):
    assert http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422


def test_punch_in_rejects_absent_status(http):
    r = http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "url",
    [
        "/employees?page=0", "/employees?page_size=101", "/attendance?page_size=0",
        "/attendance?date_from=2026-07-10&date_to=2026-07-01", "/attendance?date_from=2026-13-01",
        "/attendance?status=BOGUS", "/analytics/departments/summary", "/analytics/departments/summary?month=2026-13",
        "/analytics/leaderboard/late?month=2026-07&limit=51", "/analytics/leaderboard/late?month=2026-07&limit=0",
        "/analytics/departments/Sales/trend?from=2026-07-10&to=2026-07-01",
        "/analytics/departments/Sales/trend?from=2026-01-01&to=2026-12-31",
        "/analytics/departments/Sales/trend?from=2026-07-01",
        "/admin/explain/nonsense", "/admin/explain/employee_monthly", "/admin/explain/department_summary",
        "/admin/explain/department_trend?department=Sales",
    ],
)
def test_query_validation_422(http, url):
    assert http.get(url).status_code == 422


def test_patch_body_validation_422(http):
    url = "/attendance/EMP0001/2026-07-06"
    assert http.patch(url, json={"reason": "abc", "regularized_by": "hr"}).status_code == 422  # reason too short
    assert http.patch(url, json={"reason": "valid reason", "regularized_by": "hr", "punch_in": None}).status_code == 422
    assert http.patch("/attendance/EMP0001/2026-02-30", json={"reason": "valid reason", "regularized_by": "hr"}).status_code == 422
