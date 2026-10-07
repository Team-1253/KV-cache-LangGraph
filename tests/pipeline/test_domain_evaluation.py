"""도메인 원문 인용과 가중치·미확인 점수 계약을 검증한다."""

import json

import pytest

from agents import domain_evaluation as domain
from tests.pipeline.test_agent_flow import completed_state, offline_models, Model


@pytest.mark.parametrize("quote,verified", [("source evidence", True), ("한국어 번역 근거", False)])
def test_only_original_quotes_are_accepted(monkeypatch, completed_state, quote, verified):
    class QuoteModel(Model):
        def structured(self, schema, messages):
            result = super().structured(schema, messages)
            for field in domain.FIELDS.values():
                getattr(result, field).evidence[0].quote = quote
            return result
    monkeypatch.setattr(domain, "init_chat_model", lambda *args, **kwargs: QuoteModel())
    result = domain.domain_evaluation_agent(completed_state)
    for view in result["domain_result"].values():
        assert (view["coverage"] > 0) is verified
    if not verified:
        assert not result["references"]
        assert all(v["score"] == 0 and v["verdict"].startswith("판정 보류") for v in result["domain_result"].values())


def test_unverified_weight_remains_zero_in_the_total(monkeypatch, completed_state):
    class PartialModel(Model):
        def structured(self, schema, messages):
            result = super().structured(schema, messages)
            for field in domain.FIELDS.values():
                getattr(result, field).status = "NOT_VERIFIED"
            result.memory_efficiency.status = "EVALUATED"
            result.memory_efficiency.score = 5
            return result
    monkeypatch.setattr(domain, "init_chat_model", lambda *args, **kwargs: PartialModel())
    rubric = json.loads(domain.RUBRIC_PATH.read_text())
    weight = float(rubric["criteria"][0]["weight"])
    result = domain.domain_evaluation_agent(completed_state)
    for view in result["domain_result"].values():
        assert view["score"] == round(5 * weight, 2)
        assert view["coverage"] == weight
        assert view["verdict"].startswith("판정 보류")
