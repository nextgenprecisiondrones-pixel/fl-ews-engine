"""EWS engine: loads Ed-Fi-shaped Parquet into DuckDB and exposes the SQL views.

All indicator logic lives in rules.sql. This module only loads data, applies
thresholds, and wraps the views in small query helpers used by the API and agent.
"""

from __future__ import annotations

import os
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from ews.synth import SCHEMAS

# Single source of truth for every threshold in the rules.
THRESHOLDS: dict[str, Any] = {
    "as_of_date": "2026-10-30",
    "school_year": 2027,          # Ed-Fi key for 2026-27 (end year)
    "school_year_start": "2026-07-01",        # every indicator counts records on/after this date
    "assessment_window_start": "2025-07-01",  # (d) only: last spring's PM3 is the level in effect
    "ews_grade_max": 8,           # K-8 only
    "att_threshold_pct": 90,      # ATT: attendance rate strictly below 90%
    "att_min_days": 10,           # local policy: no ATT flag until 10 days enrolled (not statutory)
    "sus_min_count": 1,           # SUS: one or more ISS/OSS
    "crs_subjects": ["ELA", "Mathematics"],
    "crs_failing_grades": ["F"],
    "crs_grade_min": 1,           # CRS applies to grades 1-8 (not K)
    "crs_grade_max": 8,
    "lvl1_level": 1,              # LVL1 (d): Level 1 on statewide ELA or math
    "fast_grade_min": 3,          # statewide assessments start in grade 3
    "fast_grade_max": 8,
    "srd_grade_max": 3,           # DEF (e): substantial reading deficiency, K-3
    "smd_grade_max": 4,           # DEF (e): substantial mathematics deficiency, K-4
    "min_indicators": 2,          # 2+ indicators -> school-based team decision
}

