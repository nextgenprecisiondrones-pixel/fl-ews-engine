"""FastAPI service over the EWS engine.

    uvicorn ews.api:app --reload
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ews.engine import Engine, parse_grade

app = FastAPI(
    title="Florida K-8 Early Warning System",
    description="Indicators per s. 1001.42(18)(b), F.S. Concept by Sal Abuawad. All student data is synthetic.",
    version="0.1.0",
)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return Engine()


def _check_school(engine: Engine, school: str | None) -> str | None:
    if school is None:
        return None
    code = school.upper()
    if code not in engine.school_codes():
        raise HTTPException(404, f"Unknown school '{school}'. Known: {sorted(engine.school_codes())}")
    return code


class InterventionIn(BaseModel):
    student_id: str
    category: str = Field(min_length=3, max_length=120)
    begin_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    parent_involved: bool = True
    notes: str = Field(default="", max_length=1000)


@app.get("/health")
def health(engine: Engine = Depends(get_engine)) -> dict:
    n = engine.query("SELECT count(*) AS n FROM enrollment")[0]["n"]
    return {"status": "ok", "as_of": engine.as_of, "students": n}


@app.get("/summary")
def summary(school: str | None = None, engine: Engine = Depends(get_engine)) -> dict:
    return engine.summary(_check_school(engine, school))


@app.get("/students")
def students(
    school: str | None = None,
    grade: str | None = Query(default=None, description="K or 1-8"),
    min_indicators: int = Query(default=0, ge=0, le=4),
    no_intervention: bool | None = None,
    indicator: str | None = Query(default=None, description="ATT, SUS, CRS or LVL1"),
    limit: int = Query(default=100, ge=1, le=1000),
    engine: Engine = Depends(get_engine),
) -> dict:
    code = _check_school(engine, school)
    try:
        parse_grade(grade)
        rows = engine.find_students(code, grade, min_indicators, no_intervention, indicator, limit)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"count": len(rows), "students": rows}


@app.get("/students/{student_id}")
def student(student_id: str, engine: Engine = Depends(get_engine)) -> dict:
    s = engine.student(student_id)
    if s is None:
        raise HTTPException(404, "Student not found")
    return s


@app.post("/interventions", status_code=201)
def add_intervention(body: InterventionIn, engine: Engine = Depends(get_engine)) -> dict:
    try:
        return engine.add_intervention(body.student_id, body.category, body.begin_date,
                                       body.parent_involved, body.notes)
    except KeyError as e:
        raise HTTPException(404, "Student not found") from e


@app.get("/brief", response_class=PlainTextResponse)
def brief(school: str, engine: Engine = Depends(get_engine)) -> str:
    return engine.brief(_check_school(engine, school))
