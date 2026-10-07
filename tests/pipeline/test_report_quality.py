"""품질 판정, 보고서 피드백 전달, 재작성 상한과 오류 종료를 검증한다."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app
from agents import synthesizer as report, report_quality as quality
from tests.pipeline.test_agent_flow import offline_models


@pytest.fixture
def report_graph(monkeypatch, offline_models):
    requests = []

    def generate(messages):
        requests.append(json.loads(messages[1].content))
        return report.ReportNarrative(
            **{field: report.Paragraph(text="확보된 자료에 따른 해석.", reference_ids=[])
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
    state = app.build_graph().invoke({"target_domain": "데이터센터"})
    assert len(requests) == len(report_graph) == state["step_count"] == (2 if failed_item else 1)
    assert requests[-1]["report"] == Path(state["report_uri"]).read_text()
    assert requests[-1]["material"]["references"]
    # 재작성해도 조사·평가 Worker를 반복하거나 results를 중복 누적하지 않는다.
    assert len(state["results"]) == 9
    if failed_item:
        assert report_graph[1]["previous_report"] == requests[0]["report"]
        assert report_graph[1]["quality_feedback"]["feedback"] == "근거 부족을 명시한다."
        assert state["quality"][failed_item] is second_passes
    assert state["status"] == ("PARTIAL" if failed_item and not second_passes else "SUCCESS")
    assert state.get("errors", []) == []


def test_judge_error_is_recorded_without_retry(monkeypatch, report_graph):
    def unavailable(*args, **kwargs):
        raise ConnectionError()

    monkeypatch.setattr(quality, "init_chat_model", unavailable)
    state = app.build_graph().invoke({})
    assert len(report_graph) == state["step_count"] == 1
    assert state["quality"] == {} and state["status"] == "FAILED"
    assert state["errors"][0]["stage"] == "report_quality"
    assert state["errors"][0]["error_type"] == "ConnectionError"
    assert Path(state["report_uri"]).exists()


def test_regeneration_failure_still_stops_after_one_retry(monkeypatch, report_graph):
    calls = []
    original = report.init_chat_model

    def model(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ConnectionError()
        return original(*args, **kwargs)

    monkeypatch.setattr(report, "init_chat_model", model)
    monkeypatch.setattr(quality, "init_chat_model", lambda *args, **kwargs: SimpleNamespace(
        with_structured_output=lambda schema: SimpleNamespace(invoke=lambda messages: quality.ReportQuality(
            groundedness=False, neutrality=True, bias_control=True,
            perspective_coverage=True, feedback="근거 부족을 명시한다.",
        )),
    ))
    state = app.build_graph().invoke({})
    assert len(calls) == state["step_count"] == 2
    assert state["quality"]["groundedness"] is False
    assert state["status"] == "FAILED"
    assert "부분 결과" in Path(state["report_uri"]).read_text()
    assert state["errors"][0]["error_type"] == "ConnectionError"


def test_max_steps_can_disable_regeneration(monkeypatch, report_graph):
    monkeypatch.setattr(quality, "init_chat_model", lambda *args, **kwargs: SimpleNamespace(
        with_structured_output=lambda schema: SimpleNamespace(invoke=lambda messages: quality.ReportQuality(
            groundedness=False, neutrality=True, bias_control=True,
            perspective_coverage=True, feedback="근거 부족을 명시한다.",
        )),
    ))
    state = app.build_graph().invoke({"max_steps": 1})
    assert len(report_graph) == state["step_count"] == 1
    assert state["status"] == "PARTIAL"