INDICATORS = ("ATT", "SUS", "CRS", "LVL1", "DEF")
RULES_SQL = Path(__file__).with_name("rules.sql")
DEFAULT_DATA_DIR = Path(os.environ.get("EWS_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))


def _sql_value(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(f"'{_sql_value(x)}'" if isinstance(x, str) else _sql_value(x) for x in v) + ")"
    if isinstance(v, str):
        return v.replace("'", "''")
    return str(v)


def parse_grade(grade: str | int | None) -> int | None:
    if grade is None or grade == "":
        return None
    if str(grade).upper() in ("K", "KG", "0"):
        return 0
    g = int(grade)
    if not 0 <= g <= 8:
        raise ValueError("grade must be K or 1-8")
    return g


class Engine:
    def __init__(self, data_dir: str | Path | None = None, thresholds: dict | None = None):
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.cfg = {**THRESHOLDS, **(thresholds or {})}
        self.as_of = self.cfg["as_of_date"]
        self._lock = threading.Lock()
        self.con = duckdb.connect()
        self._load()

    # ---- loading -----------------------------------------------------------
    def _load(self) -> None:
        missing = [t for t in SCHEMAS if not (self.data_dir / f"{t}.parquet").exists()]
        if missing:
            raise FileNotFoundError(
                f"Missing {missing} in {self.data_dir}. Run `make data` first.")
        for table in SCHEMAS:
            path = (self.data_dir / f"{table}.parquet").as_posix()
            # Base tables (not views) so POST /interventions can insert.
            self.con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet('{path}')")
        sql = RULES_SQL.read_text().format(**{k: _sql_value(v) for k, v in self.cfg.items()})
        self.con.execute(sql)

    # ---- query helpers -----------------------------------------------------
    def query(self, sql: str, params: list | None = None) -> list[dict]:
        with self._lock:
            cur = self.con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [_jsonable(dict(zip(cols, row))) for row in cur.fetchall()]

    def schools(self) -> list[dict]:
        return self.query("SELECT schoolId AS school_id, shortName AS school, nameOfInstitution AS name, "
                          "lowGrade AS low_grade, highGrade AS high_grade FROM schools ORDER BY schoolId")

    def school_codes(self) -> set[str]:
        return {s["school"] for s in self.schools()}

    def normalize_school(self, school: str | None) -> str | None:
        """Case-insensitive school code. Raises ValueError for an unknown code."""
        if school is None or str(school).strip() == "":
            return None
        code = str(school).strip().upper()
        if code not in self.school_codes():
            raise ValueError(f"unknown school code '{school}'. Known: {sorted(self.school_codes())}")
        return code

    def summary(self, school: str | None = None) -> dict:
        school = self.normalize_school(school)
        where, params = ("WHERE school = ?", [school]) if school else ("", [])
        by_grade = self.query(
            f"SELECT school, grade_label AS grade, enrolled, att AS ATT, sus AS SUS, crs AS CRS, "
            f"lvl1 AS LVL1, def AS DEF, two_plus FROM grade_counts {where} ORDER BY school, grade_num", params)
        if school:
            by_grade = [{k: v for k, v in r.items() if k != "school"} for r in by_grade]
        tot = self.query(
            f"SELECT count(*) AS enrolled, count(*) FILTER (WHERE att) AS ATT, "
            f"count(*) FILTER (WHERE sus) AS SUS, count(*) FILTER (WHERE crs) AS CRS, "
            f"count(*) FILTER (WHERE lvl1) AS LVL1, count(*) FILTER (WHERE def) AS DEF "
            f"FROM student_indicators {where}", params)[0]
        wl = self.query(
            f"SELECT count(*) AS two_plus, count(*) FILTER (WHERE no_intervention) AS no_int, "
            f"coalesce(median(days_flagged) FILTER (WHERE no_intervention), 0) AS med_days "
            f"FROM watchlist {where}", params)[0]
        return {
            "school": school or "ALL",
            "as_of": self.as_of,
            "school_year_start": self.cfg["school_year_start"],
            "enrolled": tot["enrolled"],
            "indicators": {k: tot[k] for k in INDICATORS},
            "two_plus": wl["two_plus"],
            "two_plus_no_intervention": wl["no_int"],
            "median_days_flagged_no_intervention": float(wl["med_days"]),
            "by_grade": by_grade,
        }

    def find_students(
        self,
        school: str | None = None,
        grade: str | int | None = None,
        min_indicators: int = 0,
        no_intervention: bool | None = None,
        indicator: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        school = self.normalize_school(school)
        clauses, params = ["si.indicator_count >= ?"], [int(min_indicators)]
        if school:
            clauses.append("si.school = ?")
            params.append(school)
        g = parse_grade(grade)
        if g is not None:
            clauses.append("si.grade_num = ?")
            params.append(g)
        if indicator:
            ind = indicator.upper()
            if ind not in INDICATORS:
                raise ValueError(f"indicator must be one of {INDICATORS}")
            clauses.append(f"si.{ind.lower()}")
        if no_intervention is not None:
            clauses.append("coalesce(w.no_intervention, iv.n IS NULL) = ?")
            params.append(bool(no_intervention))
        params.append(int(limit))
        return self.query(
            f"""
            SELECT si.student_id, si.student_name, si.school, si.grade_label AS grade,
                   si.indicator_count, si.indicators, si.att_rate, si.suspensions,
                   coalesce(iv.n, 0) AS interventions, w.flagged_since, w.days_flagged
            FROM student_indicators si
            LEFT JOIN watchlist w USING (student_id)
            LEFT JOIN (SELECT student_id, interventions AS n FROM current_interventions) iv USING (student_id)
            WHERE {' AND '.join(clauses)}
            ORDER BY si.indicator_count DESC, w.days_flagged DESC NULLS LAST, si.student_id
            LIMIT ?
            """,
            params,
        )

    def student(self, student_id: str) -> dict | None:
        rows = self.query("SELECT * FROM student_indicators WHERE student_id = ?", [student_id])
        if not rows:
            return None
        s = rows[0]
        p = [student_id]
        s["indicator_detail"] = self.query(
            "SELECT indicator, flagged_since FROM indicator_events WHERE student_id = ? ORDER BY flagged_since", p)
        s["attendance_monthly"] = self.query(
            "SELECT month, days_enrolled, days_absent, att_rate FROM attendance_monthly "
            "WHERE student_id = ? ORDER BY month", p)
        s["grades"] = self.query(
            "SELECT academicSubjectDescriptor AS subject, letterGradeEarned AS grade, gradingPeriodDescriptor "
            "AS period FROM grades WHERE studentUniqueId = ? ORDER BY subject", p)
        s["assessments"] = self.query(
            "SELECT assessmentIdentifier AS assessment, administrationDate AS date, performanceLevel AS level "
            "FROM studentAssessments WHERE studentUniqueId = ? ORDER BY date, assessment", p)
        s["discipline"] = self.query(
            "SELECT disciplineDate AS date, disciplineDescriptor AS action FROM disciplineActions "
            "WHERE studentUniqueId = ? ORDER BY date", p)
        s["interventions"] = self.query(
            "SELECT interventionIdentificationCode AS id, interventionCategory AS category, beginDate AS begin_date, "
            "parentInvolved AS parent_involved, notes FROM studentInterventionAssociations "
            "WHERE studentUniqueId = ? ORDER BY beginDate", p)
        wl = self.query("SELECT flagged_since, days_flagged, no_intervention FROM watchlist WHERE student_id = ?", p)
        # (18)(b)2: 2+ indicators and not already served by a current-year intervention
        s["team_decision_required"] = bool(wl) and wl[0]["no_intervention"]
        s["two_plus_since"] = wl[0]["flagged_since"] if wl else None
        return s

    def add_intervention(self, student_id: str, category: str, begin_date: date | str | None = None,
                         parent_involved: bool = True, notes: str = "") -> dict:
        """Log an intervention. begin_date defaults to the as-of date and must fall in the
        current school year (school_year_start .. as_of_date); otherwise ValueError."""
        begin_d = date.fromisoformat(begin_date) if isinstance(begin_date, str) else begin_date
        begin_d = begin_d or date.fromisoformat(self.as_of)
        lo, hi = date.fromisoformat(self.cfg["school_year_start"]), date.fromisoformat(self.as_of)
        if not lo <= begin_d <= hi:
            raise ValueError(f"begin_date must be between {lo} and {hi} (current school year, not after as-of)")
        enr = self.query("SELECT school_id FROM enrollment WHERE student_id = ?", [student_id])
        if not enr:
            raise KeyError(student_id)
        with self._lock:
            n = self.con.execute("SELECT count(*) FROM studentInterventionAssociations").fetchone()[0]
            code = f"INT-API-{n + 1:05d}"
            begin = begin_d.isoformat()
            self.con.execute(
                "INSERT INTO studentInterventionAssociations VALUES (?, ?, ?, ?, ?, ?, ?)",
                [code, student_id, enr[0]["school_id"], category, begin, parent_involved, notes],
            )
        return {"id": code, "student_id": student_id, "category": category, "begin_date": begin,
                "parent_involved": parent_involved, "notes": notes}

    def brief(self, school: str) -> str:
        """Deterministic weekly support-team brief. Same data -> same text."""
        school = self.normalize_school(school)
        if school is None:
            raise ValueError("school is required")
        name = next(s["name"] for s in self.schools() if s["school"] == school)
        sm = self.summary(school)
        ind = sm["indicators"]
        pending = self.query(
            "SELECT student_id, student_name, grade_label, indicators, days_flagged FROM watchlist "
            "WHERE school = ? AND no_intervention ORDER BY days_flagged DESC, student_id LIMIT 5", [school])
        lines = [
            f"WEEKLY EARLY WARNING BRIEF - {name} ({school})",
            f"Data as of {sm['as_of']}. Synthetic data. Rules: s. 1001.42(18)(b), F.S.",
            "",
            f"Enrolled K-8: {sm['enrolled']}",
            f"Indicator counts: ATT {ind['ATT']} | SUS {ind['SUS']} | CRS {ind['CRS']} | "
            f"LVL1 {ind['LVL1']} | DEF {ind['DEF']}",
            f"Students with 2+ indicators: {sm['two_plus']} "
            f"({sm['two_plus_no_intervention']} with no current-year intervention on file)",
            "",
            "By grade (enrolled / ATT / SUS / CRS / LVL1 / DEF / 2+):",
        ]
        for g in sm["by_grade"]:
            lines.append(f"  Grade {g['grade']:>2}: {g['enrolled']:>3} / {g['ATT']:>2} / {g['SUS']:>2} / "
                         f"{g['CRS']:>2} / {g['LVL1']:>2} / {g['DEF']:>2} / {g['two_plus']:>2}")
        lines.append("")
        if pending:
            lines.append("Team decision needed - longest flagged without an intervention:")
            for p in pending:
                initials = "".join(w[0] for w in p["student_name"].split())
                lines.append(f"  - {p['student_id']} ({initials}, gr {p['grade_label']}): "
                             f"{p['indicators']}, flagged {p['days_flagged']} days")
            lines.append("")
            lines.append("Action: schedule a school-based team meeting with the parent for each student "
                         "above and log the agreed intervention.")
        else:
            lines.append("Every student with 2+ indicators has a current-year intervention on file.")
        return "\n".join(lines)


def _jsonable(row: dict) -> dict:
    for k, v in row.items():
        if isinstance(v, date):
            row[k] = v.isoformat()
        elif isinstance(v, Decimal):
            row[k] = float(v)
    return row
