"""실제 노드 연결에서 공통 평가·출처 계약과 프롬프트 사용을 확인한다."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

import app
from agents import technical_research as technical, domain_evaluation as domain
from agents import market_evaluation as market, stakeholder_evaluation as stakeholder
from agents import evaluation_synthesis as synthesis, report_generation as report
from agents.state import PerspectiveResult, Criterion, Reference, evaluation_material


class Retriever:
    def __init__(self, tech_id, *args, **kwargs):
        self.tech_id = tech_id

    def build(self):
        return self

    def search(self, query, k=3):
        return [Document(page_content="source evidence KV cache reduction 93.3% versus DeepSeek 67B.",
                         metadata={"chunk_id": f"{self.tech_id}-0000", "page": 2})]


class Search:
    def __init__(self, **kwargs):
        pass

    def invoke(self, request):
        return {"results": [{"title": "Actual source", "url": "https://example.test/source",
                             "content": "source evidence", "published_date": "2026-01"}]}


class Model:
    calls = []

    def with_structured_output(self, schema):
        return SimpleNamespace(invoke=lambda messages: self.structured(schema, messages))

    def structured(self, schema, messages):
        self.calls.append((schema, messages))
        request = messages[1].content
        tech_id = "itme" if "ITME" in request else "deepseek_v2_mla"
        source = technical._Source(chunk_id=f"{tech_id}-0000", page=2)
        if schema is technical._Extraction:
            return schema(overview="Measured approach", mechanism=[technical._Evidenced(text="mechanism", source=source)],
                          scope=[technical._Evidenced(text="scope", source=source)],
                          measurements=[technical._Measurement(metric="KV cache", value="93.3%", baseline="DeepSeek 67B", condition="model comparison", source=source)])
        if schema is technical._ImplicitLimits:
            return schema(limits_implicit=[])
        if schema is technical.TrlAssessment:
            return schema(components=[technical.TrlComponent(component="system", trl=6, is_critical=True,
                          evidence="Measured system", reference_ids=[f"technical-{tech_id}-0000"])], rationale="paper only")
        if schema is market.EvidenceJudgement:
            return schema(evidence_score=4, reason="sources found")
        if schema is market.RubricScore:
            return schema(score=4, rationale="supported market", source_indices=[1], evidence=[])
        if schema is stakeholder.TechnologyAssessment:
            rubric = json.loads(stakeholder.RUBRIC_PATH.read_text())
            return schema(criteria=[stakeholder.CriterionAssessment(
                criterion_id=c["id"], status="VERIFIED", score=4, rationale="supported stakeholders",
                evidence=[stakeholder.Evidence(url="https://example.test/source", source_category="INDEPENDENT")],
            ) for c in rubric["criteria"]])
        if schema is domain.DomainAssessment:
            return schema(**{field: domain.CriterionResult(status="EVALUATED", score=1, rationale="source evidence",
                evidence=[domain.Evidence(source=f"{tech_id}-0000", quote="source evidence")]) for field in domain.FIELDS.values()})
        if schema is report.ReportNarrative:
            material = json.loads(request)
            ids = [ref["id"] for ref in material["references"]]
            return schema(**{field: report.Paragraph(text="근거에 따른 해석", reference_ids=ids[:1]) for field in schema.model_fields})
        raise AssertionError(schema)

    def invoke(self, messages):
        self.calls.append((None, messages))
        return SimpleNamespace(content='{"관찰": "관점별 점수를 합산하지 않는다"}')


@pytest.fixture
def offline_models(monkeypatch):
    model = Model()
    model.calls = []
    for module in (technical, domain, market, stakeholder, synthesis, report):
        monkeypatch.setattr(module, "init_chat_model", lambda *args, **kwargs: model)
    for module in (technical, domain):
        monkeypatch.setattr(module, "TechRetriever", Retriever)
    monkeypatch.setattr(domain, "BGEM3Embeddings", lambda: object())
    import rag.embeddings
    monkeypatch.setattr(rag.embeddings, "BGEM3Embeddings", lambda: object())
    for module in (market, stakeholder):
        monkeypatch.setattr(module, "TavilySearch", Search)
    return model


@pytest.fixture
def completed_state(offline_models):
    return app.build_graph().invoke({"selected_technologies": {"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
                                     "target_domain": "데이터센터", "references": []})


def test_all_perspectives_have_the_same_fields_and_real_references(completed_state):
    state = completed_state
    assert state.get("run_errors", []) == []
    references = {ref["id"]: ref for ref in state["references"]}
    assert all(set(ref) == set(Reference.__annotations__) for ref in references.values())
    for key in ("trl_result", "market_result", "stakeholder_result", "domain_result"):
        assert set(state[key]) == {"deepseek_v2_mla", "itme"}
        for evaluation in state[key].values():
            assert set(evaluation) == set(PerspectiveResult.__annotations__)
            assert 0 <= evaluation["coverage"] <= 1
            for criterion in evaluation["criteria"]:
                assert set(criterion) == set(Criterion.__annotations__)
                for evidence in criterion["evidence"]:
                    assert references[evidence["reference_id"]]["tech_id"] == evaluation["tech_id"]
    assert state["market_result"]["itme"]["score"] == 80
    assert state["stakeholder_result"]["itme"]["score"] == 80
    assert state["trl_result"]["itme"]["score"] == [6, 6]


def test_report_connects_each_perspective_and_preserves_values(completed_state):
    text = completed_state["final_report"]
    for perspective in ("technical", "market", "stakeholder", "domain"):
        assert f"[^{perspective}-" in text.split("## REFERENCE")[0]
    assert "80.0 (0-100)" in text
    assert "coverage: **100%**" in text
    assert "DeepSeek 67B" in text and "model comparison" in text
    assert "## 6. 한계점" in text and "## REFERENCE" in text
    material = evaluation_material(completed_state)
    assert len(material["evaluations"]) == 8
    assert all(ref["content"] for ref in material["references"])


def test_every_model_receives_its_markdown_prompt(completed_state, offline_models):
    sections = technical._prompt_sections()
    expected = {
        technical._Extraction: sections["SYSTEM"], technical._ImplicitLimits: sections["SYSTEM"],
        technical.TrlAssessment: sections["TRL_SYSTEM"],
        market.EvidenceJudgement: market.PROMPT_PATH.read_text(), market.RubricScore: market.PROMPT_PATH.read_text(),
        stakeholder.TechnologyAssessment: stakeholder.PROMPT_PATH.read_text(),
        domain.DomainAssessment: domain.PROMPT_PATH.read_text(),
        report.ReportNarrative: report.PROMPT_PATH.read_text(), None: synthesis.PROMPT_PATH.read_text(),
    }
    seen = set()
    for schema, messages in offline_models.calls:
        assert messages[0].content == expected[schema]
        seen.add(schema)
    assert seen == set(expected)


def test_technical_discards_ungrounded_and_placeholder_measurements(monkeypatch, completed_state):
    class InvalidExtractionModel(Model):
        def structured(self, schema, messages):
            result = super().structured(schema, messages)
            if schema is technical._Extraction:
                source = result.measurements[0].source
                result.measurements.append(technical._Measurement(metric="cache", value="10%", baseline="N/A", condition="test", source=source))
                result.claims.append(technical._Claim(text="unsupported", baseline="model", source=technical._Source(chunk_id="missing", page=99)))
                result.measurements[0].source.page = 99
            return result
    monkeypatch.setattr(technical, "init_chat_model", lambda *args, **kwargs: InvalidExtractionModel())
    result = technical.technical_research_agent(completed_state)
    for profile in result["technical_result"].values():
        assert len(profile["measurements"]) == 1
        assert profile["measurements"][0]["source"]["page"] == 2
        assert profile["retrieval"]["dropped_ungrounded"]["measurements"] == 1
        assert profile["retrieval"]["dropped_ungrounded"]["claims"] == 1


def test_trl_with_unknown_critical_evidence_is_withheld(monkeypatch, completed_state):
    class UnknownCriticalModel(Model):
        def structured(self, schema, messages):
            result = super().structured(schema, messages)
            result.components[0].reference_ids = ["invented-id"]
            return result
    monkeypatch.setattr(technical, "init_chat_model", lambda *args, **kwargs: UnknownCriticalModel())
    result = technical.trl_evaluation_node(completed_state)
    assert all(view["score"] is None and view["status"] == "NOT_VERIFIED" for view in result["trl_result"].values())


def test_stakeholder_only_uses_urls_returned_by_search(monkeypatch, completed_state):
    class UnknownUrlModel(Model):
        def structured(self, schema, messages):
            result = super().structured(schema, messages)
            for criterion in result.criteria:
                criterion.evidence[0].url = "https://invented.test/source"
            return result
    monkeypatch.setattr(stakeholder, "init_chat_model", lambda *args, **kwargs: UnknownUrlModel())
    result = stakeholder.stakeholder_evaluation_agent(completed_state)
    assert result["references"] == []
    assert all(view["score"] == 0 and view["coverage"] == 0 for view in result["stakeholder_result"].values())


def test_stakeholder_preserves_search_content_as_evidence(completed_state):
    result = stakeholder.stakeholder_evaluation_agent(completed_state)
    references = {ref["id"]: ref for ref in result["references"]}
    for view in result["stakeholder_result"].values():
        for criterion in view["criteria"]:
            for evidence in criterion["evidence"]:
                assert evidence["text"] == references[evidence["reference_id"]]["content"] == "source evidence"
