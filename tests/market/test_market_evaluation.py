"""시장 평가 Agent 단위 테스트.

실제 네트워크/LLM 없이 주입형 fake 의존성으로 상태 전이·채점·조립을 검증한다.
"""

import json

import pytest

from agents.market_evaluation import (
    CONFIRM_THRESHOLD,
    MAX_ATTEMPTS,
    TAG_MEDIUM,
    TAG_NOT_VERIFIED,
    TAG_WEAK,
    EvidenceItem,
    EvidenceJudgement,
    MarketDeps,
    RubricScore,
    _source_type,
    load_rubric,
    map_tag,
    run_market_evaluation,
)
from agents.state import EvaluationState


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _result(url="https://example.com/a", title="Doc", date="2024-05", content="c"):
    return {"title": title, "url": url, "content": content, "published_date": date}


class FakeSearch:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def __call__(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return [dict(r) for r in self._results]


class PerKeyJudge:
    """(기술, 항목)별로 정해진 순서의 evidence_score를 반환한다."""

    def __init__(self, scores):
        self._scores = list(scores)
        self.counts = {}

    def __call__(self, system_prompt, criterion, technology, results):
        key = (technology, criterion["id"])
        index = self.counts.get(key, 0)
        self.counts[key] = index + 1
        score = self._scores[min(index, len(self._scores) - 1)]
        return EvidenceJudgement(evidence_score=score, reason=f"score={score}")

    @property
    def total_calls(self):
        return sum(self.counts.values())


class FakeScorer:
    def __init__(self, score=3, evidence=None):
        self._score = score
        self._evidence = evidence or []
        self.calls = 0

    def __call__(self, system_prompt, criterion, technology, results, evidence_score):
        self.calls += 1
        return RubricScore(
            score=self._score, rationale="rationale", evidence=self._evidence
        )


def make_deps(results, judge_scores, rubric_score=3):
    search = FakeSearch(results)
    judge = PerKeyJudge(judge_scores)
    scorer = FakeScorer(rubric_score)
    return MarketDeps(search, judge, scorer), search, judge, scorer


def _source(chunk_id="c1", page=1):
    return {"chunk_id": chunk_id, "page": page}


def make_state() -> EvaluationState:
    return EvaluationState(
        selected_technologies={"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
        technical_result={
            "deepseek_v2_mla": {
                "tech_id": "deepseek_v2_mla",
                "camp": "SW",
                "title": "DeepSeek-V2 MLA",
                "overview": "MLA compresses KV cache into a low-rank latent vector.",
                "mechanism": [{"text": "joint low-rank compression", "source": _source()}],
                "scope": [{"text": "long-context serving", "source": _source(page=2)}],
                "claims": [
                    {
                        "text": "93.3% KV cache reduction",
                        "baseline": "MHA",
                        "source": _source(page=1),
                    }
                ],
                "measurements": [
                    {
                        "metric": "KV cache",
                        "value": "93.3%",
                        "baseline": "MHA",
                        "condition": "long context",
                        "source": _source(page=5),
                    }
                ],
                "limits_explicit": [],
                "limits_implicit": [
                    {
                        "text": "compute overhead",
                        "basis": "increased FLOPs",
                        "source": _source(page=7),
                    }
                ],
                "evidence_level": "strong",
                "retrieval": {"chunks_used": 3},
            },
            "itme": {
                "tech_id": "itme",
                "camp": "HW",
                "title": "ITME",
                "overview": "CXL-Hybrid memory expansion.",
                "mechanism": [],
                "scope": [],
                "claims": [],
                "measurements": [
                    {
                        "metric": "throughput",
                        "value": "35.7%",
                        "baseline": "CPU-offload",
                        "condition": "expansion turn",
                        "source": _source(chunk_id="c9", page=6),
                    }
                ],
                "limits_explicit": [],
                "limits_implicit": [],
                "evidence_level": "limited",
                "retrieval": {},
            },
        },
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("score", "expected"),
    [(5, "강함"), (4, "보통"), (3, "보통"), (2, "약함"), (1, "약함"), (0, TAG_NOT_VERIFIED)],
)
def test_map_tag(score, expected):
    assert map_tag(score) == expected


def test_returns_only_own_keys():
    deps, *_ = make_deps([_result()], [4])
    out = run_market_evaluation(make_state(), deps)
    assert set(out.keys()) == {"market_result", "references"}


def test_confirm_on_first_attempt():
    deps, search, judge, scorer = make_deps([_result()], [4])
    out = run_market_evaluation(make_state(), deps)

    sw_items = out["market_result"]["deepseek_v2_mla"]["items"]
    assert list(sw_items.keys()) == ["3-2-a", "3-2-b", "3-2-c", "3-2-d"]
    assert all(item["attempts"] == 1 for item in sw_items.values())
    assert all(item["confidence_tag"] == TAG_MEDIUM for item in sw_items.values())
    # 4 items x 2 technologies
    assert judge.total_calls == 8
    assert scorer.calls == 8


def test_weak_retries_then_confirms():
    deps, search, judge, scorer = make_deps([_result()], [2, 2, 4])
    out = run_market_evaluation(make_state(), deps)

    items = out["market_result"]["deepseek_v2_mla"]["items"]
    assert all(item["attempts"] == MAX_ATTEMPTS for item in items.values())
    assert all(item["confidence_tag"] == TAG_MEDIUM for item in items.values())
    assert judge.total_calls == 8 * 3


def test_no_results_is_not_verified():
    deps, search, judge, scorer = make_deps([], [4])
    out = run_market_evaluation(make_state(), deps)

    item = out["market_result"]["deepseek_v2_mla"]["items"]["3-2-a"]
    assert item["attempts"] == MAX_ATTEMPTS
    assert item["confidence_tag"] == TAG_NOT_VERIFIED
    assert item["score"] == 1
    assert judge.total_calls == 0
    assert scorer.calls == 0


def test_weak_kept_when_sources_exist():
    deps, search, judge, scorer = make_deps([_result()], [2])
    out = run_market_evaluation(make_state(), deps)

    item = out["market_result"]["deepseek_v2_mla"]["items"]["3-2-a"]
    assert item["attempts"] == MAX_ATTEMPTS
    assert item["confidence_tag"] == TAG_WEAK
    assert item["score"] == 3  # from fake scorer, not forced to 1
    assert scorer.calls == 8


def test_total_score_formula():
    deps, *_ = make_deps([_result()], [4], rubric_score=4)
    out = run_market_evaluation(make_state(), deps)
    # sum(scores) / 20 * 100 = 16 / 20 * 100 = 80.0
    assert out["market_result"]["deepseek_v2_mla"]["score"] == pytest.approx(80.0)


def test_search_escalates_on_retry():
    deps, search, *_ = make_deps([_result()], [2, 2, 4])
    run_market_evaluation(make_state(), deps)

    depths = {call["depth"] for call in search.calls}
    assert "basic" in depths and "advanced" in depths
    # 3-2-c(채택·상용화)는 news 토픽 사용
    assert any(call["topic"] == "news" for call in search.calls)


def test_references_normalized_and_deduped():
    results = [
        _result(url="https://arxiv.org/abs/2405.1", title="Paper"),
        _result(url="https://example.com/a", title="Blog"),
    ]
    deps, *_ = make_deps(results, [4])
    out = run_market_evaluation(make_state(), deps)

    refs = out["references"]
    urls = [ref["url"] for ref in refs]
    assert len(urls) == len(set(urls))  # 중복 제거
    first = refs[0]
    for field in ("id", "technology", "item", "source", "source_type", "as_of", "url"):
        assert field in first
    assert any(ref["source_type"] == "peer_review" for ref in refs)


def test_json_serializable():
    deps, *_ = make_deps([_result()], [4])
    out = run_market_evaluation(make_state(), deps)
    json.dumps(out)  # 예외 없이 직렬화되어야 한다


def test_technologies_evaluated_separately():
    deps, *_ = make_deps([_result()], [4])
    out = run_market_evaluation(make_state(), deps)
    assert set(out["market_result"].keys()) == {"deepseek_v2_mla", "itme"}
    assert out["market_result"]["deepseek_v2_mla"]["technology"] == "DeepSeek-V2 MLA"
    assert out["market_result"]["itme"]["technology"] == "ITME"


def test_consumes_techprofile_schema():
    deps, search, *_ = make_deps([_result()], [2, 2, 4])
    out = run_market_evaluation(make_state(), deps)

    sw = out["market_result"]["deepseek_v2_mla"]
    assert sw["tech_id"] == "deepseek_v2_mla"
    assert sw["camp"] == "SW"
    assert out["market_result"]["itme"]["tech_id"] == "itme"
    assert out["market_result"]["itme"]["camp"] == "HW"

    joined = " ".join(call["query"] for call in search.calls)
    # TechProfile의 overview/measurements가 쿼리 시드로 사용되어야 한다.
    assert "latent vector" in joined
    assert "93.3%" in joined
    assert "CPU-offload" in joined


def test_structured_evidence_mapped_from_results():
    item_evidence = [
        EvidenceItem(
            result_index=1, value="93.3%", unit="%", baseline="MHA", note="long context"
        )
    ]
    search = FakeSearch(
        [_result(url="https://arxiv.org/abs/2405.1", title="Paper", date="2024-05")]
    )
    scorer = FakeScorer(score=4, evidence=item_evidence)
    deps = MarketDeps(search, PerKeyJudge([4]), scorer)
    out = run_market_evaluation(make_state(), deps)

    evidence = out["market_result"]["deepseek_v2_mla"]["items"]["3-2-a"]["evidence"]
    assert evidence and evidence[0]["value"] == "93.3%"
    assert evidence[0]["unit"] == "%"
    assert evidence[0]["baseline"] == "MHA"
    assert evidence[0]["url"] == "https://arxiv.org/abs/2405.1"
    assert evidence[0]["as_of"] == "2024-05"

    ref = out["references"][0]
    assert ref["url"] == "https://arxiv.org/abs/2405.1"
    assert ref["source_type"] == "peer_review"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/abs/2405.1", "peer_review"),
        ("https://dl.acm.org/doi/10.1145/x", "peer_review"),
        ("https://github.com/deepseek-ai/DeepSeek-V2", "official"),
        ("https://huggingface.co/deepseek-ai/DeepSeek-V2", "official"),
        ("https://docs.nvidia.com/x", "vendor"),
        ("https://www.samsung.com/semiconductor/x", "vendor"),
        ("https://www.reuters.com/technology/x", "news"),
        ("https://medium.com/@x/y", "community"),
        ("https://example.com/x", "unknown"),
    ],
)
def test_source_type_classification(url, expected):
    assert _source_type(url) == expected


