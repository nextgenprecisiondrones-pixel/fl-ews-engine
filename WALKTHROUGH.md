# Five-minute walkthrough

Concept by Sal Abuawad. Not affiliated with any vendor or school district. All student data is synthetic.

Every number below comes from seed 42 with the as-of date 2026-10-30. Run the commands from the
repo root.

## 1. Install and generate data (about 30 seconds)

```bash
pip install -e ".[dev]"
make data
```

Expected:

```
Wrote 10 tables to data/ (seed=42)
  schools                                 6 rows
  students                              770 rows
  studentSchoolAssociations             770 rows
  calendarDates                         354 rows
  studentSchoolAttendanceEvents        2601 rows
  disciplineActions                     147 rows
  grades                               3080 rows
  studentAssessments                    938 rows
  studentProgramAssociations             41 rows
  studentInterventionAssociations       190 rows
```

The files are Ed-Fi-shaped: camelCase fields and descriptor strings such as
`entryGradeLevelDescriptor = "Third grade"`. Open `data/disciplineActions.csv` to see the mix of
suspensions and actions that must not count, such as `Detention`.

## 2. Read the rules (1 minute)

Open `ews/rules.sql`. Each indicator is one branch of the `indicator_events` view:

- **ATT** compares `days_present * 100 < 90 * days_enrolled` with integer math, so 90.0% never flags.
- **CRS** is limited to grades 1-8, so kindergarten cannot be flagged.
- **LVL1** combines FAST Level 1 (grades 3-8) and substantial reading deficiency (K-3) and groups by
  student, so a 3rd grader who meets both paths still has one indicator.

`student_indicators`, `grade_counts` and `watchlist` are built on top of it. The thresholds come
from `THRESHOLDS` in `ews/engine.py`.

## 3. Run the tests (15 seconds)

```bash
make test
```

Expected: `28 passed`. The tests in `tests/test_rules.py` build a 10-day, single-school fixture by
hand and check each edge case: exactly 90% attendance, no CRS in kindergarten, both LVL1 paths in
grade 3, the grade limits on each LVL1 path, the most recent FAST result winning, and 2+ counting
with days flagged.

## 4. Start the API and query it (2 minutes)

```bash
make run          # leave running; use a second terminal
```

```bash
curl -s localhost:8000/health
```
```json
{"status":"ok","as_of":"2026-10-30","students":770}
```

School summary:

```bash
curl -s 'localhost:8000/summary?school=HCE'
```
```json
{"school":"HCE","as_of":"2026-10-30","enrolled":130,
 "indicators":{"ATT":31,"SUS":3,"CRS":19,"LVL1":24},
 "two_plus":19,"two_plus_no_intervention":6,"median_days_flagged_no_intervention":28.5,
 "by_grade":[{"grade":"K","enrolled":22,"ATT":7,"SUS":1,"CRS":0,"LVL1":2,"two_plus":3}, ...]}
```

Kindergarten shows `CRS: 0`, as the statute requires.

Students who need a team decision and have no intervention on file, longest flagged first within
each indicator count:

```bash
curl -s 'localhost:8000/students?school=SHM&min_indicators=2&no_intervention=true&limit=3'
```
```json
{"count":3,"students":[
 {"student_id":"7000461","school":"SHM","grade":"7","indicator_count":4,"indicators":"ATT,SUS,CRS,LVL1",
  "att_rate":81.4,"suspensions":1,"interventions":0,"flagged_since":"2026-09-09","days_flagged":51, ...},
 {"student_id":"7000507", ..., "indicators":"ATT,SUS,LVL1","days_flagged":81},
 {"student_id":"7000394", ..., "indicators":"ATT,LVL1","days_flagged":65}]}
```

Drill into one student:

```bash
curl -s localhost:8000/students/7000461
```

The `indicator_detail` field shows LVL1 from 2026-08-10, ATT from 2026-09-09, SUS from 2026-10-08
and CRS from 2026-10-23. The response also has `"team_decision_required": true` and
`"two_plus_since": "2026-09-09"`, which is the date the second indicator started.

Log the team's decision:

```bash
curl -s -X POST localhost:8000/interventions -H 'content-type: application/json' \
  -d '{"student_id":"7000461","category":"Check-in/check-out mentoring","begin_date":"2026-10-30","notes":"Team met with parent"}'
```
```json
{"id":"INT-API-00191","student_id":"7000461","category":"Check-in/check-out mentoring","begin_date":"2026-10-30","parent_involved":true,"notes":"Team met with parent"}
```

Run the no-intervention query again. 7000461 is no longer listed, and the SHM count drops from 11
to 10.

The weekly brief gives the same text for the same data:

```bash
curl -s 'localhost:8000/brief?school=WKA'
```
```
WEEKLY EARLY WARNING BRIEF - Withlacoochee K-8 Academy (WKA)
Data as of 2026-10-30. Synthetic data. Rules: s. 1001.42(18)(b), F.S.

Enrolled K-8: 120
Indicator counts: ATT 29 | SUS 6 | CRS 19 | LVL1 35
Students with 2+ indicators: 22 (12 with no intervention on file)

By grade (enrolled / ATT / SUS / CRS / LVL1 / 2+):
  Grade  K:  14 /  2 /  0 /  0 /  3 /  1
  ...
Team decision needed - longest flagged without an intervention:
  - 7000749 (AZ, gr 8): ATT,SUS,CRS,LVL1, flagged 70 days
  ...
```

The brief uses student IDs and initials instead of full names.

## 5. Ask the analyst agent (1 minute)

Without an API key, the agent runs in offline mode and labels its output:

```bash
python -m ews.agent "How many students at SHM have 2+ indicators?"
```
```
[OFFLINE MODE - no ANTHROPIC_API_KEY set; canned answer computed directly by the engine]
SHM as of 2026-10-30: 140 K-8 students. ATT 24, SUS 12, CRS 18, LVL1 50. 25 students have 2+ indicators; 11 of them have no intervention on file.
Source: get_summary(school='SHM')
```

```bash
python -m ews.agent "How many grade 7 students at SHM are flagged for attendance?"
```
```
[OFFLINE MODE - no ANTHROPIC_API_KEY set; canned answer computed directly by the engine]
8 students in grade 7 in SHM are flagged ATT.
Source: find_students(school='SHM', grade='7', indicator='ATT')
```

To use the live agent, set a key and model, then ask an open-ended question:

```bash
export ANTHROPIC_API_KEY=...            # your key
export ANTHROPIC_MODEL=...              # a current Claude model ID
python -m ews.agent "Which middle-school grade has the most students at 2+ without an intervention, and who has waited longest?"
```

The model calls `get_summary` and `find_students`, and the system prompt requires every number to
come from those results. The CLI agent loads its own engine, so it does not see interventions
posted to a running API process.

## Where to go next

The production notes in `README.md` cover moving to Postgres or a warehouse, SSO, row-level
security by school, FERPA audit logging, and data retention.
