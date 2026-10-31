-- Florida K-8 Early Warning System indicators, s. 1001.42(18)(b), F.S.
--
-- Pure SQL views over Ed-Fi-shaped base tables. Thresholds come from
-- THRESHOLDS in engine.py and are substituted as named placeholders at load time.
-- Every view is recomputed on read, so a new intervention row shows up in the
-- watchlist immediately.
--
-- Scope: every indicator is evaluated for the current school year
-- ({school_year_start} .. {as_of_date}). The one deliberate exception is (d): the
-- statewide assessment level in effect in the fall is last spring's PM3, so (d)
-- looks back to {assessment_window_start} (current or immediately prior school year).

CREATE OR REPLACE VIEW grade_levels AS
SELECT * FROM (VALUES
    ('Kindergarten', 0, 'K'), ('First grade', 1, '1'), ('Second grade', 2, '2'),
    ('Third grade', 3, '3'), ('Fourth grade', 4, '4'), ('Fifth grade', 5, '5'),
    ('Sixth grade', 6, '6'), ('Seventh grade', 7, '7'), ('Eighth grade', 8, '8')
) t(descriptor, grade_num, grade_label);

-- Every enrollment span this school year. exitWithdrawDate is treated as exclusive
-- (first day no longer enrolled), so a same-day transfer never double-counts a day.
CREATE OR REPLACE VIEW enrollment_spans AS
SELECT
    ssa.studentUniqueId                                         AS student_id,
    ssa.schoolId                                                AS school_id,
    greatest(ssa.entryDate, DATE '{school_year_start}')         AS entry_date,
    ssa.exitWithdrawDate                                        AS exit_date,
    ssa.entryGradeLevelDescriptor                               AS grade_descriptor
FROM studentSchoolAssociations ssa
WHERE ssa.schoolYear = {school_year}
  AND ssa.entryDate <= DATE '{as_of_date}'
  AND (ssa.exitWithdrawDate IS NULL OR ssa.exitWithdrawDate > ssa.entryDate);

-- Students in scope: one row per student, their current K-8 enrollment as of the as-of date.
-- Students who withdrew before the as-of date and did not re-enroll are out of scope.
CREATE OR REPLACE VIEW enrollment AS
WITH cur AS (
    SELECT * FROM enrollment_spans
    WHERE exit_date IS NULL OR exit_date > DATE '{as_of_date}'
    QUALIFY row_number() OVER (PARTITION BY student_id ORDER BY entry_date DESC, school_id) = 1
),
first_entry AS (
    SELECT student_id, min(entry_date) AS year_entry_date FROM enrollment_spans GROUP BY ALL
)
SELECT
    cur.student_id,
    s.firstName || ' ' || s.lastSurname      AS student_name,
    sc.shortName                             AS school,
    sc.schoolId                              AS school_id,
    g.grade_num,
    g.grade_label,
    cur.entry_date,                          -- entry at the current school
    f.year_entry_date                        -- first entry anywhere this school year
FROM cur
JOIN first_entry f   ON f.student_id = cur.student_id
JOIN students s      ON s.studentUniqueId = cur.student_id
JOIN schools sc      ON sc.schoolId = cur.school_id
JOIN grade_levels g  ON g.descriptor = cur.grade_descriptor
WHERE g.grade_num BETWEEN 0 AND {ews_grade_max};

-- Instructional days each in-scope student was actually enrolled (at any school), through as-of.
CREATE OR REPLACE VIEW enrolled_days AS
SELECT DISTINCT sp.student_id, c.date AS day
FROM enrollment_spans sp
JOIN enrollment e USING (student_id)
JOIN calendarDates c
  ON c.schoolId = sp.school_id
 AND c.calendarEventDescriptor = 'Instructional day'
 AND c.date >= sp.entry_date
 AND (sp.exit_date IS NULL OR c.date < sp.exit_date)
 AND c.date <= DATE '{as_of_date}';