def test_fallback_to_selected_technologies():
    state = EvaluationState(
        selected_technologies={"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
    )
    deps, *_ = make_deps([_result()], [4])
    out = run_market_evaluation(state, deps)
    assert set(out["market_result"].keys()) == {"sw", "hw"}
    assert out["market_result"]["sw"]["tech_id"] == "sw"
    assert out["market_result"]["sw"]["camp"] == ""


def test_criteria_filter_limits_items():
    only_a = [c for c in load_rubric()["criteria"] if c["id"] == "3-2-a"]
    deps, *_ = make_deps([_result()], [4], rubric_score=4)
    out = run_market_evaluation(make_state(), deps, only_a)
    assert list(out["market_result"]["deepseek_v2_mla"]["items"].keys()) == ["3-2-a"]
    # 1개 항목 x 5점 기준 -> 4 / 5 * 100
    assert out["market_result"]["deepseek_v2_mla"]["score"] == pytest.approx(80.0)


def test_partial_search_failure_is_tolerated():
    class FlakySearch:
        def __init__(self):
            self.calls = 0

        def __call__(self, query, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("network")
            return [_result()]

    scorer = FakeScorer(score=4)
    deps = MarketDeps(FlakySearch(), PerKeyJudge([4]), scorer)
    out = run_market_evaluation(make_state(), deps)
    assert out["market_result"]["deepseek_v2_mla"]["items"]["3-2-a"]["confidence_tag"] == TAG_MEDIUM
