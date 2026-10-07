"""공통 출처 자료·노드 실패·보고서 인용을 검증한다."""

import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import app
from agents import report_generation as report, evaluation_synthesis as synthesis
from agents.resilient import continuing_node
from agents.state import evaluation_material
from tests.pipeline.test_agent_flow import completed_state, offline_models, Model


@pytest.mark.parametrize("content", ["논문 [1]의 요약", '```json\n{"관찰": "조건부"}\n```'])
def test_synthesis_preserves_prose_and_json(monkeypatch, completed_state, content):
    model = SimpleNamespace(invoke=lambda messages: SimpleNamespace(content=content))
    monkeypatch.setattr(synthesis, "init_chat_model", lambda *args, **kwargs: model)
    result = synthesis.evaluation_synthesis_agent(completed_state)["evaluation_result"]
    assert result == ({"관찰": "조건부"} if content.startswith("```") else content)


def test_unused_reference_is_not_sent_to_synthesis_or_report(completed_state):
    completed_state["references"].append({"id": "unused", "content": "irrelevant"})
    assert "unused" not in {ref["id"] for ref in evaluation_material(completed_state)["references"]}


def test_unknown_report_reference_is_not_in_the_bibliography(monkeypatch, completed_state):
    class BadReferenceModel(Model):
        def structured(self, schema, messages):
            return schema(**{field: report.Paragraph(text="확보된 자료의 해석", reference_ids=["invented-id"])
                             for field in schema.model_fields})
    monkeypatch.setattr(report, "init_chat_model", lambda *args, **kwargs: BadReferenceModel())
    result = report.report_generation_agent(completed_state)
    assert "invented-id" not in result["final_report"]
    assert "[^market-" in result["final_report"]


def test_report_failure_preserves_the_actual_input(monkeypatch, completed_state):
    def unavailable(*args, **kwargs):
        raise ConnectionError()
    monkeypatch.setattr(report, "init_chat_model", unavailable)
    result = continuing_node(report.report_generation_agent, "final_report")(completed_state)
    assert "DeepSeek 67B" in result["final_report"]
    assert result["run_errors"][0]["error_type"] == "ConnectionError"


def test_all_failed_nodes_still_reach_a_fallback_report():
    failure = Mock(side_effect=ValueError("failed"))
    failure.__name__ = "failed_node"
    names = ("technical_research_agent", "trl_evaluation_node", "market_evaluation_agent",
             "stakeholder_evaluation_agent", "domain_evaluation_agent", "evaluation_synthesis_agent", "report_generation_agent")
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(patch.object(app, name, failure))
        state = app.build_graph().invoke({"references": []})
    assert len(state["run_errors"]) == 7
    assert "부분 결과" in state["final_report"]


def test_user_interrupt_is_not_swallowed():
    def interrupted(state):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        continuing_node(interrupted, "market_result")({})
