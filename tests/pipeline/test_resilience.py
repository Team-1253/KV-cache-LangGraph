"""공통 출처 자료·노드 실패·보고서 인용을 검증한다."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import app
from agents import synthesizer as synthesis
from agents.synthesizer import evaluation_material
from tests.pipeline.test_agent_flow import completed_state, orchestrator_state, offline_models, Model


def test_synthesizer_uses_raw_evaluations_in_one_model_call(orchestrator_state, offline_models):
    calls = [messages for schema, messages in offline_models.calls
             if schema is synthesis.ReportNarrative]
    assert len(calls) == 1
    material = json.loads(calls[0][1].content)
    assert {result["perspective"] for result in material["evaluations"]} == {
        "trl", "market", "stakeholder", "domain"
    }
    assert "evaluation_result" not in material
    assert "## 5. 시사점" in Path(orchestrator_state["report_uri"]).read_text()


def test_unused_reference_is_not_sent_to_synthesizer(orchestrator_state):
    orchestrator_state["results"][0]["output"]["references"].append({"id": "unused", "content": "irrelevant"})
    assert "unused" not in {ref["id"] for ref in evaluation_material(orchestrator_state)["references"]}


def test_unknown_report_reference_is_not_in_the_bibliography(monkeypatch, orchestrator_state):
    class BadReferenceModel(Model):
        def structured(self, schema, messages):
            return schema(**{field: synthesis.Paragraph(text="확보된 자료의 해석", reference_ids=["invented-id"])
                             for field in schema.model_fields if field != "criterion_rationales"},
                          criterion_rationales=[])
    monkeypatch.setattr(synthesis, "init_chat_model", lambda *args, **kwargs: BadReferenceModel())
    result = synthesis.synthesizer(orchestrator_state)
    text = Path(result["report_uri"]).read_text()
    assert "invented-id" not in text
    assert "[^1]:" in text


def test_report_failure_preserves_the_actual_input(monkeypatch, orchestrator_state):
    def unavailable(*args, **kwargs):
        raise ConnectionError()
    monkeypatch.setattr(synthesis, "init_chat_model", unavailable)
    result = synthesis.synthesizer(orchestrator_state)
    assert "DeepSeek 67B" in Path(result["report_uri"]).read_text()
    assert result["errors"][0]["error_type"] == "ConnectionError"


def test_all_failed_nodes_still_reach_a_fallback_report(monkeypatch, tmp_path, offline_models):
    monkeypatch.setattr(synthesis, "OUTPUT_DIR", tmp_path)
    failure = Mock(side_effect=ValueError("failed"))
    failure.__name__ = "failed_node"
    for key in app.AGENTS:
        monkeypatch.setitem(app.AGENTS, key, failure)
    monkeypatch.setattr(synthesis, "init_chat_model", failure)
    state = app.build_graph().invoke({})
    assert len(state["errors"]) == 2
    assert state["plan"] == [] and len(state["results"]) == 1
    assert state["status"] == "FAILED"
    assert "부분 결과" in Path(state["report_uri"]).read_text()


def test_user_interrupt_is_not_swallowed(monkeypatch):
    def interrupted(state):
        raise KeyboardInterrupt()
    monkeypatch.setitem(app.AGENTS, "market_result", interrupted)
    with pytest.raises(KeyboardInterrupt):
        app.worker({"task": {"task_id": "task-1", "worker": "market_result"}, "task_input": {}})
