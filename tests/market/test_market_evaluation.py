"""시장 재검색·출처 연결·미확인 점수를 검증한다."""

import json

from agents import market_evaluation as market
from tests.pipeline.test_agent_flow import completed_state, offline_models, Model, Search


def test_retry_queries_stay_short_even_for_long_profiles():
    rubric = json.loads(market.RUBRIC_PATH.read_text())
    profile = {"title": "DeepSeek-V2 MLA", "overview": "long profile " * 1000}
    for criterion in rubric["criteria"]:
        for attempt in (1, 2, 3):
            assert max(map(len, market.build_queries(criterion, profile, attempt))) <= 500


def test_weak_evidence_changes_query_and_stops_after_three_attempts(monkeypatch, completed_state):
    class WeakModel(Model):
        judgement_calls = 0
        def structured(self, schema, messages):
            if schema is market.EvidenceJudgement:
                self.judgement_calls += 1
                return schema(evidence_score=2, reason="weak but available")
            return super().structured(schema, messages)
    model = WeakModel()
    monkeypatch.setattr(market, "init_chat_model", lambda *args, **kwargs: model)
    result = market.market_evaluation_agent(completed_state)
    assert model.judgement_calls == 24  # 두 기술 × 네 항목 × 세 시도
    assert all(c["metadata"]["attempts"] == 3 and c["metadata"]["confidence_tag"] == "약함"
               for view in result["market_result"].values() for c in view["criteria"])


def test_search_failure_is_not_a_negative_evaluation(monkeypatch, completed_state):
    class FailedSearch(Search):
        def invoke(self, request):
            return {"error": RuntimeError("search failed")}
    monkeypatch.setattr(market, "TavilySearch", FailedSearch)
    result = market.market_evaluation_agent(completed_state)
    assert result["run_errors"]
    for view in result["market_result"].values():
        assert view["score"] == 0 and view["coverage"] == 0
        assert all(c["score"] is None and c["status"] == "NOT_VERIFIED" for c in view["criteria"])


def test_model_cannot_cite_an_unknown_search_result(monkeypatch, completed_state):
    class UngroundedModel(Model):
        def structured(self, schema, messages):
            if schema is market.RubricScore:
                return schema(score=5, rationale="unsupported", source_indices=[999], evidence=[])
            return super().structured(schema, messages)
    monkeypatch.setattr(market, "init_chat_model", lambda *args, **kwargs: UngroundedModel())
    result = market.market_evaluation_agent(completed_state)
    assert result["references"] == []
    assert all(v["score"] == 0 for v in result["market_result"].values())