-- (a) counts out-of-school suspension days as absences. Each OSS covers
-- actualDisciplineActionLength enrolled instructional days starting on the discipline date
-- (default 1 when the length is missing).
CREATE OR REPLACE VIEW oss_days AS
WITH ed AS (
    SELECT student_id, day, row_number() OVER (PARTITION BY student_id ORDER BY day) AS n
    FROM enrolled_days
),
oss AS (
    SELECT disciplineActionIdentifier AS action_id, studentUniqueId AS student_id, disciplineDate,
           greatest(coalesce(ceil(actualDisciplineActionLength), 1), 1) AS len
    FROM disciplineActions
    WHERE disciplineDescriptor = 'Out-of-School Suspension'
      AND disciplineDate BETWEEN DATE '{school_year_start}' AND DATE '{as_of_date}'
),
starts AS (
    SELECT o.action_id, o.student_id, any_value(o.len) AS len, min(ed.n) AS n0
    FROM oss o JOIN ed ON ed.student_id = o.student_id AND ed.day >= o.disciplineDate
    GROUP BY o.action_id, o.student_id
)
SELECT DISTINCT ed.student_id, ed.day
FROM starts s
JOIN ed ON ed.student_id = s.student_id AND ed.n >= s.n0 AND ed.n < s.n0 + s.len;

-- One row per student per enrolled instructional day. Florida counts all absences,
-- excused or not, and OSS days.
CREATE OR REPLACE VIEW attendance_daily AS
SELECT
    d.student_id,
    d.day,
    CASE WHEN a.studentUniqueId IS NULL AND o.student_id IS NULL THEN 0 ELSE 1 END AS absent
FROM enrolled_days d
LEFT JOIN (
    SELECT DISTINCT studentUniqueId, eventDate
    FROM studentSchoolAttendanceEvents
    WHERE attendanceEventCategoryDescriptor IN ('Excused Absence', 'Unexcused Absence')
) a ON a.studentUniqueId = d.student_id AND a.eventDate = d.day
LEFT JOIN oss_days o ON o.student_id = d.student_id AND o.day = d.day;

-- Cumulative rate as of each day; integer math so exactly 90% is never "below 90%".
CREATE OR REPLACE VIEW attendance_running AS
SELECT
    student_id,
    day,
    count(*)    OVER w                AS days_enrolled,
    sum(absent) OVER w                AS days_absent,
    count(*) OVER w >= {att_min_days}
    AND (count(*) OVER w - sum(absent) OVER w) * 100
        < {att_threshold_pct} * count(*) OVER w AS below
FROM attendance_daily
WINDOW w AS (PARTITION BY student_id ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW);

CREATE OR REPLACE VIEW attendance_summary AS
WITH last_day AS (
    SELECT * FROM attendance_running
    QUALIFY row_number() OVER (PARTITION BY student_id ORDER BY day DESC) = 1
),
transitions AS (  -- days the student crossed from at/above threshold to below it
    SELECT student_id, day
    FROM (
        SELECT student_id, day, below,
               lag(below, 1, false) OVER (PARTITION BY student_id ORDER BY day) AS prev_below
        FROM attendance_running
    )
    WHERE below AND NOT prev_below
)
SELECT
    l.student_id,
    l.days_enrolled,
    l.days_absent,
    l.days_enrolled - l.days_absent                                AS days_present,
    round((l.days_enrolled - l.days_absent) * 100.0 / l.days_enrolled, 1) AS att_rate,
    l.below                                                        AS att_flag,
    CASE WHEN l.below THEN (SELECT max(t.day) FROM transitions t WHERE t.student_id = l.student_id) END
                                                                   AS att_since
FROM last_day l;

CREATE OR REPLACE VIEW attendance_monthly AS
SELECT
    student_id,
    strftime(day, '%Y-%m')                                   AS month,
    count(*)                                                 AS days_enrolled,
    sum(absent)                                              AS days_absent,
    round((count(*) - sum(absent)) * 100.0 / count(*), 1)    AS att_rate
