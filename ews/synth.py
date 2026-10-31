"""Deterministic synthetic data generator for the EWS engine.

Every record here is invented. Names, IDs, schools and the district are
fictional. Output is shaped like Ed-Fi ODS/API resources (camelCase field
names, descriptor strings) so the ingest layer reads like it would against a
real Ed-Fi feed, and is written as CSV (human-readable) and Parquet (what the
engine loads).

    python -m ews.synth --out data --seed 42
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

SCHOOL_YEAR = 2027  # Ed-Fi convention: school year 2026-27 is keyed by its end year
FIRST_DAY = date(2026, 8, 10)
LAST_DAY = date(2026, 10, 30)
HOLIDAYS = {date(2026, 9, 7)}  # Labor Day
PM1_DATE = date(2026, 9, 16)
PRIOR_PM3_DATE = date(2026, 5, 12)
GRADES_POSTED = date(2026, 10, 23)

GRADE_DESCRIPTORS = [
    "Kindergarten", "First grade", "Second grade", "Third grade", "Fourth grade",
    "Fifth grade", "Sixth grade", "Seventh grade", "Eighth grade",
]

# (schoolId, shortName, name, low grade, high grade, enrollment)
SCHOOLS = [
    (990101, "HCE", "Heron Creek Elementary", 0, 5, 130),
    (990102, "CBE", "Cypress Bend Elementary", 0, 5, 120),
    (990103, "PRE", "Palmetto Ridge Elementary", 0, 5, 125),
    (990201, "SHM", "Sandhill Middle", 6, 8, 140),
    (990202, "GPM", "Gulf Pine Middle", 6, 8, 135),
    (990301, "WKA", "Withlacoochee K-8 Academy", 0, 8, 120),
]

# Column name -> DuckDB type. Drives CSV headers and typed Parquet output,
# so empty tables (e.g. in test fixtures) still get the right schema.
SCHEMAS: dict[str, dict[str, str]] = {
    "schools": {
        "schoolId": "INTEGER", "shortName": "VARCHAR", "nameOfInstitution": "VARCHAR",
        "lowGrade": "INTEGER", "highGrade": "INTEGER",
    },
    "students": {
        "studentUniqueId": "VARCHAR", "firstName": "VARCHAR", "lastSurname": "VARCHAR",
        "birthDate": "DATE",
    },
    "studentSchoolAssociations": {
        "studentUniqueId": "VARCHAR", "schoolId": "INTEGER", "schoolYear": "INTEGER",
        "entryDate": "DATE", "entryGradeLevelDescriptor": "VARCHAR",
    },
    "calendarDates": {
        "schoolId": "INTEGER", "date": "DATE", "calendarEventDescriptor": "VARCHAR",
    },
    "studentSchoolAttendanceEvents": {
        "studentUniqueId": "VARCHAR", "schoolId": "INTEGER", "eventDate": "DATE",
        "attendanceEventCategoryDescriptor": "VARCHAR",
    },
    "disciplineActions": {
        "disciplineActionIdentifier": "VARCHAR", "studentUniqueId": "VARCHAR",
        "responsibilitySchoolId": "INTEGER", "disciplineDate": "DATE",
        "disciplineDescriptor": "VARCHAR", "incidentIdentifier": "VARCHAR",
    },
    "grades": {
        "studentUniqueId": "VARCHAR", "schoolId": "INTEGER", "localCourseCode": "VARCHAR",
        "academicSubjectDescriptor": "VARCHAR", "gradingPeriodDescriptor": "VARCHAR",
        "letterGradeEarned": "VARCHAR", "gradePostedDate": "DATE",
    },
    "studentAssessments": {
        "studentUniqueId": "VARCHAR", "assessmentIdentifier": "VARCHAR",
        "academicSubjectDescriptor": "VARCHAR", "administrationDate": "DATE",
        "whenAssessedGradeLevelDescriptor": "VARCHAR", "performanceLevel": "INTEGER",
    },
    "studentProgramAssociations": {
        "studentUniqueId": "VARCHAR", "programName": "VARCHAR", "beginDate": "DATE",
        "endDate": "DATE",
    },
    "studentInterventionAssociations": {
        "interventionIdentificationCode": "VARCHAR", "studentUniqueId": "VARCHAR",
        "schoolId": "INTEGER", "interventionCategory": "VARCHAR", "beginDate": "DATE",
        "parentInvolved": "BOOLEAN", "notes": "VARCHAR",
    },
}

SRD_PROGRAM = "Substantial Reading Deficiency"

FIRST_NAMES = [
    "Avery", "Jordan", "Riley", "Camila", "Mateo", "Zoe", "Elijah", "Layla", "Noah", "Maya",
    "Liam", "Aaliyah", "Ezra", "Sofia", "Isaiah", "Nora", "Gabriel", "Leah", "Caleb", "Ava",
    "Josiah", "Mila", "Dominic", "Harper", "Adrian", "Aria", "Miles", "Valeria", "Jonah", "Ruby",
    "Theo", "Ivy", "Santiago", "Hazel", "Owen", "Naomi", "Kai", "Lucia", "Silas", "Elena",
]
LAST_NAMES = [
    "Alder", "Brightwater", "Calloway", "Delacroix", "Ellison", "Fairbanks", "Galvan", "Hollis",
    "Ibarra", "Jessup", "Kingsley", "Lockhart", "Marlow", "Navarro", "Okafor", "Pemberton",
    "Quintero", "Rourke", "Sandoval", "Thibault", "Underhill", "Vasquez", "Whitlock", "Yardley",
    "Zamora", "Ashby", "Burrell", "Crestwood", "Dunmore", "Everly",
]

ABSENCE_CATEGORIES = ["Excused Absence", "Unexcused Absence"]
NON_SUSPENSION_ACTIONS = ["Detention", "Parent Conference", "Loss of Privileges"]
INTERVENTION_CATEGORIES = [
    "Attendance contract", "Check-in/check-out mentoring", "Tier 2 reading small group",
    "Tier 2 math small group", "Counseling referral", "Behavior support plan",
]


def instructional_days() -> list[date]:
    days, d = [], FIRST_DAY
    while d <= LAST_DAY:
        if d.weekday() < 5 and d not in HOLIDAYS:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate(seed: int = 42) -> dict[str, list[dict]]:
    """Build every table in memory. Same seed -> byte-identical output."""
    rng = random.Random(seed)
    cal = instructional_days()
    t: dict[str, list[dict]] = {name: [] for name in SCHEMAS}

    for school_id, short, name, lo, hi, _ in SCHOOLS:
        t["schools"].append({"schoolId": school_id, "shortName": short,
                             "nameOfInstitution": name, "lowGrade": lo, "highGrade": hi})
        for d in cal:
            t["calendarDates"].append({"schoolId": school_id, "date": d,
                                       "calendarEventDescriptor": "Instructional day"})

    sid = 7_000_000
    disc_n = 0
    int_n = 0
    for school_id, short, _, lo, hi, size in SCHOOLS:
        grades = list(range(lo, hi + 1))
        for i in range(size):
            sid += 1
            stu = str(sid)
            grade = grades[i % len(grades)]
            risk = rng.random() ** 3  # most students low risk, long tail
            t["students"].append({
                "studentUniqueId": stu,
                "firstName": rng.choice(FIRST_NAMES),
                "lastSurname": rng.choice(LAST_NAMES),
                "birthDate": date(2020 - grade, rng.randint(1, 12), rng.randint(1, 28)),
            })
            entry = FIRST_DAY
            if rng.random() < 0.05:  # late enrollees
                entry = rng.choice(cal[5:35])
            t["studentSchoolAssociations"].append({
                "studentUniqueId": stu, "schoolId": school_id, "schoolYear": SCHOOL_YEAR,
                "entryDate": entry, "entryGradeLevelDescriptor": GRADE_DESCRIPTORS[grade],
            })

            # Attendance: one event per absent day (Ed-Fi records exceptions, not presence)
            p_absent = 0.005 + 0.03 * rng.random() + 0.14 * risk
            for d in cal:
                if d >= entry and rng.random() < p_absent:
                    t["studentSchoolAttendanceEvents"].append({
                        "studentUniqueId": stu, "schoolId": school_id, "eventDate": d,
                        "attendanceEventCategoryDescriptor": rng.choice(ABSENCE_CATEGORIES),
                    })

            # Discipline: suspensions more likely in middle grades and for high-risk students
            p_sus = (0.003 + 0.10 * risk) if grade < 6 else (0.02 + 0.30 * risk)
            n_actions = 0
            if rng.random() < p_sus:
                n_actions = 1 + (rng.random() < 0.3)
            for _ in range(n_actions):
                disc_n += 1
                t["disciplineActions"].append({
                    "disciplineActionIdentifier": f"DA-{disc_n:05d}", "studentUniqueId": stu,
                    "responsibilitySchoolId": school_id,
                    "disciplineDate": rng.choice([d for d in cal if d >= entry]),
                    "disciplineDescriptor": rng.choice(
                        ["In-School Suspension", "Out-of-School Suspension"]),
                    "incidentIdentifier": f"INC-{disc_n:05d}",
                })
            if rng.random() < 0.10:  # minor actions that must NOT count as suspensions
                disc_n += 1
                t["disciplineActions"].append({
                    "disciplineActionIdentifier": f"DA-{disc_n:05d}", "studentUniqueId": stu,
                    "responsibilitySchoolId": school_id,
                    "disciplineDate": rng.choice([d for d in cal if d >= entry]),
                    "disciplineDescriptor": rng.choice(NON_SUSPENSION_ACTIONS),
                    "incidentIdentifier": f"INC-{disc_n:05d}",
                })

            # Course grades: first nine weeks progress grades. K is standards-based (E/S/N).
            for subject in ["ELA", "Mathematics", "Science", "Social Studies"]:
                if grade == 0:
                    letter = rng.choices(["E", "S", "N"], [3, 6, 1])[0]
                else:
                    p_f = 0.01 + 0.25 * risk if subject in ("ELA", "Mathematics") else 0.02
                    letter = "F" if rng.random() < p_f else rng.choices(
                        ["A", "B", "C", "D"], [3, 4, 3, 1])[0]
                t["grades"].append({
                    "studentUniqueId": stu, "schoolId": school_id,
                    "localCourseCode": f"{subject[:3].upper()}-{grade:02d}",
                    "academicSubjectDescriptor": subject,
                    "gradingPeriodDescriptor": "First Nine Weeks",
                    "letterGradeEarned": letter, "gradePostedDate": GRADES_POSTED,
                })

            # FAST achievement levels. Levels exist for spring PM3, so grades 4-8 carry
            # last spring's result; a few retained 3rd graders carry a grade-3 PM3.
            retained_g3 = grade == 3 and rng.random() < 0.06
            if grade >= 4 or retained_g3:
                tested_grade = grade if retained_g3 else grade - 1
                for subject, ident in [("ELA", "FAST-ELA-READING"), ("Mathematics", "FAST-MATH")]:
                    lvl = 1 if rng.random() < 0.07 + 0.40 * risk else rng.choices(
                        [2, 3, 4, 5], [3, 4, 3, 1])[0]
                    t["studentAssessments"].append({
                        "studentUniqueId": stu, "assessmentIdentifier": f"{ident}-PM3",
                        "academicSubjectDescriptor": subject,
                        "administrationDate": PRIOR_PM3_DATE,
                        "whenAssessedGradeLevelDescriptor": GRADE_DESCRIPTORS[tested_grade],
                        "performanceLevel": lvl,
                    })

            # Substantial reading deficiency (K-3), identified from fall screening
            if grade <= 3 and rng.random() < 0.05 + 0.30 * risk:
                t["studentProgramAssociations"].append({
                    "studentUniqueId": stu, "programName": SRD_PROGRAM,
                    "beginDate": max(entry, PM1_DATE) + timedelta(days=rng.randint(1, 14)),
                    "endDate": None,
                })

            # Interventions already on file: more likely for higher-risk students
            if rng.random() < 0.10 + 0.60 * risk:
                int_n += 1
                t["studentInterventionAssociations"].append({
                    "interventionIdentificationCode": f"INT-{int_n:05d}",
                    "studentUniqueId": stu, "schoolId": school_id,
                    "interventionCategory": rng.choice(INTERVENTION_CATEGORIES),
                    "beginDate": rng.choice(cal[20:]), "parentInvolved": rng.random() < 0.8,
                    "notes": "Synthetic record",
                })
    return t


def write_tables(tables: dict[str, list[dict]], out_dir: str | Path) -> Path:
    """Write each table as CSV and typed Parquet. Missing tables are written empty."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for name, cols in SCHEMAS.items():
        rows = tables.get(name, [])
        csv_path = out / f"{name}.csv"
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(cols))
            w.writeheader()
            for r in rows:
                w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
        col_spec = ", ".join(f"'{c}': '{ty}'" for c, ty in cols.items())
        con.execute(
            f"COPY (SELECT * FROM read_csv('{csv_path.as_posix()}', header=true, "
            f"columns={{{col_spec}}}, nullstr='')) "
            f"TO '{(out / f'{name}.parquet').as_posix()}' (FORMAT parquet)"
        )
    con.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic Ed-Fi-shaped EWS data")
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    tables = generate(args.seed)
    write_tables(tables, args.out)
    print(f"Wrote {len(SCHEMAS)} tables to {args.out}/ (seed={args.seed})")
    for name in SCHEMAS:
        print(f"  {name:<34} {len(tables[name]):>6} rows")


if __name__ == "__main__":
    main()
