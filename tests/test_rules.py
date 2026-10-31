"""Edge cases for each indicator rule, on a tiny hand-built dataset.

Calendar: 10 instructional days (2026-10-19 .. 2026-10-30), one K-8 school.
"""

from datetime import date, timedelta

import pytest

from ews.engine import Engine
from ews.synth import GRADE_DESCRIPTORS, SRD_PROGRAM, write_tables

SCHOOL = 990999
DAYS = [date(2026, 10, 19) + timedelta(days=i) for i in range(12)]
DAYS = [d for d in DAYS if d.weekday() < 5]  # 10 weekdays
assert len(DAYS) == 10


def build(tmp_path, students: dict[str, dict], thresholds=None) -> Engine:
    """students: id -> {grade, absences, actions, grades, fast, srd, interventions, entry}"""
    t = {k: [] for k in ["students", "studentSchoolAssociations", "studentSchoolAttendanceEvents",
                         "disciplineActions", "grades", "studentAssessments",
                         "studentProgramAssociations", "studentInterventionAssociations"]}
    t["schools"] = [{"schoolId": SCHOOL, "shortName": "TST", "nameOfInstitution": "Test K-8",
                     "lowGrade": 0, "highGrade": 8}]
    t["calendarDates"] = [{"schoolId": SCHOOL, "date": d, "calendarEventDescriptor": "Instructional day"}
                          for d in DAYS]
    for sid, s in students.items():
        g = s["grade"]
        t["students"].append({"studentUniqueId": sid, "firstName": "Test", "lastSurname": sid,
                              "birthDate": date(2015, 1, 1)})
        t["studentSchoolAssociations"].append({
            "studentUniqueId": sid, "schoolId": SCHOOL, "schoolYear": 2027,
            "entryDate": s.get("entry", DAYS[0]), "entryGradeLevelDescriptor": GRADE_DESCRIPTORS[g]})
        for d in DAYS[: s.get("absences", 0)]:
            t["studentSchoolAttendanceEvents"].append({
                "studentUniqueId": sid, "schoolId": SCHOOL, "eventDate": d,
                "attendanceEventCategoryDescriptor": "Excused Absence"})
        for i, action in enumerate(s.get("actions", [])):
            t["disciplineActions"].append({
                "disciplineActionIdentifier": f"{sid}-{i}", "studentUniqueId": sid,
                "responsibilitySchoolId": SCHOOL, "disciplineDate": DAYS[2],
                "disciplineDescriptor": action, "incidentIdentifier": f"{sid}-{i}"})
        for subject, letter in s.get("grades", {}).items():
            t["grades"].append({
                "studentUniqueId": sid, "schoolId": SCHOOL, "localCourseCode": subject[:3],
                "academicSubjectDescriptor": subject, "gradingPeriodDescriptor": "First Nine Weeks",
                "letterGradeEarned": letter, "gradePostedDate": DAYS[4]})
        for subject, admin_date, level in s.get("fast", []):
            t["studentAssessments"].append({
                "studentUniqueId": sid, "assessmentIdentifier": f"FAST-{subject}",
                "academicSubjectDescriptor": subject, "administrationDate": admin_date,
                "whenAssessedGradeLevelDescriptor": GRADE_DESCRIPTORS[g], "performanceLevel": level})
        if s.get("srd"):
            t["studentProgramAssociations"].append({
                "studentUniqueId": sid, "programName": SRD_PROGRAM, "beginDate": DAYS[1], "endDate": None})
        for i in range(s.get("interventions", 0)):
            t["studentInterventionAssociations"].append({
                "interventionIdentificationCode": f"I-{sid}-{i}", "studentUniqueId": sid,
                "schoolId": SCHOOL, "interventionCategory": "Test", "beginDate": DAYS[5],
                "parentInvolved": True, "notes": ""})
    d = write_tables(t, tmp_path / "fixture")
    return Engine(d, thresholds)


def flags(engine: Engine, sid: str) -> dict:
    return engine.query("SELECT * FROM student_indicators WHERE student_id = ?", [sid])[0]


# ---- ATT --------------------------------------------------------------------
def test_att_exactly_90_percent_is_not_flagged(tmp_path):
    e = build(tmp_path, {"a90": {"grade": 5, "absences": 1}, "a80": {"grade": 5, "absences": 2}})
    assert flags(e, "a90")["att_rate"] == 90.0
    assert flags(e, "a90")["att"] is False
    assert flags(e, "a80")["att"] is True


def test_att_min_days_is_configurable(tmp_path):
    late = {"late": {"grade": 5, "absences": 0, "entry": DAYS[5]}}
    # 5 days enrolled; add an absence on an enrolled day by giving 6 "absences" from day 0
    late["late"]["absences"] = 6  # only DAYS[5] falls inside enrollment -> 4/5 = 80%
    assert flags(build(tmp_path / "a", late), "late")["att"] is False  # default min 10 days
    assert flags(build(tmp_path / "b", late, {"att_min_days": 1}), "late")["att"] is True


