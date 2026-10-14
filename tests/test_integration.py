"""Integration tests. They need a running MongoDB AND a running server that uses the SAME test database.

    # terminal 1  (database name must end with _test: the tests wipe it)
    MONGO_DB=attendance_test uvicorn app.main:app --port 8000
    # terminal 2
    MONGO_DB=attendance_test pytest tests/test_integration.py -q

BASE_URL (default http://localhost:8000) points at the server.
"""
import os
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
import pytest
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
BASE = os.getenv("BASE_URL", "http://localhost:8000")
DB_NAME = os.getenv("MONGO_DB", "attendance_db")
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

pytestmark = pytest.mark.skipif(not DB_NAME.endswith("_test"), reason="set MONGO_DB=attendance_test (name must end with _test)")


@pytest.fixture(scope="module")
def mdb():
    return MongoClient(os.getenv("MONGO_URI") or "mongodb://localhost:27017", tz_aware=True)[DB_NAME]


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=BASE, timeout=30) as c:
        try:
            assert c.get("/health").status_code == 200
        except Exception:
            pytest.skip("server not reachable at " + BASE)
        yield c


@pytest.fixture(autouse=True)
def _clean(mdb, api):
    mdb.employees.delete_many({})
    mdb.attendance_logs.delete_many({})


def ms(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000


def mk(api, code, dept="Eng", start="09:30", end="18:30", joined="2026-01-05", name="N"):
    r = api.post("/employees", json={"emp_code": code, "name": name + code, "email": f"{code}@x.io",
                                     "department": dept, "shift_start": start, "shift_end": end, "joined_on": joined})
    assert r.status_code == 201, r.text
    return r.json()


def r2(x, p=2):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- employees
def test_employee_crud(api):
    e = mk(api, "EMP0001")
    assert set(e) == {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}
    assert isinstance(e["created_at"], int) and e["created_at"] > 10**11
    assert api.post("/employees", json={"emp_code": "EMP0001", "name": "x", "email": "a@b.co",
                                        "department": "Eng", "joined_on": "2026-01-01"}).status_code == 409
    for i in range(2, 8):
        mk(api, f"EMP000{i}", dept="Ops" if i % 2 else "Eng")
    page = api.get("/employees", params={"department": "Eng", "page": 2, "page_size": 2}).json()
    assert page["total"] == 4 and [x["emp_code"] for x in page["items"]] == ["EMP0004", "EMP0006"]
    assert [x["emp_code"] for x in api.get("/employees", params={"page_size": 3}).json()["items"]] == ["EMP0001", "EMP0002", "EMP0003"]


def test_concurrent_employee_create_one_winner(api):
    codes = []
    def go():
        codes.append(api.post("/employees", json={"emp_code": "EMP0009", "name": "x", "email": "a@b.co",
                                                  "department": "Eng", "joined_on": "2026-01-01"}).status_code)
    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(codes).count(201) == 1 and codes.count(409) == 11


# --------------------------------------------------------------------------- punch in / out
def test_punch_flow_and_rules(api):
    mk(api, "EMP0001")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 1) + 900})
    assert r.status_code == 201
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 10 and b["punch_in"] == ms(2026, 7, 6, 9, 40, 1)
    assert b["punch_out"] is None and b["work_hours"] is None and b["history"] == []
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 12)}).status_code == 409
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE1", "punched_at": ms(2026, 7, 6, 12)}).status_code == 404
    out = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10, 30)})
    assert out.status_code == 200
    o = out.json()
    assert o["overtime_minutes"] == 40 and o["half_day"] is False and o["work_hours"] == 9.51
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 20)}).status_code == 409
    assert api.post("/attendance/punch-out", json={"emp_code": "NOPE1"}).status_code == 404


def test_punch_out_validation_and_half_day(api):
    mk(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30), "status": "WFH"})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 8, 9, 31)}).status_code == 422
    o = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 13)}).json()
    assert o["work_hours"] == 3.5 and o["half_day"] is True and o["overtime_minutes"] == 0 and o["status"] == "WFH"


def test_overnight_shift(api):
    mk(api, "EMP0005", dept="Support", start="22:00", end="06:00")
    b = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 1, 0)}).json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 180
    o = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)}).json()
    assert o["overtime_minutes"] == 40


