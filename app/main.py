"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read, real environment variables win)

Everything lives in this one file, in this order:
  1. configuration and constants
  2. time helpers and the business rules R1-R5 (pure functions, unit-tested)
  3. request models, serializers, error helpers
  4. query / pipeline builders (shared by the real endpoints and /admin/explain)
  5. endpoints
"""
import calendar
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, NoReturn, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration and constants
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes, never naive ones.
client = MongoClient(
    os.getenv("MONGO_URI") or "mongodb://localhost:27017",
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB") or "attendance_db"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MS_MIN, MS_MAX = 100_000_000_000, 4_102_444_800_000  # epoch-millisecond window (openapi.yaml)
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_TREND_DAYS = 92
MIN_YEAR, MAX_YEAR = 1900, 2200  # sanity window for calendar input
MAX_SKIP = 10**12  # a huge page number must give an empty page, not a driver overflow

DEFAULT_SHIFT_START, DEFAULT_SHIFT_END = "09:30", "18:30"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NO_PUNCH = ("ABSENT", "LEAVE")

DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: floats, strings and booleans are rejected; the range rejects "seconds" values.
EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]

# Index names (explicit, so queries can be pinned to them with hint()).
IDX_EMP_UQ = "uq_emp_code"
IDX_EMP_DEPT = "department_emp_code"
IDX_EMP_JOINED = "joined_on"
IDX_EMP_DEPT_JOINED = "department_joined_on"
IDX_LOG_UQ = "uq_emp_code_date"
IDX_LOG_DATE = "date_desc_emp_code"
IDX_LOG_STATUS = "status_date_desc_emp_code"
IDX_LOG_PUNCH = "emp_code_punch_in_desc"

_indexes_ready = False
_index_lock = threading.Lock()


def ensure_indexes() -> None:
    """Create every index the service relies on. Idempotent; safe to call repeatedly."""
    global _indexes_ready
    if _indexes_ready:
        return
    with _index_lock:
        if _indexes_ready:
            return
        emp, logs = db.employees, db.attendance_logs
        emp.create_index([("emp_code", ASCENDING)], unique=True, name=IDX_EMP_UQ)
        emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name=IDX_EMP_DEPT)
        emp.create_index([("joined_on", ASCENDING)], name=IDX_EMP_JOINED)
        emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name=IDX_EMP_DEPT_JOINED)
        logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name=IDX_LOG_UQ)
        logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name=IDX_LOG_DATE)
        logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name=IDX_LOG_STATUS
        )
        logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name=IDX_LOG_PUNCH)
        _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # MongoDB may still be starting; /health retries
        log.warning("index creation deferred: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules (pure functions)
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Normalise to aware UTC and drop sub-second precision (R1: whole seconds)."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (integer maths, no floats)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def hhmm_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_minutes(shift_end) <= hhmm_minutes(shift_start)


def at_shift_time(day: Date, hhmm: str, plus_days: int = 0) -> datetime:
    minutes = hhmm_minutes(hhmm)
    return datetime.combine(day, dtime(minutes // 60, minutes % 60), tzinfo=IST) + timedelta(days=plus_days)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch belongs to the previous day."""
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < hhmm_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, att_date: Date, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((as_utc(punch_in) - at_shift_time(att_date, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, att_date: Date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end (next calendar day for overnight shifts), only if >= 30."""
    end = at_shift_time(att_date, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    seconds = int((as_utc(punch_out) - end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def round_half_up(value: Any, places: int) -> Decimal:
    """R8 rounding. Python's round() and MongoDB's $round are banker's rounding, so neither is used for output."""
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    seconds = int((as_utc(punch_out) - as_utc(punch_in)).total_seconds())
    return float(round_half_up(Decimal(seconds) / Decimal(3600), 2))


def is_half_day(work_hours: float) -> bool:
    """R5: judged on the already-rounded work_hours."""
    return Decimal(str(work_hours)) < HALF_DAY_LIMIT


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    att_date: Date,
    shift_start: str,
    shift_end: str,
) -> dict:
    """All stored derived values for a record (DATA_MODEL.md section 3 table)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status in NO_PUNCH or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, att_date, shift_start)
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, att_date, shift_start, shift_end)
        out["half_day"] = is_half_day(out["work_hours"])
    return out


def count_weekdays(start: Date, end: Date) -> int:
    """Mon-Fri days in [start, end] (R7). Closed form: whole weeks * 5 plus the leftover days."""
    if end < start:
        return 0
    full_weeks, extra = divmod((end - start).days + 1, 7)
    return full_weeks * 5 + sum(1 for i in range(extra) if (start + timedelta(days=i)).weekday() < 5)


def clean_float(value: float) -> Decimal:
    """A float produced by MongoDB division/averaging, with binary noise removed, ready for half-up rounding."""
    return Decimal(repr(round(value, 10)))


def parse_date(value: str) -> Date:
    day = Date.fromisoformat(value)  # raises ValueError for 2026-02-30 and similar
    if not MIN_YEAR <= day.year <= MAX_YEAR:
        raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}")
    return day


# --------------------------------------------------------------------------- #
# 3. Models, serializers, error helpers
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default=DEFAULT_SHIFT_START, pattern=HHMM_PATTERN)
    shift_end: str = Field(default=DEFAULT_SHIFT_END, pattern=HHMM_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("name", "department")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_calendar_date(cls, v: str) -> str:
        parse_date(v)
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    """Derived fields are not declared, so a client that sends them is simply ignored."""

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


def invalid(loc: str, msg: str, value: Any = None, where: str = "query") -> NoReturn:
    """Raise a 422 with FastAPI's standard validation body."""
    raise RequestValidationError([{"type": "value_error", "loc": (where, loc), "msg": msg, "input": value}])


def query_date(name: str, value: Optional[str], where: str = "query") -> Optional[Date]:
    if value is None:
        return None
    try:
        return parse_date(value)
    except ValueError as exc:
        invalid(name, str(exc), value, where)


def month_range(month: str) -> tuple[Date, Date]:
    year, mon = int(month[:4]), int(month[5:7])
    if not MIN_YEAR <= year <= MAX_YEAR:
        invalid("month", f"year must be between {MIN_YEAR} and {MAX_YEAR}", month)
    return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])


