from ews.synth import generate


def test_synth_is_deterministic():
    assert generate(seed=7) == generate(seed=7)
    assert generate(seed=7) != generate(seed=8)


def test_synth_shape():
    t = generate(seed=42)
    assert len(t["schools"]) == 6
    assert len(t["students"]) == 770


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "as_of": "2026-10-30", "students": 770}


def test_summary_district_and_school(client):
    d = client.get("/summary").json()
    assert d["school"] == "ALL" and d["enrolled"] == 770
    assert sum(g["enrolled"] for g in d["by_grade"]) == 770
    assert sum(g["two_plus"] for g in d["by_grade"]) == d["two_plus"]

    s = client.get("/summary", params={"school": "hce"}).json()
    assert s["school"] == "HCE" and s["enrolled"] == 130
    assert [g["grade"] for g in s["by_grade"]] == ["K", "1", "2", "3", "4", "5"]
    assert s["two_plus_no_intervention"] <= s["two_plus"]


def test_summary_unknown_school_404(client):
    assert client.get("/summary", params={"school": "XYZ"}).status_code == 404


def test_students_filters(client):
    r = client.get("/students", params={"school": "SHM", "min_indicators": 2,
                                        "no_intervention": True, "limit": 1000}).json()
    summary = client.get("/summary", params={"school": "SHM"}).json()
    assert r["count"] == summary["two_plus_no_intervention"] > 0
    assert all(s["indicator_count"] >= 2 and s["interventions"] == 0 for s in r["students"])
    assert all(s["days_flagged"] is not None for s in r["students"])

    k = client.get("/students", params={"school": "WKA", "grade": "K", "limit": 1000}).json()
    assert k["count"] > 0 and all(s["grade"] == "K" for s in k["students"])
    assert all("CRS" not in s["indicators"] for s in k["students"])

    att = client.get("/students", params={"indicator": "ATT", "limit": 1000}).json()
    assert att["count"] == client.get("/summary").json()["indicators"]["ATT"]


def test_students_bad_params(client):
    assert client.get("/students", params={"grade": "12"}).status_code == 422
    assert client.get("/students", params={"indicator": "GPA"}).status_code == 422
    assert client.get("/students", params={"min_indicators": 5}).status_code == 200
    assert client.get("/students", params={"min_indicators": 6}).status_code == 422
    defs = client.get("/students", params={"indicator": "def", "limit": 1000}).json()
    assert defs["count"] == client.get("/summary").json()["indicators"]["DEF"] > 0


def test_student_detail_and_404(client):
    sid = client.get("/students", params={"min_indicators": 3, "limit": 1}).json()["students"][0]["student_id"]
    s = client.get(f"/students/{sid}").json()
    assert s["team_decision_required"] is (s["interventions"] == [] or not any(
        "2026-07-01" <= i["begin_date"] <= "2026-10-30" for i in s["interventions"]))
    assert len(s["indicator_detail"]) == s["indicator_count"] >= 3
    assert [m["month"] for m in s["attendance_monthly"]] == ["2026-08", "2026-09", "2026-10"]
    assert client.get("/students/0000000").status_code == 404


def test_post_intervention_updates_watchlist(client):
    before = client.get("/students", params={"school": "GPM", "min_indicators": 2,
                                             "no_intervention": True, "limit": 1000}).json()
    sid = before["students"][0]["student_id"]
    r = client.post("/interventions", json={"student_id": sid, "category": "Attendance contract",
                                            "begin_date": "2026-10-30", "notes": "Team met with parent"})
    assert r.status_code == 201 and r.json()["student_id"] == sid
    after = client.get("/students", params={"school": "GPM", "min_indicators": 2,
                                            "no_intervention": True, "limit": 1000}).json()
    assert after["count"] == before["count"] - 1
    assert len(client.get(f"/students/{sid}").json()["interventions"]) == 1
    assert client.post("/interventions", json={"student_id": "nope", "category": "Test x"}).status_code == 404


def test_brief_is_deterministic(client):
    a = client.get("/brief", params={"school": "SHM"})
    b = client.get("/brief", params={"school": "SHM"})
    assert a.status_code == 200 and a.text == b.text
    assert a.text.startswith("WEEKLY EARLY WARNING BRIEF - Sandhill Middle (SHM)")
    two_plus = client.get("/summary", params={"school": "SHM"}).json()["two_plus"]
    assert f"Students with 2+ indicators: {two_plus} " in a.text


def test_post_intervention_rejects_bad_dates(client):
    sid = client.get("/students", params={"min_indicators": 2, "no_intervention": True,
                                          "limit": 1}).json()["students"][0]["student_id"]
    for bad in ("2026-13-45", "not-a-date", "1999-01-01", "2027-01-15"):
        r = client.post("/interventions", json={"student_id": sid, "category": "Attendance contract",
                                                "begin_date": bad})
        assert r.status_code == 422, bad
    still = client.get("/students", params={"min_indicators": 2, "no_intervention": True, "limit": 1000}).json()
    assert sid in {s["student_id"] for s in still["students"]}
    ok = client.post("/interventions", json={"student_id": sid, "category": "Attendance contract"})
    assert ok.status_code == 201 and ok.json()["begin_date"] == "2026-10-30"