def test_concurrent_punch_in_and_out(api):
    mk(api, "EMP0001")
    res = []
    def pin():
        res.append(api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}).status_code)
    ts = [threading.Thread(target=pin) for _ in range(15)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert res.count(201) == 1 and res.count(409) == 14
    res.clear()
    def pout():
        res.append(api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}).status_code)
    ts = [threading.Thread(target=pout) for _ in range(15)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert res.count(200) == 1 and res.count(409) == 14


# --------------------------------------------------------------------------- regularize
def test_regularize(api):
    mk(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    url = "/attendance/EMP0001/2026-07-07"
    base = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = api.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 9, 28), "late_minutes": 999})
    assert r.status_code == 200
    b = r.json()
    assert b["late_minutes"] == 0 and len(b["history"]) == 1
    ch = b["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"}
    assert ch["punch_in"]["from"] == ms(2026, 7, 7, 10, 5) and ch["late_minutes"] == {"from": 35, "to": 0}
    assert api.patch(url, json={**base, "punch_in": ms(2026, 7, 7, 9, 28)}).status_code == 422  # changes nothing
    assert api.patch(url, json={**base, "punch_in": ms(2026, 7, 8, 9, 28)}).status_code == 422  # other attendance date
    assert api.patch(url, json={**base, "punch_out": ms(2026, 7, 7, 9, 0)}).status_code == 422  # before punch_in
    assert api.patch(url, json={**base, "status": "LEAVE", "punch_in": ms(2026, 7, 7, 9)}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    assert api.patch("/attendance/NOPE1/2026-07-07", json={**base, "status": "LEAVE"}).status_code == 404
    leave = api.patch(url, json={**base, "status": "LEAVE"}).json()
    assert leave["punch_in"] is None and leave["punch_out"] is None and leave["work_hours"] is None
    assert len(leave["history"]) == 2 and leave["history"][1]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}
    back = api.patch(url, json={**base, "status": "WFH", "punch_in": ms(2026, 7, 7, 9, 30)}).json()
    assert back["status"] == "WFH" and len(back["history"]) == 3


