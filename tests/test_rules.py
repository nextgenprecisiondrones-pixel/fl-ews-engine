"""Edge cases for each indicator rule, on a tiny hand-built dataset.

Calendar: 10 instructional days (2026-10-19 .. 2026-10-30), one K-8 school.
"""

from datetime import date, timedelta

import pytest

from ews.engine import Engine
from ews.synth import GRADE_DESCRIPTORS, SMD_PROGRAM, SRD_PROGRAM, write_tables

SCHOOL = 990999
DAYS = [date(2026, 10, 19) + timedelta(days=i) for i in range(12)]
DAYS = [d for d in DAYS if d.weekday() < 5]  # 10 weekdays
assert len(DAYS) == 10


def build(tmp_path, students: dict[str, dict], thresholds=None, extra: dict | None = None) -> Engine:
    """students: id -> {grade, absences, actions, grades, fast, srd, smd, interventions, entry, exit}
    extra: table -> raw rows appended as-is (prior-year records, second schools, transfers)."""
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
            "entryDate": s.get("entry", DAYS[0]), "entryGradeLevelDescriptor": GRADE_DESCRIPTORS[g],
            "exitWithdrawDate": s.get("exit")})
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
                "studentUniqueId": sid, "assessmentIdentifier": f"FAST-{subject}-{admin_date}",
                "academicSubjectDescriptor": subject, "administrationDate": admin_date,
                "whenAssessedGradeLevelDescriptor": GRADE_DESCRIPTORS[g], "performanceLevel": level})
        for flag, program in (("srd", SRD_PROGRAM), ("smd", SMD_PROGRAM)):
            if s.get(flag):
                t["studentProgramAssociations"].append({
                    "studentUniqueId": sid, "programName": program, "beginDate": DAYS[1], "endDate": None})
        for i in range(s.get("interventions", 0)):
            t["studentInterventionAssociations"].append({
                "interventionIdentificationCode": f"I-{sid}-{i}", "studentUniqueId": sid,
                "schoolId": SCHOOL, "interventionCategory": "Test", "beginDate": DAYS[5],
                "parentInvolved": True, "notes": ""})
    for table, rows in (extra or {}).items():
        t[table].extend(rows)
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


# ---- LVL1 (d) and DEF (e) ----------------------------------------------------
def test_grade_3_level_1_plus_reading_deficiency_is_two_indicators(tmp_path):
    """(d) and (e) are separate statutory indicators: together they require a team decision."""
    e = build(tmp_path, {
        "both": {"grade": 3, "fast": [("ELA", DAYS[0], 1)], "srd": True},
        "fast": {"grade": 3, "fast": [("Mathematics", DAYS[0], 1)]},
        "srd": {"grade": 3, "srd": True},
    })
    f = flags(e, "both")
    assert (f["lvl1"], f["def"], f["indicator_count"], f["indicators"]) == (True, True, 2, "LVL1,DEF")
    assert e.query("SELECT count(*) AS n FROM watchlist WHERE student_id = 'both'")[0]["n"] == 1
    assert (flags(e, "fast")["lvl1"], flags(e, "fast")["def"]) == (True, False)
    assert (flags(e, "srd")["lvl1"], flags(e, "srd")["def"]) == (False, True)


def test_math_deficiency_counts_through_grade_4_only(tmp_path):
    e = build(tmp_path, {
        "kSmd": {"grade": 0, "smd": True},
        "g4smd": {"grade": 4, "smd": True},
        "g5smd": {"grade": 5, "smd": True},
    })
    assert flags(e, "kSmd")["def"] is True
    assert flags(e, "g4smd")["def"] is True
    assert flags(e, "g5smd")["def"] is False
    assert flags(e, "g5smd")["indicator_count"] == 0