# ---- SUS --------------------------------------------------------------------
def test_sus_counts_iss_and_oss_but_not_other_actions(tmp_path):
    e = build(tmp_path, {
        "det": {"grade": 6, "actions": ["Detention", "Parent Conference"]},
        "iss": {"grade": 6, "actions": ["In-School Suspension"]},
        "oss": {"grade": 2, "actions": ["Out-of-School Suspension"]},
    })
    assert flags(e, "det")["sus"] is False
    assert flags(e, "iss")["sus"] is True
    assert flags(e, "oss")["sus"] is True


# ---- CRS --------------------------------------------------------------------
def test_kindergarten_never_gets_crs(tmp_path):
    e = build(tmp_path, {"k": {"grade": 0, "grades": {"ELA": "F", "Mathematics": "F"}}})
    assert flags(e, "k")["crs"] is False


def test_crs_grade_1_math_failure_flags(tmp_path):
    e = build(tmp_path, {"g1": {"grade": 1, "grades": {"Mathematics": "F"}}})
    assert flags(e, "g1")["crs"] is True


def test_crs_ignores_non_core_subjects_and_d_grades(tmp_path):
    e = build(tmp_path, {"sci": {"grade": 4, "grades": {"Science": "F", "ELA": "D"}}})
    assert flags(e, "sci")["crs"] is False


# ---- LVL1 -------------------------------------------------------------------
@pytest.mark.parametrize("profile", [
    {"fast": [("ELA", DAYS[0], 1)]},                        # via FAST
    {"srd": True},                                          # via reading deficiency
    {"fast": [("Mathematics", DAYS[0], 1)], "srd": True},   # both -> still one indicator
])
def test_grade_3_hits_lvl1_via_either_path(tmp_path, profile):
    e = build(tmp_path, {"g3": {"grade": 3, **profile}})
    f = flags(e, "g3")
    assert f["lvl1"] is True
    assert f["indicator_count"] == 1


def test_lvl1_path_grade_bounds(tmp_path):
    e = build(tmp_path, {
        "g2fast": {"grade": 2, "fast": [("ELA", DAYS[0], 1)]},  # FAST path starts at grade 3
        "g4srd": {"grade": 4, "srd": True},                     # SRD path ends at grade 3
        "kSrd": {"grade": 0, "srd": True},
        "g8fast": {"grade": 8, "fast": [("Mathematics", DAYS[0], 1)]},
    })
    assert flags(e, "g2fast")["lvl1"] is False
    assert flags(e, "g4srd")["lvl1"] is False
    assert flags(e, "kSrd")["lvl1"] is True
    assert flags(e, "g8fast")["lvl1"] is True


def test_lvl1_uses_most_recent_fast_result(tmp_path):
    e = build(tmp_path, {"up": {"grade": 6, "fast": [("ELA", date(2026, 5, 1), 1), ("ELA", DAYS[1], 3)]}})
    assert flags(e, "up")["lvl1"] is False


# ---- 2+ counting and watchlist ---------------------------------------------
def test_two_plus_counting_and_watchlist(tmp_path):
    e = build(tmp_path, {
        "one": {"grade": 6, "actions": ["In-School Suspension"]},
        "two": {"grade": 6, "actions": ["In-School Suspension"], "grades": {"ELA": "F"}},
        "two_iv": {"grade": 6, "absences": 3, "grades": {"Mathematics": "F"}, "interventions": 1},
        "four": {"grade": 6, "absences": 5, "actions": ["Out-of-School Suspension"],
                 "grades": {"ELA": "F"}, "fast": [("ELA", DAYS[0], 1)]},
    })
    assert flags(e, "one")["indicator_count"] == 1
    assert flags(e, "two")["indicators"] == "SUS,CRS"
    assert flags(e, "four")["indicator_count"] == 4
    wl = {r["student_id"]: r for r in e.query("SELECT * FROM watchlist")}
    assert set(wl) == {"two", "two_iv", "four"}
    assert wl["two"]["no_intervention"] is True
    assert wl["two_iv"]["no_intervention"] is False
    # 2+ since the second indicator began: CRS posted DAYS[4] after SUS on DAYS[2]
    assert wl["two"]["flagged_since"] == DAYS[4].isoformat()
    assert wl["two"]["days_flagged"] == (date(2026, 10, 30) - DAYS[4]).days

    gc = e.query("SELECT * FROM grade_counts")[0]
    assert (gc["enrolled"], gc["sus"], gc["crs"], gc["two_plus"]) == (4, 3, 3, 3)


def test_posted_intervention_clears_no_intervention(tmp_path):
    e = build(tmp_path, {"two": {"grade": 6, "actions": ["In-School Suspension"], "grades": {"ELA": "F"}}})
    assert e.query("SELECT no_intervention FROM watchlist")[0]["no_intervention"] is True
    e.add_intervention("two", "Behavior support plan", "2026-10-30")
    assert e.query("SELECT no_intervention FROM watchlist")[0]["no_intervention"] is False
