"""Analyst agent: answers support-team questions using only engine-sourced numbers.

Online mode uses the Anthropic Python SDK tool-use loop with two tools
(find_students, get_summary). Offline mode (no ANTHROPIC_API_KEY) answers a
few fixed question patterns directly from the engine and says so.

    python -m ews.agent "Which students at SHM have 2+ indicators and no intervention?"
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from ews.engine import INDICATORS, Engine, parse_grade

SYSTEM_PROMPT = """You are an early-warning analyst for a Florida K-8 school district's student
support teams. The five indicators follow s. 1001.42(18)(b)1., F.S.: ATT (attendance below
90%, excused or not, OSS days counted), SUS (one or more suspensions), CRS (ELA or math course
failure, grades 1-8), LVL1 (Level 1 on the statewide ELA or math assessment, grades 3-8), DEF
(substantial reading deficiency in K-3 or substantial mathematics deficiency in K-4). All are
scoped to the current school year. Students with 2 or more indicators need a school-based team
decision with the parent unless already served by a current-year intervention.

Rules:
- Every number, count, rate, name, or student ID you state MUST come from a tool result in
  this conversation. Never estimate, extrapolate, or recall figures. If the tools cannot
  answer, say so plainly.
- Call a tool before answering any quantitative question.
- Name the tool call(s) your figures came from at the end of the answer.
- Refer to students by student ID and initials. Tools never return full names.
- If a tool returns an "error" field, report the error; do not treat it as a zero.
- Be brief and actionable."""

TOOLS = [
    {
        "name": "find_students",
        "description": "List students matching filters, with their indicators, attendance rate, "
                       "intervention count and days flagged at 2+ indicators. Returns {count, students}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "school": {"type": "string", "description": "School code: HCE, CBE, PRE, SHM, GPM, WKA"},
                "grade": {"type": "string", "description": "K or 1-8"},
                "min_indicators": {"type": "integer", "minimum": 0, "maximum": 5},
                "no_intervention": {"type": "boolean",
                                    "description": "true = only students with no current-year intervention on file"},
                "indicator": {"type": "string", "enum": list(INDICATORS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "get_summary",
        "description": "Indicator counts for one school (or the whole district if school is omitted), "
                       "students with 2+ indicators, how many lack an intervention, and counts by grade.",
        "input_schema": {
            "type": "object",
            "properties": {"school": {"type": "string", "description": "School code; omit for district"}},
        },
    },
]

OFFLINE_LABEL = "[OFFLINE MODE - no ANTHROPIC_API_KEY set; canned answer computed directly by the engine]"


def _initials(name: str | None) -> str:
    return "".join(w[0].upper() for w in (name or "").split() if w)


def _minimal(row: dict) -> dict:
    """Strip full names before anything reaches the model: IDs and initials only."""
    out = {k: v for k, v in row.items() if k != "student_name"}
    out["initials"] = _initials(row.get("student_name"))
    return out


def run_tool(engine: Engine, name: str, args: dict) -> dict:
    """Execute one tool call. Bad inputs come back as {"error": ...}, never as a silent zero."""
    try:
        school = engine.normalize_school(args.get("school"))
        if name == "get_summary":
            return engine.summary(school)
        if name == "find_students":
            grade = args.get("grade")
            grade = str(grade).strip().upper() if grade not in (None, "") else None
            parse_grade(grade)
            indicator = args.get("indicator")
            indicator = str(indicator).strip().upper() if indicator else None
            rows = engine.find_students(
                school=school, grade=grade,
                min_indicators=args.get("min_indicators", 0),
                no_intervention=args.get("no_intervention"), indicator=indicator,
                limit=args.get("limit", 50),
            )
            return {"count": len(rows), "students": [_minimal(r) for r in rows]}
    except ValueError as e:
        return {"error": str(e)}
    return {"error": f"unknown tool {name}"}


# ---- online ----------------------------------------------------------------
def ask_online(question: str, engine: Engine, client: Any = None, model: str | None = None,
               max_turns: int = 8) -> str:
    model = model or os.environ.get("ANTHROPIC_MODEL")
    if not model:
        raise RuntimeError("Set ANTHROPIC_MODEL to the Claude model ID you want to use.")
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": question}]
    for _ in range(max_turns):
        resp = client.messages.create(model=model, max_tokens=1500, system=SYSTEM_PROMPT,
                                      tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                out = run_tool(engine, block.name, dict(block.input))
                result = {"type": "tool_result", "tool_use_id": block.id,
                          "content": json.dumps(out, default=str)}
                if "error" in out:
                    result["is_error"] = True
                results.append(result)
            except Exception as e:  # surface tool errors to the model, don't crash
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"Error: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})
    return "Stopped: too many tool calls without a final answer."


# ---- offline ---------------------------------------------------------------
def _school_in(question: str, engine: Engine) -> str | None:
    q = question.lower()
    for s in engine.schools():
        if re.search(rf"\b{s['school'].lower()}\b", q) or s["name"].lower() in q:
            return s["school"]
    return None


def ask_offline(question: str, engine: Engine) -> str:
    q = question.lower()
    school = _school_in(question, engine)
    where = school or "the district"
    grade_m = re.search(r"\bgrade\s*(k|[1-8])\b", q)
    grade = grade_m.group(1).upper() if grade_m else None
    ind = next((i for i in INDICATORS if re.search(rf"\b{i.lower()}\b", q)), None)
    if ind is None and "attendance" in q:
        ind = "ATT"
    elif ind is None and "suspen" in q:
        ind = "SUS"

    if re.search(r"\b(no|without)\s+(an\s+)?intervention", q):
        r = run_tool(engine, "find_students", {"school": school, "min_indicators": 2,
                                               "no_intervention": True, "limit": 200})
        ids = ", ".join(f"{s['student_id']} ({s['indicators']}, {s['days_flagged']}d)"
                        for s in r["students"][:10])
        more = f" (showing 10 of {r['count']})" if r["count"] > 10 else ""
        return (f"{OFFLINE_LABEL}\n{r['count']} students in {where} have 2+ indicators and no "
                f"intervention on file{more}: {ids or 'none'}.\n"
                f"Source: find_students(school={school!r}, min_indicators=2, no_intervention=True)")

    if grade or ind:
        args = {"school": school, "grade": grade, "indicator": ind, "limit": 1000}
        r = run_tool(engine, "find_students", args)
        what = f"flagged {ind}" if ind else "enrolled"
        g = f" in grade {grade}" if grade else ""
        return (f"{OFFLINE_LABEL}\n{r['count']} students{g} in {where} are {what}.\n"
                f"Source: find_students({', '.join(f'{k}={v!r}' for k, v in args.items() if k != 'limit')})")

    if re.search(r"(2\+|two or more|multiple|2 or more)", q) or re.search(r"\b(summary|overview|how is|how are)\b", q):
        s = run_tool(engine, "get_summary", {"school": school})
        i = s["indicators"]
        return (f"{OFFLINE_LABEL}\n{school or 'The district'} as of {s['as_of']}: "
                f"{s['enrolled']} K-8 students. ATT {i['ATT']}, SUS {i['SUS']}, CRS {i['CRS']}, LVL1 {i['LVL1']}, "
                f"DEF {i['DEF']}. "
                f"{s['two_plus']} students have 2+ indicators; {s['two_plus_no_intervention']} of them have "
                f"no current-year intervention on file.\nSource: get_summary(school={school!r})")

    return (f"{OFFLINE_LABEL}\nOffline mode answers only these patterns:\n"
            "  - 'How many students at SHM have 2+ indicators?' / 'Summary for HCE'\n"
            "  - 'Which students at GPM have no intervention?'\n"
            "  - 'How many grade 7 students at SHM are flagged for attendance?'\n"
            "Set ANTHROPIC_API_KEY and ANTHROPIC_MODEL for open-ended questions.")


def ask(question: str, engine: Engine | None = None) -> str:
    engine = engine or Engine()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ask_online(question, engine)
    return ask_offline(question, engine)


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Summary for the district"
    print(ask(question))


if __name__ == "__main__":
    main()
