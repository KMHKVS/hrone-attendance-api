"""Seed ~100,000 attendance records and time every endpoint (also checks the explain plans).

    python tests/bench_100k.py seed            # fills database attendance_bench (wipes it first)
    MONGO_DB=attendance_bench python -m uvicorn app.main:app --port 8001     # builds the indexes on 100k docs at startup
    python tests/bench_100k.py run             # times the endpoints, checks IXSCAN / no COLLSCAN

Env: MONGO_URI (default mongodb://localhost:27017), BASE_URL (default http://localhost:8001)
"""
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from pymongo import MongoClient

URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
BASE = os.getenv("BASE_URL", "http://localhost:8001")
DB = "attendance_bench"
UTC = timezone.utc
DEPTS = ["Engineering", "Sales", "Support", "Finance", "HR", "Ops", "Legal", "Design", "QA", "Data"]


def seed():
    db = MongoClient(URI, tz_aware=True)[DB]
    db.employees.drop()
    db.attendance_logs.drop()
    rnd = random.Random(7)
    emps = []
    for i in range(1, 1001):
        emps.append({"emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "email": f"e{i}@x.io",
                     "department": DEPTS[i % len(DEPTS)], "shift_start": "09:30", "shift_end": "18:30",
                     "joined_on": "2026-01-05" if i % 10 else "2026-07-15",
                     "created_at": datetime(2026, 1, 1, tzinfo=UTC)})
    db.employees.insert_many(emps)
    logs, d = [], date(2026, 6, 1)
    while d <= date(2026, 9, 30):
        if d.weekday() < 5:
            for e in emps:
                r = rnd.random()
                if r < 0.03:
                    status, pin, pout, late, ot, wh, half = "LEAVE", None, None, 0, 0, None, False
                elif r < 0.05:
                    status, pin, pout, late, ot, wh, half = "ABSENT", None, None, 0, 0, None, False
                else:
                    status = rnd.choice(["PRESENT"] * 8 + ["WFH", "ON_DUTY"])
                    late = rnd.choice([0] * 6 + [12, 25, 45, 70])
                    start = datetime.fromisoformat(d.isoformat() + "T09:30:00+05:30").astimezone(UTC)
                    pin = start + timedelta(minutes=late, seconds=rnd.randint(0, 59))
                    hours = rnd.choice([3.5, 8.0, 8.5, 9.0, 9.75])
                    pout = pin + timedelta(hours=hours)
                    wh, half = round(hours, 2), hours < 4.5
                    ot = rnd.choice([0, 0, 0, 35, 50])
                logs.append({"emp_code": e["emp_code"], "date": d.isoformat(), "status": status, "punch_in": pin,
                             "punch_out": pout, "work_hours": wh, "late_minutes": late, "overtime_minutes": ot,
                             "half_day": half, "history": []})
        d += timedelta(days=1)
    for i in range(0, len(logs), 20000):
        db.attendance_logs.insert_many(logs[i:i + 20000])
    print(f"seeded {len(emps)} employees, {len(logs)} attendance records into '{DB}' (no indexes yet)")


def stages(x, out=None):
    out = set() if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            if k == "stage" and isinstance(v, str):
                out.add(v)
            stages(v, out)
    elif isinstance(x, list):
        for v in x:
            stages(v, out)
    return out


def run():
    cases = [
        ("attendance_list", "/attendance", "attendance_list", {}),
        ("attendance_list (page 500)", "/attendance", "attendance_list", {"page": 500}),
        ("attendance_list emp+dates", "/attendance", "attendance_list",
         {"emp_code": "EMP0042", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("attendance_list status", "/attendance", "attendance_list", {"status": "LEAVE", "page": 3}),
        ("employee_monthly", "/analytics/employees/EMP0042/monthly", "employee_monthly",
         {"emp_code": "EMP0042", "month": "2026-07"}),
        ("department_summary", "/analytics/departments/summary", "department_summary", {"month": "2026-07"}),
        ("department_summary Sales", "/analytics/departments/summary", "department_summary",
         {"month": "2026-07", "department": "Sales"}),
        ("late_leaderboard", "/analytics/leaderboard/late", "late_leaderboard", {"month": "2026-07", "limit": 10}),
        ("department_trend 92d", "/analytics/departments/Sales/trend", "department_trend",
         {"department": "Sales", "from": "2026-07-01", "to": "2026-09-30"}),
    ]
    bad = 0
    with httpx.Client(base_url=BASE, timeout=120) as c:
        assert c.get("/health").status_code == 200, "server not reachable / not healthy"
        print(f"{'endpoint':34}{'status':>7}{'ms':>9}   explain")
        for name, path, ep, params in cases:
            t = time.perf_counter()
            r = c.get(path, params={k: v for k, v in params.items() if k != "emp_code" or "monthly" not in path})
            ms = (time.perf_counter() - t) * 1000
            ex = c.get(f"/admin/explain/{ep}", params=params)
            st = stages(ex.json().get("explain", {})) if ex.status_code == 200 else {"explain-failed"}
            ok = r.status_code == 200 and ex.status_code == 200 and "IXSCAN" in st and "COLLSCAN" not in st
            bad += not ok
            print(f"{name:34}{r.status_code:>7}{ms:>9.0f}   {'OK ' if ok else 'BAD'} {sorted(st)}")
        t = time.perf_counter()
        c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1893456000000})
        print(f"{'punch-in':34}{'':>7}{(time.perf_counter() - t) * 1000:>9.0f}")
    print("ALL GOOD" if not bad else f"{bad} problem(s) - see BAD lines")


if __name__ == "__main__":
    {"seed": seed, "run": run}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