FROM attendance_daily
GROUP BY ALL;

-- Suspensions this school year (feeds SUS and the suspensions column).
CREATE OR REPLACE VIEW suspensions_current AS
SELECT studentUniqueId AS student_id, disciplineDate
FROM disciplineActions
WHERE disciplineDescriptor IN ('In-School Suspension', 'Out-of-School Suspension')
  AND disciplineDate BETWEEN DATE '{school_year_start}' AND DATE '{as_of_date}';

-- One row per (student, indicator) currently in effect, with the date it began.
CREATE OR REPLACE VIEW indicator_events AS
-- ATT, (18)(b)1.a: attendance below 90%, excused or not, OSS days counted as absences
SELECT student_id, 'ATT' AS indicator, att_since AS flagged_since
FROM attendance_summary WHERE att_flag

UNION ALL
-- SUS, (18)(b)1.b: one or more in-school or out-of-school suspensions this school year
SELECT e.student_id, 'SUS', greatest(min(d.disciplineDate), any_value(e.year_entry_date))
FROM enrollment e
JOIN suspensions_current d ON d.student_id = e.student_id
GROUP BY e.student_id
HAVING count(*) >= {sus_min_count}

UNION ALL
-- CRS, (18)(b)1.c: course failure in ELA or math in any grading period this school year
SELECT e.student_id, 'CRS', greatest(min(gr.gradePostedDate), any_value(e.year_entry_date))
FROM enrollment e
JOIN grades gr ON gr.studentUniqueId = e.student_id
WHERE e.grade_num BETWEEN {crs_grade_min} AND {crs_grade_max}
  AND gr.academicSubjectDescriptor IN {crs_subjects}
  AND gr.letterGradeEarned IN {crs_failing_grades}
  AND gr.gradePostedDate BETWEEN DATE '{school_year_start}' AND DATE '{as_of_date}'
GROUP BY e.student_id

UNION ALL
-- LVL1, (18)(b)1.d: Level 1 on the most recent statewide ELA or math assessment that
-- HAS a level (a later row with no level, e.g. a fall PM1, never hides a Level 1).
SELECT e.student_id, 'LVL1', greatest(min(a.administrationDate), any_value(e.year_entry_date))
FROM enrollment e
JOIN (
    SELECT * FROM studentAssessments
    WHERE academicSubjectDescriptor IN ('ELA', 'Mathematics')
      AND performanceLevel IS NOT NULL
      AND administrationDate BETWEEN DATE '{assessment_window_start}' AND DATE '{as_of_date}'
    QUALIFY row_number() OVER (
        PARTITION BY studentUniqueId, academicSubjectDescriptor
        ORDER BY administrationDate DESC) = 1
) a ON a.studentUniqueId = e.student_id
WHERE e.grade_num BETWEEN {fast_grade_min} AND {fast_grade_max}
  AND a.performanceLevel = {lvl1_level}
GROUP BY e.student_id

UNION ALL
-- DEF, (18)(b)1.e: substantial reading deficiency (K-3) or substantial mathematics
-- deficiency (K-4) under s. 1008.25(5)(a)/(6)(a), active as of the as-of date.
SELECT e.student_id, 'DEF', greatest(min(p.beginDate), any_value(e.year_entry_date))
FROM enrollment e
JOIN studentProgramAssociations p ON p.studentUniqueId = e.student_id
WHERE p.beginDate <= DATE '{as_of_date}'
  AND (p.endDate IS NULL OR p.endDate > DATE '{as_of_date}')
  AND (   (p.programName = 'Substantial Reading Deficiency'     AND e.grade_num <= {srd_grade_max})
       OR (p.programName = 'Substantial Mathematics Deficiency' AND e.grade_num <= {smd_grade_max}))
GROUP BY e.student_id;

