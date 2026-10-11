"""Employee Attendance & Analytics API.

Run from the repository root: uvicorn app.main:app --port 8000
"""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import ObjectId
from bson.json_util import dumps as bson_dumps
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictInt, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---- Validation and serialization -------------------------------------------------
def fail422(message: str):
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail422("date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail422("invalid calendar date")


def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail422("month must be YYYY-MM")
    y, m = map(int, value.split("-"))
    first = date(y, m, 1)
    return first, date(y, m, monthrange(y, m)[1])


def epoch_to_dt(ms: int) -> datetime:
    if type(ms) is not int or not MIN_EPOCH_MS <= ms <= MAX_EPOCH_MS:
        fail422("timestamp must be an integer epoch-millisecond value in the supported range")
    # Convert via UTC and truncate before any business calculation.
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def dt_to_ms(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.astimezone(UTC).timestamp()) * 1000
    return value


def api_value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: api_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [api_value(v) for v in value]
    return value


def record_out(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("work_hours", None)
    doc.setdefault("punch_in", None)
    doc.setdefault("punch_out", None)
    return api_value(doc)


def employee_out(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    return api_value(doc)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_datetime(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_day(punch_in: datetime, emp: dict) -> date:
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(emp) and local.time().replace(tzinfo=None) < time(*map(int, emp["shift_end"].split(":"))):
        d -= timedelta(days=1)
    return d


def compute_late_minutes(punch_in: Optional[datetime], emp: dict, day: date) -> int:
    if punch_in is None:
        return 0
    start = shift_datetime(day, emp["shift_start"]).astimezone(UTC)
    elapsed = int((punch_in - start).total_seconds() // 60)
    seconds = (punch_in - start).total_seconds()
    return elapsed if seconds > 600 else 0


def shift_end_datetime(emp: dict, day: date) -> datetime:
    end = shift_datetime(day, emp["shift_end"])
    if is_overnight(emp):
        end += timedelta(days=1)
    return end.astimezone(UTC)


def derived_values(emp: dict, day: date, punch_in: Optional[datetime], punch_out: Optional[datetime], status: str) -> dict:
    if status not in PRESENT_STATUSES:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp, day)
    if punch_out is None or punch_in is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = (punch_out - punch_in).total_seconds()
    hours = round_half_up(seconds / 3600, 2)
    overtime_seconds = (punch_out - shift_end_datetime(emp, day)).total_seconds()
    overtime = int(overtime_seconds // 60) if overtime_seconds >= 1800 else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}


def month_bounds_strings(month: str):
    first, last = parse_month(month)
    return first.isoformat(), last.isoformat(), first, last


def round_expr(expr, places=2):
    """Mongo expression for positive values rounded half-up (not Mongo's ties-to-even $round)."""
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


def is_weekday_expr(date_expr):
    # $dayOfWeek: Sunday=1, Monday=2, ..., Saturday=7
    return {"$and": [{"$gte": [{"$dayOfWeek": {"date": date_expr, "timezone": "UTC"}}, 2]},
                     {"$lte": [{"$dayOfWeek": {"date": date_expr, "timezone": "UTC"}}, 6]}]}


def date_expr(field="date"):
    return {"$dateFromString": {"dateString": f"${field}", "format": "%Y-%m-%d", "timezone": "UTC"}}


# ---- Request models ----------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        if not re.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid calendar date") from exc
        return v

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, data):
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return data

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            epoch_to_dt(v)
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_timestamp(cls, data):
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return data

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            epoch_to_dt(v)
        return v


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_mutations(cls, data):
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} cannot be null")
        return data

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None:
            epoch_to_dt(v)
        return v


# ---- Indexes ------------------------------------------------------------------------
@app.on_event("startup")
def ensure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    db.employees.create_index([("joined_on", ASCENDING)], name="employee_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="attendance_date_status")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="employee_punch_in")


# ---- Basic endpoints ----------------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else now_truncated()
    day = attendance_day(ts, emp)
    vals = derived_values(emp, day, ts, None, body.status)
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": vals["late_minutes"], "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


# ---- Attendance listing --------------------------------------------------------------
def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            parse_date(date_from); q["date"]["$gte"] = date_from
        if date_to:
            parse_date(date_to); q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            fail422("date_from must be on or before date_to")
    if status is not None:
        if status not in ALL_STATUSES: fail422("invalid status")
        q["status"] = status
    return q


def attendance_list_pipeline(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20):
    q = attendance_filter(emp_code, date_from, date_to, status)
    return [{"$match": q}, {"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}]


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.aggregate(attendance_list_pipeline(emp_code, date_from, date_to, status, page, page_size)))
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# ---- Punch-out and regularization ----------------------------------------------------
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_dt(body.punched_at) if body.punched_at is not None else now_truncated()
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not doc:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    pin = doc.get("punch_in")
    if pin is None or ts <= pin or ts - pin > timedelta(hours=24):
        fail422("punched_at must be after punch-in and no more than 24 hours later")
    day = parse_date(doc["date"])
    vals = derived_values(emp, day, pin, ts, doc["status"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **vals}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day.isoformat()})
    if not old:
        raise HTTPException(404, "attendance record not found")
    old_status = old.get("status", "PRESENT")
    old_in, old_out = old.get("punch_in"), old.get("punch_out")
    status = body.status if "status" in body.model_fields_set and body.status is not None else old_status
    if status in ("ABSENT", "LEAVE") and (("punch_in" in body.model_fields_set and body.punch_in is not None) or ("punch_out" in body.model_fields_set and body.punch_out is not None)):
        fail422("ABSENT and LEAVE cannot have punch times")
    if status in ("ABSENT", "LEAVE"):
        new_in = new_out = None
    else:
        new_in = epoch_to_dt(body.punch_in) if "punch_in" in body.model_fields_set and body.punch_in is not None else old_in
        new_out = epoch_to_dt(body.punch_out) if "punch_out" in body.model_fields_set and body.punch_out is not None else old_out
        if new_in is None:
            fail422("presence status requires punch_in")
        if attendance_day(new_in, emp) != day:
            fail422("punch_in must belong to the record attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            fail422("punch_out must be after punch_in and within 24 hours")
    vals = derived_values(emp, day, new_in, new_out, status)
    final = {"status": status, "punch_in": new_in, "punch_out": new_out, **vals}
    tracked = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    before = {"status": old_status, "punch_in": old_in, "punch_out": old_out,
              "work_hours": old.get("work_hours"), "late_minutes": old.get("late_minutes", 0),
              "overtime_minutes": old.get("overtime_minutes", 0), "half_day": old.get("half_day", False)}
    changes = {}
    for key in tracked:
        if before.get(key) != final.get(key):
            changes[key] = {"from": before.get(key), "to": final.get(key)}
    if not changes:
        fail422("regularization does not change the record")
    now = now_truncated()
    entry = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: a competing edit changes history, so one update loses with 409 instead of overwriting it.
    if "history" in old:
        concurrency_filter = {"_id": old["_id"], "history": old["history"]}
    else:
        concurrency_filter = {"_id": old["_id"], "history": {"$exists": False}}
    update = {"$set": final, "$push": {"history": entry}}
    updated = db.attendance_logs.find_one_and_update(concurrency_filter, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was concurrently regularized; retry")
    return record_out(updated)


# ---- Analytics aggregation pipelines ------------------------------------------------
def employee_monthly_pipeline(emp_code: str, month: str):
    first_s, last_s, first, last = month_bounds_strings(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}]}}},
            {"$addFields": {"_workday": is_weekday_expr(date_expr("date"))}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": ["$_workday", {"$in": ["$status", list(PRESENT_STATUSES)]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
        ], "as": "_logs"}},
        {"$addFields": {"_monthStart": {"$dateFromString": {"dateString": {"$max": ["$joined_on", first_s]}, "format": "%Y-%m-%d"}},
                         "_monthEnd": {"$dateFromString": {"dateString": last_s, "format": "%Y-%m-%d"}}}},
        {"$addFields": {"working_days": {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$max": [0, {"$add": [{"$dateDiff": {"startDate": "$_monthStart", "endDate": "$_monthEnd", "unit": "day"}}, 1]}]}]}, "as": "i", "in": {"$dateAdd": {"startDate": "$_monthStart", "unit": "day", "amount": "$$i"}}}}, "as": "d", "cond": is_weekday_expr("$$d")}}}}},
        {"$project": {"_id": 0, "emp_code": 1, "working_days": 1,
            "_metrics": {"$arrayElemAt": ["$_logs", 0]}}},
        {"$addFields": {"present_days": {"$ifNull": ["$_metrics.present_days", 0]}, "leave_days": {"$ifNull": ["$_metrics.leave_days", 0]},
                         "late_count": {"$ifNull": ["$_metrics.late_count", 0]}, "total_late_minutes": {"$ifNull": ["$_metrics.total_late_minutes", 0]},
                         "total_overtime_minutes": {"$ifNull": ["$_metrics.total_overtime_minutes", 0]}}},
        {"$addFields": {"attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, round_expr({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4), None]}}},
        {"$project": {"_metrics": 0}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    parse_month(month)
    if not db.employees.find_one({"emp_code": emp_code}, {"_id": 1}):
        raise HTTPException(404, "employee not found")
    # Same indexed attendance lookup used by the endpoint; a separate count is not needed.
    result = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not result:
        raise HTTPException(404, "employee not found")
    row = result[0]
    return {"emp_code": emp_code, "month": month, "working_days": int(row.get("working_days", 0)),
            "present_days": round_half_up(row.get("present_days", 0), 2), "leave_days": int(row.get("leave_days", 0)),
            "late_count": int(row.get("late_count", 0)), "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)), "attendance_pct": row.get("attendance_pct")}


def department_summary_pipeline(month: str, department: Optional[str] = None):
    first_s, last_s, _, _ = month_bounds_strings(month)
    employee_match = {"joined_on": {"$lte": last_s}}
    if department is not None: employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}]}}},
            {"$addFields": {"_workday": is_weekday_expr(date_expr("date"))}},
            {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": ["$_workday", {"$in": ["$status", list(PRESENT_STATUSES)]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "work_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}, "$work_hours", 0]}},
                "work_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}
        ], "as": "_logs"}},
        {"$addFields": {"_m": {"$arrayElemAt": ["$_logs", 0]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$_m.present_days", 0]}},
            "work_sum": {"$sum": {"$ifNull": ["$_m.work_sum", 0]}}, "work_count": {"$sum": {"$ifNull": ["$_m.work_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$_m.late_count", 0]}}, "total_late_minutes": {"$sum": {"$ifNull": ["$_m.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$_m.leave_count", 0]}}, "on_duty_count": {"$sum": {"$ifNull": ["$_m.on_duty_count", 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]}, round_expr({"$divide": ["$work_sum", "$work_count"]}, 2), None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    first_s, last_s, _, _ = month_bounds_strings(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    parse_month(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def trend_pipeline(department: str, from_day: str, to_day: str):
    first, last = parse_date(from_day), parse_date(to_day)
    if last < first: fail422("to must be on or after from")
    if (last - first).days + 1 > 92: fail422("date range cannot exceed 92 days")
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None}},
        {"$project": {"_id": 0, "dates": {"$map": {"input": {"$range": [0, (last - first).days + 1]}, "as": "i", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_day, "format": "%Y-%m-%d"}}, "unit": "day", "amount": "$$i"}}, "format": "%Y-%m-%d", "timezone": "UTC"}}}}}},
        {"$unwind": "$dates"}, {"$set": {"date": "$dates"}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "n"}
        ], "as": "hc"}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                         "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                         "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
                         "_date": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}},
        {"$addFields": {"is_working_day": is_weekday_expr("$_date"),
                         "attendance_rate": {"$cond": [{"$and": [is_weekday_expr("$_date"), {"$gt": ["$headcount", 0]}]}, round_expr({"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 100]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, round_expr("$moving_avg_7d", 4)]}}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_day: str = Query(..., alias="from"), to_day: str = Query(..., alias="to")):
    first, last = parse_date(from_day), parse_date(to_day)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    pipeline = trend_pipeline(department, from_day, to_day)
    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


# ---- Explain support -----------------------------------------------------------------
def json_safe(value):
    # Explain plans can contain BSON types not natively JSON serializable by FastAPI.
    return __import__("json").loads(bson_dumps(value))


def explain_find(collection, query, sort, page, page_size):
    command = {"find": collection.name, "filter": query, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size}
    return db.command("explain", command, verbosity="executionStats")


def explain_aggregate(collection, pipeline):
    return db.command("explain", {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str,
                     emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, from_day: Optional[str] = Query(None, alias="from"),
                     to_day: Optional[str] = Query(None, alias="to"), page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100)):
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid: fail422("unsupported endpoint")
    if endpoint == "attendance_list":
        pipe = attendance_list_pipeline(emp_code, date_from, date_to, status, page, page_size)
        result = explain_aggregate(db.attendance_logs, pipe)
        coll = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: fail422("emp_code and month are required")
        parse_month(month); pipe = employee_monthly_pipeline(emp_code, month)
        result = explain_aggregate(db.employees, pipe); coll = "employees"
    elif endpoint == "department_summary":
        if not month: fail422("month is required")
        parse_month(month); pipe = department_summary_pipeline(month, department)
        result = explain_aggregate(db.employees, pipe); coll = "employees"
    elif endpoint == "late_leaderboard":
        if not month: fail422("month is required")
        parse_month(month); pipe = leaderboard_pipeline(month, limit, department)
        result = explain_aggregate(db.attendance_logs, pipe); coll = "attendance_logs"
    else:
        if not department or not from_day or not to_day: fail422("department, from and to are required")
        pipe = trend_pipeline(department, from_day, to_day)
        result = explain_aggregate(db.employees, pipe); coll = "employees"
    return {"endpoint": endpoint, "collection": coll, "explain": json_safe(result)}
