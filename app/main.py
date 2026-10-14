"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""

import os
import re
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="idx_employee_department_joined",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING)],
        name="idx_attendance_date_status",
    )

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def to_ist(dt: datetime) -> datetime:
    """Convert a MongoDB UTC datetime or aware datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def truncate_seconds(dt: datetime) -> datetime:
    """Truncate, never round, to a whole second."""
    return dt.replace(microsecond=0)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Serialize a datetime as epoch milliseconds for API responses."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_epoch_ms(value: int) -> datetime:
    """Convert a validated epoch-millisecond instant to UTC."""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(
        microsecond=0
    )


def shift_instants(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
    punch_in: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Build the correct IST shift boundaries, including overnight shifts."""
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    day = date.fromisoformat(attendance_date)
    overnight = shift_end <= shift_start

    if overnight and punch_in is not None:
        local_in = to_ist(punch_in)
        if (local_in.hour, local_in.minute) < (end_hour, end_minute):
            day -= timedelta(days=1)

    start = datetime(
        day.year, day.month, day.day,
        start_hour, start_minute, tzinfo=IST
    )
    end_day = day + timedelta(days=1) if overnight else day
    end = datetime(
        end_day.year, end_day.month, end_day.day,
        end_hour, end_minute, tzinfo=IST
    )
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local_in = to_ist(truncate_seconds(punch_in))
    h, m = map(int, shift_start.split(":"))

    # Infer the previous shift-start date for an overnight early-morning punch.
    start_day = local_in.date()
    if shift_start > "00:00" and (local_in.hour, local_in.minute) < (h, m):
        # This adjustment is appropriate only for overnight shifts; the caller
        # should use compute_late_for_employee when shift_end is available.
        pass

    start = datetime(
        start_day.year, start_day.month, start_day.day,
        h, m, tzinfo=IST
    )
    elapsed_seconds = (local_in - start).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_late_for_employee(
    punch_in: datetime, shift_start: str, shift_end: str
) -> int:
    local_in = to_ist(truncate_seconds(punch_in))
    start, _ = shift_instants(
        local_in.date().isoformat(), shift_start, shift_end, local_in
    )
    elapsed_seconds = (local_in - start).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime, shift_end: str, date_str: str
) -> int:
    h, m = map(int, shift_end.split(":"))
    day = date.fromisoformat(date_str)

    end = datetime(
        day.year, day.month, day.day, h, m, tzinfo=IST
    )
    local_out = to_ist(truncate_seconds(punch_out))
    elapsed_seconds = (local_out - end).total_seconds()

    if elapsed_seconds < 1800:
        return 0
    return int(elapsed_seconds // 60)

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, strict=True,
        ge=100000000000, le=4102444800000
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, strict=True,
        ge=100000000000, le=4102444800000
    )

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None
    punch_in: Optional[int] = Field(
        default=None, strict=True,
        ge=100000000000, le=4102444800000
    )
    punch_out: Optional[int] = Field(
        default=None, strict=True,
        ge=100000000000, le=4102444800000
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        item["created_at"] = to_epoch_ms(item.get("created_at"))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }




@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        ts = truncate_seconds(datetime.now(timezone.utc))
    else:
        ts = parse_epoch_ms(body.punched_at)

    local_ts = to_ist(ts)
    attendance_date = local_ts.date().isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_for_employee(
            ts, emp["shift_start"], emp["shift_end"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    doc["punch_in"] = to_epoch_ms(doc["punch_in"])
    doc["punch_out"] = None
    return doc

@app.get("/attendance")

def serialize_attendance(doc: dict) -> dict:
    """Convert MongoDB attendance datetimes to API epoch milliseconds."""
    doc.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        doc[field] = to_epoch_ms(doc.get(field))

    for entry in doc.get("history", []):
        if entry.get("at") is not None:
            entry["at"] = to_epoch_ms(entry["at"])

        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = to_epoch_ms(change[side])

    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal[
        "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
    ]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(422, "date_from must be YYYY-MM-DD")

    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(422, "date_to must be YYYY-MM-DD")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not exceed date_to")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status

    if date_from or date_to:
        date_filter = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        query["date"] = date_filter

    total = db.attendance_logs.count_documents(query)

    docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [serialize_attendance(doc) for doc in docs]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    punched_out_at = (
        truncate_seconds(datetime.now(timezone.utc))
        if body.punched_at is None
        else parse_epoch_ms(body.punched_at)
    )

    # Search by instant, not today's date: overnight shifts cross midnight.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_out_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_at = record["punch_in"]
    elapsed = (punched_out_at - punch_in_at).total_seconds()

    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "Invalid punch-out time")

    work_hours = compute_work_hours(punch_in_at, punched_out_at)
    overtime = compute_overtime(
        punched_out_at, emp["shift_end"], record["date"]
    )

    # Only one concurrent punch-out can update a still-open record.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_out_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": work_hours < 4.50,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Already punched out")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    try:
        requested_date = datetime.strptime(date, "%Y-%m-%d").date()
        if requested_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    old = dict(record)
    old.setdefault("history", [])
    old.setdefault("half_day", False)

    # Work on a copy so we can compare actual changes.
    new = dict(old)
    changes_requested = body.model_dump(
        exclude_unset=True,
        exclude={"reason", "regularized_by"},
    )

    requested_status = changes_requested.get("status", old["status"])

    if requested_status in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_requested or "punch_out" in changes_requested:
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot include punch times",
            )
        new["punch_in"] = None
        new["punch_out"] = None
    else:
        if "punch_in" in changes_requested:
            new["punch_in"] = (
                parse_epoch_ms(changes_requested["punch_in"])
                if changes_requested["punch_in"] is not None
                else None
            )
        if "punch_out" in changes_requested:
            new["punch_out"] = (
                parse_epoch_ms(changes_requested["punch_out"])
                if changes_requested["punch_out"] is not None
                else None
            )

    new["status"] = requested_status

    if new["status"] in ("PRESENT", "WFH", "ON_DUTY"):
        if new.get("punch_in") is None:
            raise HTTPException(
                422,
                "Presence status requires punch_in",
            )

        local_in = to_ist(new["punch_in"])
        if local_in.date().isoformat() != date:
            raise HTTPException(
                422,
                "punch_in must fall on the attendance date in IST",
            )

    if new.get("punch_in") is not None and new.get("punch_out") is not None:
        elapsed = (new["punch_out"] - new["punch_in"]).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 3600:
            raise HTTPException(
                422,
                "punch_out must be after punch_in and within 24 hours",
            )
    elif new.get("punch_out") is not None:
        raise HTTPException(422, "punch_out requires punch_in")

    if new["status"] in ("ABSENT", "LEAVE"):
        new.update({
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        })
    else:
        new["late_minutes"] = compute_late_for_employee(
            new["punch_in"], emp["shift_start"], emp["shift_end"]
        )

        if new.get("punch_out") is not None:
            new["work_hours"] = compute_work_hours(
                new["punch_in"], new["punch_out"]
            )
            new["overtime_minutes"] = compute_overtime(
                new["punch_out"], emp["shift_end"], date
            )
            new["half_day"] = new["work_hours"] < 4.50
        else:
            new["work_hours"] = None
            new["overtime_minutes"] = 0
            new["half_day"] = False

    tracked_fields = (
        "status", "punch_in", "punch_out", "work_hours",
        "late_minutes", "overtime_minutes", "half_day",
    )

    def comparable(value):
        if isinstance(value, datetime):
            return to_epoch_ms(value)
        return value

    actual_changes = {}
    for field in tracked_fields:
        before = old.get(field)
        after = new.get(field)
        if comparable(before) != comparable(after):
            actual_changes[field] = {
                "from": comparable(before),
                "to": comparable(after),
            }

    if not actual_changes:
        raise HTTPException(422, "Request makes no changes")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": actual_changes,
    }

    # Compare-and-set: concurrent corrections cannot overwrite each
    # other's history silently. A losing request receives HTTP 409.
    result = db.attendance_logs.update_one(
        {
            "_id": old["_id"],
            "history": old["history"],
            "status": old["status"],
            "punch_in": old.get("punch_in"),
            "punch_out": old.get("punch_out"),
        },
        {
            "$set": {
                "status": new["status"],
                "punch_in": new.get("punch_in"),
                "punch_out": new.get("punch_out"),
                "work_hours": new["work_hours"],
                "late_minutes": new["late_minutes"],
                "overtime_minutes": new["overtime_minutes"],
                "half_day": new["half_day"],
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "Record changed concurrently; reload and retry",
        )

    updated = db.attendance_logs.find_one({"_id": old["_id"]})
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    try:
        month_start = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    next_month = (
        month_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1)
    month_end = next_month - timedelta(days=1)

    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    joined_on = date.fromisoformat(emp["joined_on"])
    first_day = max(joined_on, month_start)

    working_days = 0
    current_day = first_day
    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    logs = list(db.attendance_logs.find({
        "emp_code": emp_code,
        "date": {
            "$gte": month_start.isoformat(),
            "$lt": next_month.isoformat(),
        },
    }, {"_id": 0}))

    present_days = Decimal("0")
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for log in logs:
        if log.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
            if date.fromisoformat(log["date"]).weekday() < 5:
                present_days += (
                    Decimal("0.5")
                    if log.get("half_day", False)
                    else Decimal("1")
                )

        if log.get("status") == "LEAVE":
            leave_days += 1

        late = log.get("late_minutes", 0) or 0
        if late > 0:
            late_count += 1
            total_late_minutes += late

        total_overtime_minutes += log.get("overtime_minutes", 0) or 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (present_days * Decimal("100") / Decimal(working_days)).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    try:
        month_start = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    next_month = (
        month_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        
{
    "$group": {
        "_id": "$emp_code",
        "total_late_minutes": {"$sum": "$late_minutes"},
        "late_count": {"$sum": 1},
    }
},

        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "late_count": 1,
            }
        },
    ]

    if department:
        pipeline.append({"$match": {"department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"rank": 1, "emp_code": 1}},
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if start_day.isoformat() != from_date or end_day.isoformat() != to_date:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if end_day < start_day:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")

    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    employee_count = db.employees.count_documents({"department": department})
    if employee_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = [
        {
            "$documents": [
                {"start": from_date, "end": to_date}
            ]
        },
        {
            "$project": {
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": "$start"
                                                    }
                                                },
                                                "endDate": {
                                                    "$dateFromString": {
                                                        "dateString": "$end"
                                                    }
                                                },
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": "$start"
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "format": "%Y-%m-%d",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {"$project": {"_id": 0, "date": "$days"}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"trend_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$trend_date"]},
                        }
                    }
                ],
                "as": "employees",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "trend_date": "$date",
                    "employee_codes": "$employees.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$trend_date"]},
                                    {"$in": ["$emp_code", "$$employee_codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "headcount": {"$size": "$employees"},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    "$$log.half_day",
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": ["$$log.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {
                                    "$multiply": [
                                        {"$divide": ["$present_count", "$headcount"]},
                                        100,
                                    ]
                                },
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.aggregate(pipeline))
    return {"department": department, "items": items}

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    try:
        month_start = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")

    next_month = (
        month_start.replace(day=28) + timedelta(days=4)
    ).replace(day=1)
    month_end = next_month - timedelta(days=1)

    employee_filter = {
        "joined_on": {"$lte": month_end.isoformat()}
    }
    if department:
        employee_filter["department"] = department

    employees = list(
        db.employees.find(employee_filter, {"_id": 0})
    )

    departments = {}
    for emp in employees:
        departments.setdefault(emp["department"], []).append(emp)

    results = []

    for dept_name in sorted(departments):
        dept_employees = departments[dept_name]
        emp_codes = [emp["emp_code"] for emp in dept_employees]

        logs = list(
            db.attendance_logs.find(
                {
                    "emp_code": {"$in": emp_codes},
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lt": next_month.isoformat(),
                    },
                },
                {"_id": 0},
            )
        )

        present_days = Decimal("0")
        work_hours_values = []
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0

        for log in logs:
            status = log.get("status")

            if status in ("PRESENT", "WFH", "ON_DUTY"):
                if date.fromisoformat(log["date"]).weekday() < 5:
                    present_days += (
                        Decimal("0.5")
                        if log.get("half_day", False)
                        else Decimal("1")
                    )

                hours = log.get("work_hours")
                if hours is not None:
                    work_hours_values.append(Decimal(str(hours)))

            if status == "LEAVE":
                leave_count += 1

            if status == "ON_DUTY":
                on_duty_count += 1

            late = log.get("late_minutes", 0) or 0
            if late > 0:
                late_count += 1
                total_late_minutes += late

        avg_work_hours = None
        if work_hours_values:
            avg_work_hours = float(
                (sum(work_hours_values) / len(work_hours_values)).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

        results.append({
            "department": dept_name,
            "headcount": len(dept_employees),
            "present_days": float(present_days),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late_minutes,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return {"month": month, "items": results}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal[
        "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
    ]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    query = {}

    if endpoint == "attendance_list":
        if emp_code:
            query["emp_code"] = emp_code
        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(422, "Invalid date_from")
            query.setdefault("date", {})["$gte"] = date_from
        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(422, "Invalid date_to")
            query.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
        if status:
            query["status"] = status

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422, "emp_code and month are required"
            )
        query["emp_code"] = emp_code
        query["date"] = {
            "$gte": month + "-01",
            "$lt": (
                date.fromisoformat(month + "-01").replace(day=28)
                + timedelta(days=4)
            ).replace(day=1).isoformat(),
        }

    elif endpoint in ("department_summary", "late_leaderboard"):
        if not month:
            raise HTTPException(422, "month is required")
        try:
            month_start = date.fromisoformat(month + "-01")
        except ValueError:
            raise HTTPException(422, "Invalid month")

        next_month = (
            month_start.replace(day=28) + timedelta(days=4)
        ).replace(day=1)

        query["date"] = {
            "$gte": month_start.isoformat(),
            "$lt": next_month.isoformat(),
        }

        if endpoint == "late_leaderboard":
            query["late_minutes"] = {"$gt": 0}

        if department:
            employee_codes = [
                emp["emp_code"]
                for emp in db.employees.find(
                    {"department": department},
                    {"_id": 0, "emp_code": 1},
                )
            ]
            query["emp_code"] = {"$in": employee_codes}

    else:
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "department, from and to are required"
            )
        try:
            start_day = date.fromisoformat(from_date)
            end_day = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "Invalid date range")

        if end_day < start_day or (end_day - start_day).days > 91:
            raise HTTPException(422, "Invalid date range")

        query["date"] = {
            "$gte": from_date,
            "$lte": to_date,
        }

    command = {
        "find": "attendance_logs",
        "filter": query,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }

    explain = db.command(
        "explain",
        command,
        verbosity="executionStats",
    )

    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": explain,
    }



# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
