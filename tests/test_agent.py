from types import SimpleNamespace as NS

from ews import agent


def test_ask_uses_offline_without_key(engine, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = agent.ask("How many students at SHM have 2+ indicators?", engine)
    assert out.startswith(agent.OFFLINE_LABEL)
    s = engine.summary("SHM")
    assert f"{s['two_plus']} students have 2+ indicators" in out
    assert "Source: get_summary(school='SHM')" in out


def test_offline_no_intervention(engine):
    out = agent.ask_offline("Which students at Gulf Pine Middle have no intervention?", engine)
    n = len(engine.find_students("GPM", min_indicators=2, no_intervention=True, limit=1000))
    assert f"{n} students in GPM have 2+ indicators and no intervention" in out


def test_offline_grade_indicator(engine):
    out = agent.ask_offline("How many grade 7 students at SHM are flagged for attendance?", engine)
    n = len(engine.find_students("SHM", grade=7, indicator="ATT", limit=1000))
    assert f"{n} students in grade 7 in SHM are flagged ATT" in out


def test_offline_unknown_question_is_honest(engine):
    out = agent.ask_offline("Predict next year's graduation rate", engine)
    assert "Offline mode answers only these patterns" in out
    assert not any(ch.isdigit() for ch in out.split("\n")[1])  # no invented figures


class FakeClient:
    """Scripted stand-in for anthropic.Anthropic: one tool call, then an answer."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            return NS(stop_reason="tool_use", content=[
                NS(type="tool_use", id="t1", name="get_summary", input={"school": "PRE"})])
        result = kw["messages"][2]["content"][0]
        return NS(stop_reason="end_turn", content=[NS(type="text", text=f"Got: {result['content'][:40]}")])


def test_online_loop_executes_tools_and_requires_tool_numbers(engine):
    fake = FakeClient()
    out = agent.ask_online("Summary for PRE", engine, client=fake, model="test-model")
    assert out.startswith('Got: {"school": "PRE"')
    first = fake.calls[0]
    assert first["model"] == "test-model"
    assert {t["name"] for t in first["tools"]} == {"find_students", "get_summary"}
    assert "MUST come from a tool result" in first["system"]
    msgs = fake.calls[1]["messages"]  # same list the loop mutated
    assert msgs[2]["role"] == "user" and msgs[2]["content"][0]["tool_use_id"] == "t1"