def test_lvl1_and_def_grade_bounds(tmp_path):
    e = build(tmp_path, {
        "g2fast": {"grade": 2, "fast": [("ELA", DAYS[0], 1)]},  # statewide assessment starts at grade 3
        "g4srd": {"grade": 4, "srd": True},                     # reading deficiency ends at grade 3
        "kSrd": {"grade": 0, "srd": True},
        "g8fast": {"grade": 8, "fast": [("Mathematics", DAYS[0], 1)]},
    })
    assert flags(e, "g2fast")["lvl1"] is False
    assert flags(e, "g4srd")["def"] is False
    assert flags(e, "kSrd")["def"] is True and flags(e, "kSrd")["lvl1"] is False
    assert flags(e, "g8fast")["lvl1"] is True


def test_lvl1_uses_most_recent_fast_result(tmp_path):
    e = build(tmp_path, {"up": {"grade": 6, "fast": [("ELA", date(2026, 5, 1), 1), ("ELA", DAYS[1], 3)]}})
    assert flags(e, "up")["lvl1"] is False


def test_later_assessment_without_level_does_not_mask_level_1(tmp_path):
    """A fall PM1 row with no level must not hide last spring's PM3 Level 1."""
    e = build(tmp_path, {"pm": {"grade": 6, "fast": [("ELA", date(2026, 5, 12), 1), ("ELA", DAYS[1], None)]}})
    assert flags(e, "pm")["lvl1"] is True


def test_assessment_older_than_prior_year_is_ignored(tmp_path):
    e = build(tmp_path, {"old": {"grade": 7, "fast": [("ELA", date(2025, 5, 12), 1)]}})
    assert flags(e, "old")["lvl1"] is False


# ---- School-year scoping and enrollment ---------------------------------------
def test_prior_year_suspension_and_failure_do_not_count(tmp_path):
    """Review probe: a prior-year OSS plus a prior-year Q4 F must not put a student at 2+."""
    e = build(tmp_path, {"py": {"grade": 6}}, extra={
        "disciplineActions": [{
            "disciplineActionIdentifier": "py-oss", "studentUniqueId": "py", "responsibilitySchoolId": SCHOOL,
            "disciplineDate": date(2026, 3, 10), "disciplineDescriptor": "Out-of-School Suspension",
            "incidentIdentifier": "py-oss", "actualDisciplineActionLength": 3}],
        "grades": [{
            "studentUniqueId": "py", "schoolId": SCHOOL, "localCourseCode": "ELA",
            "academicSubjectDescriptor": "ELA", "gradingPeriodDescriptor": "Fourth Nine Weeks",
            "letterGradeEarned": "F", "gradePostedDate": date(2026, 5, 28)}],
    })
    f = flags(e, "py")
    assert (f["sus"], f["crs"], f["suspensions"], f["indicator_count"]) == (False, False, 0, 0)


def test_oss_days_count_as_absences(tmp_path):
    e = build(tmp_path, {"oss": {"grade": 6}, "iss": {"grade": 6}}, extra={
        "disciplineActions": [
            {"disciplineActionIdentifier": "o1", "studentUniqueId": "oss", "responsibilitySchoolId": SCHOOL,
             "disciplineDate": DAYS[3], "disciplineDescriptor": "Out-of-School Suspension",
             "incidentIdentifier": "o1", "actualDisciplineActionLength": 2},
            {"disciplineActionIdentifier": "i1", "studentUniqueId": "iss", "responsibilitySchoolId": SCHOOL,
             "disciplineDate": DAYS[3], "disciplineDescriptor": "In-School Suspension",
             "incidentIdentifier": "i1", "actualDisciplineActionLength": 2}],
    })
    o = flags(e, "oss")
    assert (o["days_absent"], o["att_rate"], o["att"], o["sus"]) == (2, 80.0, True, True)
    assert o["indicators"] == "ATT,SUS"
    i = flags(e, "iss")
    assert (i["days_absent"], i["att"], i["sus"]) == (0, False, True)


