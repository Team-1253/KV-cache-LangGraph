"""실제 노드 연결에서 공통 평가·출처 계약과 프롬프트 사용을 확인한다."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.callbacks import BaseCallbackHandler

import app
from agents import technical_research as technical, domain_evaluation as domain
from agents import market_evaluation as market, stakeholder_evaluation as stakeholder
from agents import synthesizer as synthesis
from agents import report_quality as quality
from agents.synthesizer import evaluation_material


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

    def with_structured_output(self, schema, **kwargs):
        return SimpleNamespace(invoke=lambda messages: self.structured(schema, messages))

    def structured(self, schema, messages):
        self.calls.append((schema, messages))
        if schema is app.Plan:
            research = json.loads(messages[1].content)["technical_result"]
            tasks = [{"worker": key, "tech_ids": [tech_id],
                      "instruction": f"{tech_id}의 {key} 근거를 평가", "reason": "기술별 근거가 달라 분리"}
                     for tech_id in research
                     for key in ("trl_result", "market_result", "stakeholder_result", "domain_result")]
            return schema(tasks=getattr(self, "planned_tasks", tasks))
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
        if schema is synthesis.ReportNarrative:
            material = json.loads(request)
            ids = [ref["id"] for ref in material["references"]]
            return schema(
                **{field: synthesis.Paragraph(text="근거에 따른 해석", reference_ids=ids[:1])
                   for field in schema.model_fields if field != "criterion_rationales"},
                criterion_rationales=[],
            )
        if schema is quality.ReportQuality:
            return schema(groundedness=True, neutrality=True, bias_control=True,
                          perspective_coverage=True, feedback="")
        raise AssertionError(schema)



@pytest.fixture
def offline_models(monkeypatch, tmp_path):
    monkeypatch.setattr(synthesis, "OUTPUT_DIR", tmp_path)
    model = Model()
    model.calls = []
    for module in (app, technical, domain, market, stakeholder, synthesis, quality):
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
    # 개별 평가 함수의 기존 dict 계약 검증에 사용하는 입력이다.
    orchestrator_state = app.build_graph().invoke({
        "selected_technologies": {"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
        "target_domain": "데이터센터",
    })
    data = {**orchestrator_state, "references": []}
    references = []
    for result in orchestrator_state["results"]:
        for key, value in result["output"].items():
            if key.endswith("_result"):
                data.setdefault(key, {}).update(value)
        references.extend(result["output"].get("references", []))
    data["references"] = references
    data["run_errors"] = orchestrator_state.get("errors", [])
    data["final_report"] = Path(orchestrator_state["report_uri"]).read_text()
    return data


@pytest.fixture
def orchestrator_state(offline_models):
    return app.build_graph().invoke({"selected_technologies": {"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
                                     "target_domain": "데이터센터"})


def test_parent_collects_results_without_worker_input_fields(orchestrator_state):
    state = orchestrator_state
    assert not {"technical_result", "market_result", "references", "final_report"} & state.keys()
    assert not {"task", "worker", "messages", "task_input", "task_output", "last_error"} & state.keys()
    assert len(state["results"]) == 9
    assert all(result["status"] == "SUCCESS" and isinstance(result["output"], dict)
               for result in state["results"])
    assert "## REFERENCE" in Path(state["report_uri"]).read_text()
    assert state["step_count"] == 1


def test_worker_failure_preserves_other_results(monkeypatch, offline_models):
    def failed_market(data):
        raise ConnectionError("market unavailable")
    monkeypatch.setitem(app.AGENTS, "market_result", failed_market)
    state = app.build_graph().invoke({"target_domain": "데이터센터"})
    results = {result["task"]["worker"]: result for result in state["results"]}
    assert results["market_result"]["status"] == "FAILED"
    assert all(results[key]["status"] == "SUCCESS"
               for key in ("trl_result", "stakeholder_result", "domain_result"))
    assert results["market_result"]["output"]["market_result"] == {}
    assert len(results["stakeholder_result"]["output"]["stakeholder_result"]) == 1
    assert "ConnectionError" in Path(state["report_uri"]).read_text()


def test_parallel_results_are_accumulated_before_one_synthesis(offline_models):
    offline_models.planned_tasks = [
        {"worker": "market_result", "tech_ids": [tech_id], "instruction": "시장 근거 확인", "reason": "기술별 평가"}
        for tech_id in ("deepseek_v2_mla", "itme")
    ]
    state = app.build_graph().invoke({})
    markets = [result for result in state["results"] if result["task"]["worker"] == "market_result"]
    assert len(markets) == 2
    assert len({result["task_id"] for result in markets}) == 2
    assert len(state["results"]) == 3
    assert sum(schema is synthesis.ReportNarrative for schema, _ in offline_models.calls) == 1


def test_worker_evaluates_only_the_assigned_technology_and_instruction(offline_models):
    instruction = "ITME 상용 채택의 독립 근거 확인"
    offline_models.planned_tasks = [{
        "worker": "market_result", "tech_ids": ["itme"],
        "instruction": instruction, "reason": "상용화 근거 보완",
    }]
    state = app.build_graph().invoke({})
    assert len(state["plan"]) == 1 and len(state["results"]) == 2
    result = state["results"][1]
    assert result["task"] == state["plan"][0]
    assert set(result["output"]["market_result"]) == {"itme"}
    assert all(ref["tech_id"] == "itme" for ref in result["output"]["references"])
    for schema, messages in offline_models.calls:
        if schema in (market.EvidenceJudgement, market.RubricScore):
            payload = json.loads(messages[1].content.split("\n", 1)[1])
            assert payload["instruction"] == instruction
            assert payload["technology"].startswith("ITME")


def test_plan_can_group_technologies_without_losing_evaluations(offline_models):
    offline_models.planned_tasks = [
        {"worker": key, "tech_ids": ["deepseek_v2_mla", "itme"],
         "instruction": "동일 관점으로 두 기술의 근거 평가", "reason": "평가 목표가 같아 묶음"}
        for key in ("trl_result", "market_result", "stakeholder_result", "domain_result")
    ]
    state = app.build_graph().invoke({"target_domain": "데이터센터"})
    assert len(state["plan"]) == 4 and len(state["results"]) == 5
    for result in state["results"][1:]:
        assert set(result["output"][result["task"]["worker"]]) == {"deepseek_v2_mla", "itme"}
    assert len(evaluation_material(state)["evaluations"]) == 8
    assert sum(schema is synthesis.ReportNarrative for schema, _ in offline_models.calls) == 1


def test_orchestrator_sees_the_research_and_preserves_task_reasons(orchestrator_state, offline_models):
    messages = next(messages for schema, messages in offline_models.calls if schema is app.Plan)
    research = json.loads(messages[1].content)["technical_result"]
    assert set(research) == {"deepseek_v2_mla", "itme"}
    assert research["itme"]["measurements"][0]["baseline"] == "DeepSeek 67B"
    assert len(orchestrator_state["plan"]) == 8
    assert all(task["instruction"] and task["reason"] for task in orchestrator_state["plan"])


def test_run_evaluation_connects_state_id_to_native_trace_metadata(monkeypatch, offline_models):
    starts = []

    class CaptureTrace(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                           metadata=None, **kwargs):
            starts.append((run_id, parent_run_id, metadata))

    monkeypatch.setattr(app, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    graph = app.build_graph().with_config(callbacks=[CaptureTrace()])
    monkeypatch.setattr(app, "build_graph", lambda: graph)
    state = app.run_evaluation({"target_domain": "데이터센터"})
    root = next(start for start in starts if start[1] is None)
    assert str(root[0]) == state["run_id"]
    assert all(metadata["run_id"] == state["run_id"] for _, _, metadata in starts)
    assert root[2]["pattern"] == "orchestrator-workers"


def test_all_perspectives_have_the_same_fields_and_real_references(completed_state):
    state = completed_state
    assert state.get("run_errors", []) == []
    references = {ref["id"]: ref for ref in state["references"]}
    reference_fields = {"id", "tech_id", "perspective", "title", "url", "date", "page", "content", "metadata"}
    result_fields = {"tech_id", "technology", "perspective", "status", "score", "score_scale",
                     "coverage", "summary", "verdict", "criteria", "metadata"}
    criterion_fields = {"id", "name", "status", "score", "rationale", "evidence", "metadata"}
    assert all(set(ref) == reference_fields for ref in references.values())
    for key in ("trl_result", "market_result", "stakeholder_result", "domain_result"):
        assert set(state[key]) == {"deepseek_v2_mla", "itme"}
        for evaluation in state[key].values():
            assert set(evaluation) == result_fields
            assert 0 <= evaluation["coverage"] <= 1
            for criterion in evaluation["criteria"]:
                assert set(criterion) == criterion_fields
                for evidence in criterion["evidence"]:
                    assert references[evidence["reference_id"]]["tech_id"] == evaluation["tech_id"]
    assert state["market_result"]["itme"]["score"] == 80
    assert state["stakeholder_result"]["itme"]["score"] == 80
    assert state["trl_result"]["itme"]["score"] == [6, 6]


def test_report_connects_each_perspective_and_preserves_values(completed_state):
    text = completed_state["final_report"]
    for perspective in ("기술 성숙도", "시장성", "이해관계자", "도메인 적용성"):
        assert f"— {perspective}" in text.split("## REFERENCE")[0]
    assert "[^1]" in text.split("## REFERENCE")[0]
    assert "80.0 (0-100)" in text
    assert "근거 확보율: **100%**" in text
    assert "DeepSeek 67B" in text and "model comparison" in text
    assert "## 6. 한계점" in text and "## REFERENCE" in text
    material = evaluation_material(completed_state)
    assert len(material["evaluations"]) == 8
    assert all(ref["content"] for ref in material["references"])


def test_every_model_receives_its_markdown_prompt(completed_state, offline_models):
    sections = technical._prompt_sections()
    expected = {
        app.Plan: app.PLANNER_PROMPT.read_text(),
        technical._Extraction: sections["SYSTEM"], technical._ImplicitLimits: sections["SYSTEM"],
        technical.TrlAssessment: sections["TRL_SYSTEM"],
        market.EvidenceJudgement: market.PROMPT_PATH.read_text(), market.RubricScore: market.PROMPT_PATH.read_text(),
        stakeholder.TechnologyAssessment: stakeholder.PROMPT_PATH.read_text(),
        domain.DomainAssessment: domain.PROMPT_PATH.read_text(),
        synthesis.ReportNarrative: synthesis.PROMPT_PATH.read_text(),
        quality.ReportQuality: quality.PROMPT_PATH.read_text(),
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