def test_concurrent_regularize_never_loses_history(api, mdb):
    mk(api, "EMP0001")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    res = []
    def patch(i):
        res.append(api.patch("/attendance/EMP0001/2026-07-07", json={
            "reason": "concurrent edit", "regularized_by": f"u{i}", "punch_in": ms(2026, 7, 7, 9, i)}).status_code)
    ts = [threading.Thread(target=patch, args=(i,)) for i in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    doc = mdb.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert res.count(200) == len(doc["history"]) and set(res) <= {200, 409}


# --------------------------------------------------------------------------- analytics vs. a python oracle
JULY = ("2026-07-01", "2026-07-31")


def seed_awkward(api, mdb):
    mk(api, "EMP0001", "Eng", joined="2026-01-05")
    mk(api, "EMP0002", "Eng", joined="2026-07-13")          # mid-month joiner
    mk(api, "EMP0003", "Sales", joined="2026-02-01")
    mk(api, "EMP0004", "Sales", joined="2026-02-01")
    mk(api, "EMP0005", "Support", start="22:00", end="06:00", joined="2026-03-10")
    mk(api, "EMP0006", "Support", joined="2026-03-10")      # never has a log
    mk(api, "EMP0007", "Ops", joined="2026-08-15")          # joins after July
    rows = []
    def log(code, d, status="PRESENT", late=0, ot=0, wh=None, half=False, pin=True):
        t = datetime.fromisoformat(d + "T09:30:00+05:30").astimezone(UTC)
        rows.append({"emp_code": code, "date": d, "status": status,
                     "punch_in": t if pin and status in ("PRESENT", "WFH", "ON_DUTY") else None,
                     "punch_out": None, "work_hours": wh, "late_minutes": late, "overtime_minutes": ot,
                     "half_day": half, "history": []})
    for day in range(1, 32):
        d = date(2026, 7, day)
        if d.weekday() < 5 and day % 4 != 0:               # day%4==0 weekdays are 'missing' (no record at all)
            log("EMP0001", d.isoformat(), late=15 if day % 3 == 0 else 0, ot=40 if day % 5 == 0 else 0, wh=8.5 + day / 100)
    log("EMP0001", "2026-07-04", late=20, ot=45, wh=2.0, half=True)         # Saturday record
    log("EMP0001", "2026-07-05", status="ON_DUTY", late=0, wh=7.0)           # Sunday ON_DUTY
    for day in (14, 15, 16):
        log("EMP0002", f"2026-07-{day}", late=30, wh=9.0)                    # ties with EMP0003/4 below
    log("EMP0002", "2026-07-17", status="LEAVE")
    log("EMP0003", "2026-07-06", late=45, wh=9.0); log("EMP0003", "2026-07-07", late=45, wh=4.0, half=True)
    log("EMP0004", "2026-07-06", late=90, wh=8.0)
    log("EMP0004", "2026-07-07", status="ABSENT"); log("EMP0004", "2026-07-08", status="WFH", wh=8.25)
    log("EMP0005", "2026-07-06", late=5, wh=9.1)
    log("EMP0006", "2026-06-30", late=999)                                   # June: outside July
    log("GHOST99", "2026-07-06", late=500, wh=9.0)                           # unknown employee, must be ignored
    mdb.attendance_logs.insert_many(rows)
    return rows


def present(r):  # R6
    return r["status"] in ("PRESENT", "WFH", "ON_DUTY")


def dow(r):
    return date.fromisoformat(r["date"]).weekday()


def weekdays(a, b):
    return sum(1 for i in range((b - a).days + 1) if (a + timedelta(i)).weekday() < 5)


def month_rows(rows, lo, hi, code=None):
    return [r for r in rows if lo <= r["date"] <= hi and (code is None or r["emp_code"] == code)]


def test_employee_monthly_matches_oracle(api, mdb):
    rows = seed_awkward(api, mdb)
    for code, joined in [("EMP0001", date(2026, 1, 5)), ("EMP0002", date(2026, 7, 13)), ("EMP0006", date(2026, 3, 10)), ("EMP0007", date(2026, 8, 15))]:
        got = api.get(f"/analytics/employees/{code}/monthly", params={"month": "2026-07"}).json()
        rs = month_rows(rows, *JULY, code)
        wd = weekdays(max(date(2026, 7, 1), joined), date(2026, 7, 31)) if joined <= date(2026, 7, 31) else 0
        pdays = sum((0.5 if r["half_day"] else 1) for r in rs if present(r) and dow(r) < 5)
        assert got["working_days"] == wd and got["present_days"] == pdays
        assert got["leave_days"] == sum(r["status"] == "LEAVE" for r in rs)
        assert got["late_count"] == sum(r["late_minutes"] > 0 for r in rs)
        assert got["total_late_minutes"] == sum(r["late_minutes"] for r in rs)
        assert got["total_overtime_minutes"] == sum(r["overtime_minutes"] for r in rs)
        assert got["attendance_pct"] == (None if wd == 0 else r2(Decimal(str(pdays)) / wd * 100))
    assert api.get("/analytics/employees/NOPE1/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_summary_matches_oracle(api, mdb):
    rows = seed_awkward(api, mdb)
    emps = {e["emp_code"]: e for e in mdb.employees.find()}
    got = {i["department"]: i for i in api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    assert list(got) == sorted(got) and "Ops" not in got            # Ops: headcount 0 in July -> omitted
    for dept in ("Eng", "Sales", "Support"):
        codes = {c for c, e in emps.items() if e["department"] == dept and e["joined_on"] <= JULY[1]}
        rs = [r for r in month_rows(rows, *JULY) if r["emp_code"] in codes]
        wh = [r["work_hours"] for r in rs if present(r) and r["work_hours"] is not None]
        g = got[dept]
        assert g["headcount"] == len(codes)
        assert g["present_days"] == sum((0.5 if r["half_day"] else 1) for r in rs if present(r) and dow(r) < 5)
        assert g["avg_work_hours"] == (r2(Decimal(str(sum(wh))) / len(wh)) if wh else None)
        assert g["late_count"] == sum(r["late_minutes"] > 0 for r in rs)
        assert g["total_late_minutes"] == sum(r["late_minutes"] for r in rs)
        assert g["leave_count"] == sum(r["status"] == "LEAVE" for r in rs)
        assert g["on_duty_count"] == sum(r["status"] == "ON_DUTY" for r in rs)
    assert got["Support"]["headcount"] == 2                          # EMP0006 has no usable logs but still counts
    one = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()["items"]
    assert [i["department"] for i in one] == ["Sales"]


def test_leaderboard_ties_and_limit(api, mdb):
    seed_awkward(api, mdb)
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 50}).json()["items"]
    tot = defaultdict(int)
    for r in mdb.attendance_logs.find({"date": {"$gte": JULY[0], "$lte": JULY[1]}}):
        if r["emp_code"] != "GHOST99":
            tot[r["emp_code"]] += r["late_minutes"]
    ordered = sorted(((c, t) for c, t in tot.items() if t > 0), key=lambda x: (-x[1], x[0]))
    ranks, prev, rank = [], None, 0
    for i, (c, t) in enumerate(ordered, 1):
        rank = rank if t == prev else i
        prev = t
        ranks.append((rank, c, t))
    assert [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in items] == ranks
    assert "GHOST99" not in [i["emp_code"] for i in items]
    # tie at the cutoff: everyone with rank <= limit comes back, so the list can exceed `limit`
    tied_rank = [r for r in ranks if r[0] == ranks[-1][0] or sum(1 for x in ranks if x[2] == r[2]) > 1]
    cut = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [i["rank"] for i in cut] == [r for r, _, _ in ranks if r <= 2] and tied_rank is not None
    dept = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Sales"}).json()["items"]
    assert [i["rank"] for i in dept][0] == 1 and {i["department"] for i in dept} == {"Sales"}


def test_trend_matches_oracle(api, mdb):
    rows = seed_awkward(api, mdb)
    r = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-31"})
    items = r.json()["items"]
    assert len(items) == 31 and [i["date"] for i in items] == [f"2026-07-{d:02d}" for d in range(1, 32)]
    rates = []
    for i in items:
        d = date.fromisoformat(i["date"])
        rs = [x for x in rows if x["date"] == i["date"] and x["emp_code"] in ("EMP0001", "EMP0002")]
        head = 1 + (1 if d >= date(2026, 7, 13) else 0)
        pc = sum((0.5 if x["half_day"] else 1) for x in rs if present(x))
        assert i["is_working_day"] == (d.weekday() < 5) and i["headcount"] == head and i["present_count"] == pc
        assert i["late_count"] == sum(x["late_minutes"] > 0 for x in rs)
        rate = pc / head if d.weekday() < 5 and head else None
        assert i["attendance_rate"] == (None if rate is None else r2(Decimal(str(pc)) / head, 4))
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        exp = r2(Decimal(str(sum(window) / len(window))), 4) if window else None
        assert i["moving_avg_7d"] == exp, i["date"]
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-05"}).status_code == 404
    empty = api.get("/analytics/departments/Support/trend", params={"from": "2026-12-01", "to": "2026-12-07"}).json()["items"]
    assert len(empty) == 7 and empty[0]["attendance_rate"] == 0.0 and empty[5]["attendance_rate"] is None  # Sat


def test_attendance_list_sort_filter_pagination(api, mdb):
    seed_awkward(api, mdb)
    r = api.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page_size": 100}).json()
    keys = [(i["date"], i["emp_code"]) for i in r["items"]]
    assert keys == sorted(keys, key=lambda k: (-int(k[0].replace("-", "")), k[1])) and r["total"] == len(keys)
    p = api.get("/attendance", params={"status": "LEAVE"}).json()
    assert p["total"] == 1 and p["items"][0]["punch_in"] is None and p["items"][0]["history"] == []
    assert api.get("/attendance", params={"page": 9999999999999999999}).json()["items"] == []
    assert "id" not in p["items"][0] and "_id" not in p["items"][0]


def test_legacy_documents_get_defaults(api, mdb):
    mk(api, "EMP0002")
    mdb.attendance_logs.insert_one({"emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
                                    "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC),
                                    "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC), "work_hours": 8.98})
    got = api.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
    assert got["half_day"] is False and got["history"] == [] and got["late_minutes"] == 0 and got["overtime_minutes"] == 0
    ok = api.patch("/attendance/EMP0002/2026-07-14", json={"reason": "legacy fix", "regularized_by": "hr", "status": "WFH"})
    assert ok.status_code == 200 and len(ok.json()["history"]) == 1


# --------------------------------------------------------------------------- explain
def walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield k, v
            yield from walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from walk(v)


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "ABSENT", "page": 2}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Eng"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 5}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_index_and_no_collscan(api, mdb, endpoint, params):
    seed_awkward(api, mdb)
    r = api.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    stages = {v for k, v in walk(body["explain"]) if k == "stage" and isinstance(v, str)}
    assert "IXSCAN" in stages and "COLLSCAN" not in stages, stages
