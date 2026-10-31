-- Florida K-8 Early Warning System indicators, s. 1001.42(18)(b), F.S.
--
-- Pure SQL views over Ed-Fi-shaped base tables. Thresholds come from
-- THRESHOLDS in engine.py and are substituted as named placeholders at load time.
-- Every view is recomputed on read, so a new intervention row shows up in the
-- watchlist immediately.

CREATE OR REPLACE VIEW grade_levels AS
SELECT * FROM (VALUES
    ('Kindergarten', 0, 'K'), ('First grade', 1, '1'), ('Second grade', 2, '2'),
    ('Third grade', 3, '3'), ('Fourth grade', 4, '4'), ('Fifth grade', 5, '5'),
    ('Sixth grade', 6, '6'), ('Seventh grade', 7, '7'), ('Eighth grade', 8, '8')
) t(descriptor, grade_num, grade_label);

-- Students in scope: enrolled K-8 this school year.
CREATE OR REPLACE VIEW enrollment AS
SELECT
    ssa.studentUniqueId                      AS student_id,
    s.firstName || ' ' || s.lastSurname      AS student_name,
    sc.shortName                             AS school,
    sc.schoolId                              AS school_id,
    g.grade_num,
    g.grade_label,
    ssa.entryDate                            AS entry_date
FROM studentSchoolAssociations ssa
JOIN students s      ON s.studentUniqueId = ssa.studentUniqueId
JOIN schools sc      ON sc.schoolId = ssa.schoolId
JOIN grade_levels g  ON g.descriptor = ssa.entryGradeLevelDescriptor
WHERE g.grade_num BETWEEN 0 AND {ews_grade_max};

-- One row per student per instructional day they were enrolled, through as-of.
-- Florida counts all absences, excused or not.
CREATE OR REPLACE VIEW attendance_daily AS
SELECT
    e.student_id,
    c.date AS day,
    CASE WHEN a.studentUniqueId IS NULL THEN 0 ELSE 1 END AS absent
FROM enrollment e
JOIN calendarDates c
  ON c.schoolId = e.school_id
 AND c.calendarEventDescriptor = 'Instructional day'
 AND c.date >= e.entry_date
 AND c.date <= DATE '{as_of_date}'
LEFT JOIN (
    SELECT DISTINCT studentUniqueId, eventDate
    FROM studentSchoolAttendanceEvents
    WHERE attendanceEventCategoryDescriptor IN ('Excused Absence', 'Unexcused Absence')
) a ON a.studentUniqueId = e.student_id AND a.eventDate = c.date;

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

-- One row per (student, indicator) currently in effect, with the date it began.
CREATE OR REPLACE VIEW indicator_events AS
-- ATT: attendance below threshold
SELECT student_id, 'ATT' AS indicator, att_since AS flagged_since
FROM attendance_summary WHERE att_flag

UNION ALL
-- SUS: one or more in-school or out-of-school suspensions
SELECT e.student_id, 'SUS', greatest(min(d.disciplineDate), any_value(e.entry_date))
FROM enrollment e
JOIN disciplineActions d ON d.studentUniqueId = e.student_id
WHERE d.disciplineDescriptor IN ('In-School Suspension', 'Out-of-School Suspension')
  AND d.disciplineDate <= DATE '{as_of_date}'
GROUP BY e.student_id
HAVING count(*) >= {sus_min_count}

UNION ALL
-- CRS: course failure in ELA or math, grades 1-8 only
SELECT e.student_id, 'CRS', greatest(min(gr.gradePostedDate), any_value(e.entry_date))
FROM enrollment e
JOIN grades gr ON gr.studentUniqueId = e.student_id
WHERE e.grade_num BETWEEN {crs_grade_min} AND {crs_grade_max}
  AND gr.academicSubjectDescriptor IN {crs_subjects}
  AND gr.letterGradeEarned IN {crs_failing_grades}
  AND gr.gradePostedDate <= DATE '{as_of_date}'
GROUP BY e.student_id

UNION ALL
-- LVL1: Level 1 on most recent FAST ELA or math (grades 3-8) OR substantial reading
-- deficiency (K-3). Grade 3 can qualify through either path; it is one indicator.
-- Prior-year results count from the student's first day this year.
SELECT student_id, 'LVL1', min(since)
FROM (
    SELECT e.student_id, greatest(a.administrationDate, e.entry_date) AS since
    FROM enrollment e
    JOIN (
        SELECT * FROM studentAssessments
        WHERE academicSubjectDescriptor IN ('ELA', 'Mathematics')
          AND administrationDate <= DATE '{as_of_date}'
        QUALIFY row_number() OVER (
            PARTITION BY studentUniqueId, academicSubjectDescriptor
            ORDER BY administrationDate DESC) = 1
    ) a ON a.studentUniqueId = e.student_id
    WHERE e.grade_num BETWEEN {fast_grade_min} AND {fast_grade_max}
      AND a.performanceLevel = {lvl1_level}

    UNION ALL

    SELECT e.student_id, greatest(p.beginDate, e.entry_date)
    FROM enrollment e
    JOIN studentProgramAssociations p ON p.studentUniqueId = e.student_id
    WHERE e.grade_num <= {srd_grade_max}
      AND p.programName = 'Substantial Reading Deficiency'
      AND p.beginDate <= DATE '{as_of_date}'
      AND (p.endDate IS NULL OR p.endDate > DATE '{as_of_date}')
)
GROUP BY student_id;

CREATE OR REPLACE VIEW student_indicators AS
WITH agg AS (
    SELECT
        student_id,
        bool_or(indicator = 'ATT')  AS att,
        bool_or(indicator = 'SUS')  AS sus,
        bool_or(indicator = 'CRS')  AS crs,
        bool_or(indicator = 'LVL1') AS lvl1,
        count(*)                    AS indicator_count,
        string_agg(indicator, ',' ORDER BY CASE indicator
            WHEN 'ATT' THEN 1 WHEN 'SUS' THEN 2 WHEN 'CRS' THEN 3 ELSE 4 END) AS indicators
    FROM indicator_events
    GROUP BY student_id
),
susp AS (
    SELECT studentUniqueId AS student_id, count(*) AS suspensions
    FROM disciplineActions
    WHERE disciplineDescriptor IN ('In-School Suspension', 'Out-of-School Suspension')
      AND disciplineDate <= DATE '{as_of_date}'
    GROUP BY ALL
)
SELECT
    e.student_id, e.student_name, e.school, e.grade_num, e.grade_label,
    a.days_enrolled, a.days_absent, a.att_rate,
    coalesce(s.suspensions, 0)          AS suspensions,
    coalesce(g.att, false)              AS att,
    coalesce(g.sus, false)              AS sus,
    coalesce(g.crs, false)              AS crs,
    coalesce(g.lvl1, false)             AS lvl1,
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
    count(*) FILTER (WHERE indicator_count >= {min_indicators}) AS two_plus
FROM student_indicators
GROUP BY school, grade_num, grade_label;

-- Students who need a school-based team decision (2+ indicators), how long they
-- have been at 2+, and whether any intervention is on file.
CREATE OR REPLACE VIEW watchlist AS
WITH second_flag AS (
    SELECT student_id, flagged_since
    FROM indicator_events
    QUALIFY row_number() OVER (PARTITION BY student_id ORDER BY flagged_since, indicator) = {min_indicators}
),
iv AS (
    SELECT studentUniqueId AS student_id, count(*) AS interventions, max(beginDate) AS latest_intervention
    FROM studentInterventionAssociations
    GROUP BY ALL
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
LEFT JOIN iv USING (student_id)
WHERE si.indicator_count >= {min_indicators};
