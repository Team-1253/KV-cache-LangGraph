"""실제 그래프에서 품질 판정, 피드백 전달과 1회 재생성 제한을 확인한다."""

import json
from types import SimpleNamespace

import pytest

import app
from agents import report_generation as report, report_quality as quality


@pytest.fixture
def report_graph(monkeypatch):
    for name in (
        "technical_research_agent", "trl_evaluation_node", "market_evaluation_agent",
        "stakeholder_evaluation_agent", "domain_evaluation_agent", "evaluation_synthesis_agent",
    ):
        monkeypatch.setattr(app, name, lambda state: {})

    requests = []
    def generate(messages):
        requests.append(json.loads(messages[1].content))
        return report.ReportNarrative(
            **{field: report.Paragraph(text="확보된 자료가 없다.", reference_ids=[])
               for field in report.ReportNarrative.model_fields if field != "criterion_rationales"},
            criterion_rationales=[],
        )
    monkeypatch.setattr(report, "init_chat_model", lambda *args, **kwargs: SimpleNamespace(
        with_structured_output=lambda schema: SimpleNamespace(invoke=generate),
    ))
    return requests


@pytest.mark.parametrize("failed_item", [
    None, "groundedness", "neutrality", "bias_control", "perspective_coverage",
])
@pytest.mark.parametrize("second_passes", [True, False])
def test_quality_controls_retry_and_sends_feedback(monkeypatch, report_graph, failed_item, second_passes):
    requests = []
    def judge(messages):
        requests.append(json.loads(messages[1].content))
        assert messages[0].content == quality.PROMPT_PATH.read_text(encoding="utf-8")
        checks = {key: True for key in quality.ReportQuality.model_fields if key != "feedback"}
        if failed_item and (len(requests) == 1 or not second_passes):
            checks[failed_item] = False
        return quality.ReportQuality(
            **checks, feedback="근거 부족을 명시한다." if not all(checks.values()) else "",
        )

    monkeypatch.setattr(quality, "init_chat_model", lambda *args, **kwargs: SimpleNamespace(
        with_structured_output=lambda schema: SimpleNamespace(invoke=judge),
    ))
    state = app.build_graph().invoke({"references": []})
    assert len(requests) == len(report_graph) == (2 if failed_item else 1)
    assert state["report_retry_count"] == (1 if failed_item else 0)
    assert requests[-1]["report"] == state["final_report"]
    assert requests[-1]["material"]["references"] == []
    if failed_item:
        assert report_graph[1]["previous_report"] == requests[0]["report"]
        assert report_graph[1]["quality_feedback"]["feedback"] == "근거 부족을 명시한다."
        assert state["report_quality"][failed_item] is second_passes
    assert state.get("run_errors", []) == []


def test_judge_error_is_recorded_without_retry(monkeypatch, report_graph):
    def unavailable(*args, **kwargs):
        raise ConnectionError()
    monkeypatch.setattr(quality, "init_chat_model", unavailable)
    state = app.build_graph().invoke({"references": []})
    assert len(report_graph) == 1
    assert state["report_quality"] == {}
    assert state["run_errors"][0]["stage"] == "report_quality_agent"
    assert state["run_errors"][0]["error_type"] == "ConnectionError"


def test_regeneration_failure_still_stops_after_one_retry(monkeypatch, report_graph):
    calls = []
    def generate(state):
        calls.append(state)
        if len(calls) == 2:
            raise ConnectionError()
        return {"final_report": "초안"}
    monkeypatch.setattr(app, "report_generation_agent", generate)
    monkeypatch.setattr(quality, "init_chat_model", lambda *args, **kwargs: SimpleNamespace(
        with_structured_output=lambda schema: SimpleNamespace(invoke=lambda messages: quality.ReportQuality(
            groundedness=False, neutrality=True, bias_control=True,
            perspective_coverage=True, feedback="근거 부족을 명시한다.",
        )),
    ))
    state = app.build_graph().invoke({"references": []})
    assert len(calls) == 2
    assert state["report_retry_count"] == 1
    assert state["report_quality"]["groundedness"] is False
    assert "부분 결과" in state["final_report"]
    assert state["run_errors"][0]["error_type"] == "ConnectionError"