CREATE OR REPLACE VIEW student_indicators AS
WITH agg AS (
    SELECT
        student_id,
        bool_or(indicator = 'ATT')  AS att,
        bool_or(indicator = 'SUS')  AS sus,
        bool_or(indicator = 'CRS')  AS crs,
        bool_or(indicator = 'LVL1') AS lvl1,
        bool_or(indicator = 'DEF')  AS def,
        count(*)                    AS indicator_count,
        string_agg(indicator, ',' ORDER BY CASE indicator
            WHEN 'ATT' THEN 1 WHEN 'SUS' THEN 2 WHEN 'CRS' THEN 3 WHEN 'LVL1' THEN 4 ELSE 5 END)
                                    AS indicators
    FROM indicator_events
    GROUP BY student_id
),
susp AS (
    SELECT student_id, count(*) AS suspensions FROM suspensions_current GROUP BY ALL
)
SELECT
    e.student_id, e.student_name, e.school, e.grade_num, e.grade_label,
    a.days_enrolled, a.days_absent, a.att_rate,
    coalesce(s.suspensions, 0)          AS suspensions,
    coalesce(g.att, false)              AS att,
    coalesce(g.sus, false)              AS sus,
    coalesce(g.crs, false)              AS crs,
    coalesce(g.lvl1, false)             AS lvl1,
    coalesce(g.def, false)              AS def,
    coalesce(g.indicator_count, 0)      AS indicator_count,
    coalesce(g.indicators, '')          AS indicators
FROM enrollment e
LEFT JOIN attendance_summary a ON a.student_id = e.student_id
LEFT JOIN susp s               ON s.student_id = e.student_id
LEFT JOIN agg g                ON g.student_id = e.student_id;

-- Counts by school and grade per indicator: the shape schools report.
CREATE OR REPLACE VIEW grade_counts AS
SELECT
    school, grade_num, grade_label,
    count(*)                                         AS enrolled,
    count(*) FILTER (WHERE att)                      AS att,
    count(*) FILTER (WHERE sus)                      AS sus,
    count(*) FILTER (WHERE crs)                      AS crs,
    count(*) FILTER (WHERE lvl1)                     AS lvl1,
    count(*) FILTER (WHERE def)                      AS def,
    count(*) FILTER (WHERE indicator_count >= {min_indicators}) AS two_plus
FROM student_indicators
GROUP BY school, grade_num, grade_label;

-- Interventions that count for the (18)(b)2 decision: begun this school year, on or
-- before the as-of date. Prior-year and future-dated rows do not clear a student.
CREATE OR REPLACE VIEW current_interventions AS
SELECT studentUniqueId AS student_id, count(*) AS interventions, max(beginDate) AS latest_intervention
FROM studentInterventionAssociations
WHERE beginDate BETWEEN DATE '{school_year_start}' AND DATE '{as_of_date}'
GROUP BY ALL;

-- (18)(b)2: students with 2+ indicators, how long they have been at 2+, and whether a
-- current-year intervention is on file. no_intervention = team decision still required.
CREATE OR REPLACE VIEW watchlist AS
WITH second_flag AS (
    SELECT student_id, flagged_since
    FROM indicator_events
    QUALIFY row_number() OVER (PARTITION BY student_id ORDER BY flagged_since, indicator) = {min_indicators}
)
SELECT
    si.student_id, si.student_name, si.school, si.grade_num, si.grade_label,
    si.indicator_count, si.indicators, si.att_rate, si.suspensions,
    coalesce(iv.interventions, 0)                    AS interventions,
    iv.latest_intervention,
    coalesce(iv.interventions, 0) = 0                AS no_intervention,
    sf.flagged_since,
    date_diff('day', sf.flagged_since, DATE '{as_of_date}') AS days_flagged
FROM student_indicators si
JOIN second_flag sf USING (student_id)
LEFT JOIN current_interventions iv USING (student_id)
WHERE si.indicator_count >= {min_indicators};