def test_transfer_is_one_student_with_days_enrolled_across_schools(tmp_path):
    other = 990998
    e = build(tmp_path, {"tr": {"grade": 5, "exit": DAYS[4], "absences": 1}}, extra={
        "schools": [{"schoolId": other, "shortName": "TS2", "nameOfInstitution": "Test 2",
                     "lowGrade": 0, "highGrade": 8}],
        "calendarDates": [{"schoolId": other, "date": d, "calendarEventDescriptor": "Instructional day"}
                          for d in DAYS],
        "studentSchoolAssociations": [{
            "studentUniqueId": "tr", "schoolId": other, "schoolYear": 2027, "entryDate": DAYS[4],
            "entryGradeLevelDescriptor": GRADE_DESCRIPTORS[5], "exitWithdrawDate": None}],
        "studentSchoolAttendanceEvents": [{
            "studentUniqueId": "tr", "schoolId": other, "eventDate": DAYS[8],
            "attendanceEventCategoryDescriptor": "Unexcused Absence"}],
    })
    rows = e.query("SELECT * FROM student_indicators WHERE student_id = 'tr'")
    assert len(rows) == 1
    assert rows[0]["school"] == "TS2"
    assert (rows[0]["days_enrolled"], rows[0]["days_absent"]) == (10, 2)  # 4 at TST + 6 at TS2


def test_withdrawn_student_and_prior_year_enrollment_are_out_of_scope(tmp_path):
    e = build(tmp_path, {"gone": {"grade": 5, "exit": DAYS[3]}, "here": {"grade": 5}}, extra={
        "studentSchoolAssociations": [{
            "studentUniqueId": "here", "schoolId": SCHOOL, "schoolYear": 2026, "entryDate": date(2025, 8, 11),
            "entryGradeLevelDescriptor": GRADE_DESCRIPTORS[4], "exitWithdrawDate": date(2026, 5, 29)}],
    })
    ids = [r["student_id"] for r in e.query("SELECT student_id FROM enrollment")]
    assert ids == ["here"]
    assert flags(e, "here")["grade_num"] == 5 and flags(e, "here")["days_enrolled"] == 10


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


def test_only_current_year_interventions_clear_the_flag(tmp_path):
    two = {"grade": 6, "actions": ["In-School Suspension"], "grades": {"ELA": "F"}}
    e = build(tmp_path, {"old": dict(two), "future": dict(two)}, extra={
        "studentInterventionAssociations": [
            {"interventionIdentificationCode": "I-old", "studentUniqueId": "old", "schoolId": SCHOOL,
             "interventionCategory": "Test", "beginDate": date(2026, 4, 1), "parentInvolved": True, "notes": ""},
            {"interventionIdentificationCode": "I-fut", "studentUniqueId": "future", "schoolId": SCHOOL,
             "interventionCategory": "Test", "beginDate": date(2026, 12, 1), "parentInvolved": True, "notes": ""},
        ]})
    wl = {r["student_id"]: r for r in e.query("SELECT * FROM watchlist")}
    assert wl["old"]["no_intervention"] is True and wl["old"]["interventions"] == 0
    assert wl["future"]["no_intervention"] is True
    assert e.student("old")["team_decision_required"] is True


def test_add_intervention_validates_dates_and_defaults_to_as_of(tmp_path):
    e = build(tmp_path, {"two": {"grade": 6, "actions": ["In-School Suspension"], "grades": {"ELA": "F"}}})
    for bad in ("1999-01-01", "2026-06-30", "2026-10-31"):
        with pytest.raises(ValueError):
            e.add_intervention("two", "Behavior support plan", bad)
    assert e.query("SELECT no_intervention FROM watchlist")[0]["no_intervention"] is True
    r = e.add_intervention("two", "Behavior support plan")
    assert r["begin_date"] == "2026-10-30"
    assert e.query("SELECT no_intervention FROM watchlist")[0]["no_intervention"] is False
    assert e.student("two")["team_decision_required"] is False


def test_all_five_indicators(tmp_path):
    e = build(tmp_path, {"five": {"grade": 4, "absences": 3, "actions": ["In-School Suspension"],
                                  "grades": {"Mathematics": "F"}, "fast": [("ELA", date(2026, 5, 12), 1)],
                                  "smd": True}})
    f = flags(e, "five")
    assert (f["indicator_count"], f["indicators"]) == (5, "ATT,SUS,CRS,LVL1,DEF")