def trend_range(from_s: str, to_s: str) -> tuple[Date, Date]:
    d_from, d_to = query_date("from", from_s), query_date("to", to_s)
    if d_to < d_from:
        invalid("to", "'to' must not be before 'from'", to_s)
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        invalid("to", f"range must not exceed {MAX_TREND_DAYS} days", to_s)
    return d_from, d_to


def to_api(value: Any) -> Any:
    return dt_to_ms(value) if isinstance(value, datetime) else value


def serialize_employee(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start", DEFAULT_SHIFT_START),
        "shift_end": doc.get("shift_end", DEFAULT_SHIFT_END),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(created) if created else None,
    }


def serialize_attendance(doc: dict) -> dict:
    """Stored document -> API shape. Applies the legacy defaults, never exposes _id."""
    history = [
        {
            "at": to_api(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {
                field: {"from": to_api(change.get("from")), "to": to_api(change.get("to"))}
                for field, change in (h.get("changes") or {}).items()
            },
        }
        for h in (doc.get("history") or [])
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_api(doc.get("punch_in")),
        "punch_out": to_api(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def shifts_of(emp: dict) -> tuple[str, str]:
    return emp.get("shift_start", DEFAULT_SHIFT_START), emp.get("shift_end", DEFAULT_SHIFT_END)


# --------------------------------------------------------------------------- #
# 4. Query and pipeline builders (used by the endpoints AND by /admin/explain)
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions. They read the STORED derived values (never recompute them).
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
LATE_FLAG = {"$cond": [{"$gt": [LATE_MINUTES, 0]}, 1, 0]}
IS_PRESENT = {"$in": ["$status", PRESENCE]}
# present-day weight in half-days: 2 = full day, 1 = half day; Mon-Fri only (R5-R7)
PRESENT_HALVES = {
    "$cond": [
        {"$and": [IS_PRESENT, {"$lte": ["$dow_", 5]}]},
        {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]},
        0,
    ]
}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
# work_hours in whole hundredths: summing integers keeps the average exact (no float drift)
WORK_HUNDREDTHS = {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}
DOW_STAGE = {
    "$addFields": {
        "dow_": {
            "$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}
        }
    }
}
LOG_STATS = {
    "present_halves": {"$sum": PRESENT_HALVES},
    "late_count": {"$sum": LATE_FLAG},
    "total_late_minutes": {"$sum": LATE_MINUTES},
    "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    "wh_hundredths": {"$sum": {"$cond": [HAS_WORK_HOURS, WORK_HUNDREDTHS, 0]}},
    "wh_count": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
}


def build_attendance_find(emp_code, date_from, date_to, status, page, page_size) -> dict:
    d_from, d_to = query_date("date_from", date_from), query_date("date_to", date_to)
    if d_from and d_to and d_from > d_to:
        invalid("date_from", "date_from must not be after date_to", date_from)
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        flt["date"] = {}
        if date_from is not None:
            flt["date"]["$gte"] = date_from
        if date_to is not None:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    if emp_code is not None:
        hint = IDX_LOG_UQ
    elif status is not None:
        hint = IDX_LOG_STATUS
    else:
        hint = IDX_LOG_DATE
    return {
        "filter": flt,
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": min((page - 1) * page_size, MAX_SKIP),
        "limit": page_size,
        "hint": hint,
    }


def monthly_pipeline(emp_code: str, first: Date, last: Date) -> tuple[list, str]:
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        DOW_STAGE,
        {"$group": {"_id": None, **LOG_STATS}},
    ]
    return pipeline, IDX_LOG_UQ


def summary_pipeline(first: Date, last: Date, department: Optional[str]) -> tuple[list, str]:
    """Starts from EMPLOYEES so that people with zero logs still count (R9), then joins their month of logs."""
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    pipeline = [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    DOW_STAGE,
                    {"$group": {"_id": None, **LOG_STATS}},
                ],
                "as": "logs",
            }
        },
        {"$set": {"s": {"$first": "$logs"}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{name: {"$sum": {"$ifNull": [f"$s.{name}", 0]}} for name in LOG_STATS},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    return pipeline, (IDX_EMP_DEPT_JOINED if department is not None else IDX_EMP_JOINED)


def leaderboard_pipeline(first: Date, last: Date, limit: int, department: Optional[str]) -> tuple[list, str]:
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); limit is applied AFTER ranking
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline, IDX_LOG_DATE


def trend_inputs(department: str, d_from: Date, d_to: Date) -> dict:
    """Small index-backed lookups on employees that the trend pipeline needs as constants."""
    dept = {"department": department}
    return {
        "total": db.employees.count_documents(dept),
        "codes": [e["emp_code"] for e in db.employees.find(dept, {"_id": 0, "emp_code": 1})],
        "base": db.employees.count_documents({**dept, "joined_on": {"$lte": d_from.isoformat()}}),
        "joins": [
            e["joined_on"]
            for e in db.employees.find(
                {**dept, "joined_on": {"$gt": d_from.isoformat(), "$lte": d_to.isoformat()}},
                {"_id": 0, "joined_on": 1},
            )
        ],
    }


def trend_pipeline(inputs: dict, d_from: Date, d_to: Date) -> tuple[list, str]:
    """One row per calendar day (gap filling), headcount per day and a 7-row moving average, all in MongoDB."""
    n_days = (d_to - d_from).days + 1
    first_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": inputs["codes"]},
                "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()},
            }
        },
        # $facet returns exactly one document even when no log matched, so the calendar below is never empty
        {
            "$facet": {
                "byday": [
                    {
                        "$group": {
                            "_id": "$date",
                            "present_halves": {
                                "$sum": {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]}
                            },
                            "late_count": {"$sum": LATE_FLAG},
                        }
                    }
                ]
            }
        },
        {
            "$project": {
                "_id": 0,
                "rows": {
                    "$map": {
                        "input": {"$range": [0, n_days]},
                        "as": "i",
                        "in": {
                            "$let": {
                                "vars": {"dt": {"$add": [first_dt, {"$multiply": ["$$i", 86_400_000]}]}},
                                "in": {
                                    "$let": {
                                        "vars": {
                                            "ds": {"$dateToString": {"date": "$$dt", "format": "%Y-%m-%d"}},
                                            "dow": {"$isoDayOfWeek": "$$dt"},
                                        },
                                        "in": {
                                            "$let": {
                                                "vars": {
                                                    "e": {
                                                        "$first": {
                                                            "$filter": {
                                                                "input": "$byday",
                                                                "as": "x",
                                                                "cond": {"$eq": ["$$x._id", "$$ds"]},
                                                            }
                                                        }
                                                    }
                                                },
                                                "in": {
                                                    "date": "$$ds",
                                                    "dow": "$$dow",
                                                    "headcount": {
                                                        "$add": [
                                                            inputs["base"],
                                                            {
                                                                "$size": {
                                                                    "$filter": {
                                                                        "input": {"$literal": inputs["joins"]},
                                                                        "as": "j",
                                                                        "cond": {"$lte": ["$$j", "$$ds"]},
                                                                    }
                                                                }
                                                            },
                                                        ]
                                                    },
                                                    "present_halves": {"$ifNull": ["$$e.present_halves", 0]},
                                                    "late_count": {"$ifNull": ["$$e.late_count", 0]},
                                                },
                                            }
                                        },
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {
            "$addFields": {
                "is_working_day": {"$lte": ["$dow", 5]},
                "rate": {
                    "$cond": [
                        {"$and": [{"$lte": ["$dow", 5]}, {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_halves", {"$multiply": [2, "$headcount"]}]},
                        None,
                    ]
                },
            }
        },
        # $avg ignores null rates; documents [-6, 0] = this row and up to 6 earlier rows of the range
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_halves": 1,
                "late_count": 1,
                "rate": 1,
                "moving": 1,
            }
        },
    ]
    return pipeline, IDX_LOG_UQ


# --------------------------------------------------------------------------- #
# 5. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    try:
        ensure_indexes()
    except Exception as exc:
        log.warning("index creation failed: %s", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the guarantee, not a prior lookup
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {} if department is None else {"department": department}
    total = db.employees.count_documents(flt)
    cursor = (
        db.employees.find(flt, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(min((page - 1) * page_size, MAX_SKIP))
        .limit(page_size)
    )
    return {"items": [serialize_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    start, end = shifts_of(emp)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, start, end)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(body.status, ts, None, day, start, end),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): of N parallel requests exactly one wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    start, end = shifts_of(emp)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
        hint=IDX_LOG_PUNCH,
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = as_utc(rec["punch_in"])
    if ts <= pin:
        invalid("punched_at", "punched_at must be after the punch-in", body.punched_at, "body")
    if ts - pin > timedelta(hours=24):
        invalid("punched_at", "punched_at is more than 24 hours after the punch-in", body.punched_at, "body")
    derived = derive_fields(rec["status"], pin, ts, Date.fromisoformat(rec["date"]), start, end)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # compare-and-set: only the first closer matches
        {
            "$set": {
                "punch_out": ts,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    cursor = db.attendance_logs.find(
        spec["filter"], sort=spec["sort"], skip=spec["skip"], limit=spec["limit"], hint=spec["hint"]
    )
    return {"items": [serialize_attendance(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: Annotated[str, Path(pattern=DATE_PATTERN)], body: RegularizeIn):
    day = query_date("date", date, "path")
    emp = get_employee_or_404(emp_code)
    start, end = shifts_of(emp)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    sent_in, sent_out = "punch_in" in sent, "punch_out" in sent
    if body.status is None and not sent_in and not sent_out:
        invalid("body", "nothing to change", None, "body")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status if body.status is not None else old_status
    if new_status in NO_PUNCH:
        if sent_in or sent_out:
            invalid("body", f"{new_status} clears the punch times; do not send punch_in/punch_out", None, "body")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if sent_in else old_in
        new_out = ms_to_dt(body.punch_out) if sent_out else old_out
        if new_in is None:
            invalid("punch_in", f"status {new_status} requires a punch_in", None, "body")
        if sent_in and attendance_date(new_in, start, end) != day:
            invalid("punch_in", "punch_in must fall on the record's attendance date", body.punch_in, "body")
        if new_out is not None and (sent_in or sent_out):
            if as_utc(new_out) <= as_utc(new_in):
                invalid("punch_out", "punch_out must be after punch_in", body.punch_out, "body")
            if as_utc(new_out) - as_utc(new_in) > timedelta(hours=24):
                invalid("punch_out", "punch_out must be within 24 hours of punch_in", body.punch_out, "body")

    def same(a: Any, b: Any) -> bool:
        if isinstance(a, datetime) and isinstance(b, datetime):
            return dt_to_ms(a) == dt_to_ms(b)
        return a == b

    if same(new_status, old_status) and same(new_in, old_in) and same(new_out, old_out):
        invalid("body", "the request changes nothing", None, "body")

    old_vals = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day", False)),
    }
    new_vals = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive_fields(new_status, new_in, new_out, day, start, end),
    }
    changes = {
        field: {"from": old_vals[field], "to": new_vals[field]}
        for field in old_vals
        if not same(old_vals[field], new_vals[field])
    }
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only matches if nothing it depends on changed since we read the
    # record (history length + status + punch times). The loser matches nothing and gets 409.
    history = rec.get("history")
    guard: dict = {"_id": rec["_id"], "status": old_status, "punch_in": old_in, "punch_out": old_out}
    if isinstance(history, list) and history:
        guard["history"] = {"$size": len(history)}
    else:
        guard["$or"] = [{"history": {"$size": 0}}, {"history": None}]
    update: dict = {"$set": dict(new_vals)}
    if isinstance(history, list):
        update["$push"] = {"history": entry}
    else:
        update["$set"]["history"] = [entry]
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return serialize_attendance(updated)


MonthQuery = Annotated[str, Query(pattern=MONTH_PATTERN)]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    first, last = month_range(month)
    emp = get_employee_or_404(emp_code)
    pipeline, hint = monthly_pipeline(emp_code, first, last)
    rows = list(db.attendance_logs.aggregate(pipeline, hint=hint))
    stats = rows[0] if rows else {}
    joined = Date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last)  # R7: counted from joined_on
    halves = int(stats.get("present_halves", 0))
    pct = None
    if working_days > 0:
        pct = float(round_half_up(Decimal(halves) * 100 / (2 * working_days), 2))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(Decimal(halves) / 2),
        "leave_days": int(stats.get("leave_count", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    first, last = month_range(month)
    pipeline, hint = summary_pipeline(first, last, department)
    items = []
    for row in db.employees.aggregate(pipeline, hint=hint):
        count = int(row["wh_count"])
        average = None
        if count:  # average over records, from exact integer hundredths
            average = float(round_half_up(Decimal(int(row["wh_hundredths"])) / Decimal(count) / 100, 2))
        items.append(
            {
                "department": row["_id"],
                "headcount": int(row["headcount"]),
                "present_days": float(Decimal(int(row["present_halves"])) / 2),
                "avg_work_hours": average,
                "late_count": int(row["late_count"]),
                "total_late_minutes": int(row["total_late_minutes"]),
                "leave_count": int(row["leave_count"]),
                "on_duty_count": int(row["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthQuery,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_range(month)
    pipeline, hint = leaderboard_pipeline(first, last, limit, department)
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in db.attendance_logs.aggregate(pipeline, hint=hint)
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from", pattern=DATE_PATTERN)],
    to: Annotated[str, Query(pattern=DATE_PATTERN)],
):
    d_from, d_to = trend_range(from_, to)
    inputs = trend_inputs(department, d_from, d_to)
    if inputs["total"] == 0:
        raise HTTPException(404, "unknown department")
    pipeline, hint = trend_pipeline(inputs, d_from, d_to)
    items = []
    for row in db.attendance_logs.aggregate(pipeline, hint=hint):
        headcount, halves = int(row["headcount"]), int(row["present_halves"])
        moving = row.get("moving")
        items.append(
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": headcount,
                "present_count": float(Decimal(halves) / 2),
                "late_count": int(row["late_count"]),
                # exact from integers: present_halves / (2 * headcount)
                "attendance_rate": None
                if row.get("rate") is None
                else float(round_half_up(Decimal(halves) / Decimal(2 * headcount), 4)),
                "moving_avg_7d": None if moving is None else float(round_half_up(clean_float(moving), 4)),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """explain(executionStats) of the endpoint's main query, built by the very same builder functions."""

    def required(**values):
        for name, value in values.items():
            if value is None:
                invalid(name, "field required")

    if endpoint == "attendance_list":
        spec = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": spec["filter"],
            "sort": dict(spec["sort"]),
            "skip": spec["skip"],
            "limit": spec["limit"],
            "hint": spec["hint"],
        }
    else:
        if endpoint == "employee_monthly":
            required(emp_code=emp_code, month=month)
            pipeline, hint = monthly_pipeline(emp_code, *month_range(month))
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            required(month=month)
            pipeline, hint = summary_pipeline(*month_range(month), department)
            collection = "employees"
        elif endpoint == "late_leaderboard":
            required(month=month)
            first, last = month_range(month)
            pipeline, hint = leaderboard_pipeline(first, last, limit, department)
            collection = "attendance_logs"
        else:  # department_trend
            required(department=department, **{"from": from_}, to=to)
            d_from, d_to = trend_range(from_, to)
            pipeline, hint = trend_pipeline(trend_inputs(department, d_from, d_to), d_from, d_to)
            collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    plan = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": plan}
