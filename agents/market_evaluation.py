"""시장 평가 Agent.

`data/3-2_market_evaluation.json` 루브릭을 단일 소스로 사용한다. 기술(SW/HW) 1건마다
항목(3-2-a ~ 3-2-d)을 순회하며 "쿼리 생성 → 웹검색 → 근거 판정 → (약함이면 재검색) →
루브릭 채점"을 수행한다. 바깥 그래프에는 단일 노드로 보이며, 항목별 상태 전이는 내부
LangGraph 서브그래프가 담당한다.

노드는 자신이 생성한 State Key(`market_result`, `references`)만 반환한다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlparse

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from agents.state import EvaluationState

ROOT = Path(__file__).resolve().parent.parent
RUBRIC_PATH = ROOT / "data" / "3-2_market_evaluation.json"
PROMPT_PATH = ROOT / "prompts" / "market_evaluation.md"

MAX_ATTEMPTS = 3
CONFIRM_THRESHOLD = 3

TAG_STRONG = "강함"
TAG_MEDIUM = "보통"
TAG_WEAK = "약함"
TAG_NOT_VERIFIED = "NOT_VERIFIED"


# --------------------------------------------------------------------------- #
# Structured output schemas
# --------------------------------------------------------------------------- #
class EvidenceJudgement(BaseModel):
    """검색 결과의 근거 품질 판정(검색 종료 조건)."""

    evidence_score: int = Field(ge=0, le=5, description="근거 품질 0~5. 출처 없음은 0.")
    reason: str = Field(description="판정 이유")


class EvidenceItem(BaseModel):
    """규칙 7: 수치에 출처·기준 시점·단위·baseline을 붙인 근거."""

    result_index: int = Field(ge=1, description="수치가 나온 검색 결과 번호(1부터)")
    value: str = Field(description="수치 (예: '93.3%')")
    unit: str = Field(default="", description="단위 (예: %, x, GB, ms, $/token)")
    baseline: str = Field(default="", description="비교 기준선 (예: MHA, CPU-offload)")
    note: str = Field(default="", description="보조 설명(조건 등)")


class RubricScore(BaseModel):
    """확정 근거에 대한 루브릭 채점."""

    score: int = Field(ge=1, le=5)
    rationale: str = Field(description="판단 근거 요약")
    evidence: list[EvidenceItem] = Field(
        default_factory=list, description="판단을 뒷받침하는 정량 근거 목록"
    )


# --------------------------------------------------------------------------- #
# Injectable dependencies
# --------------------------------------------------------------------------- #
@dataclass
class MarketDeps:
    web_search: Callable[..., list[dict]]
    judge_evidence: Callable[..., EvidenceJudgement]
    score_rubric: Callable[..., RubricScore]


# --------------------------------------------------------------------------- #
# Inner item state (state transitions inside the single node)
# --------------------------------------------------------------------------- #
class _ItemStateRequired(TypedDict):
    criterion: dict[str, Any]
    technology: str


class ItemState(_ItemStateRequired, total=False):
    tech_info: dict[str, Any]
    attempt: int
    queries: list[str]
    results: list[dict]
    evidence_score: int
    judgement_reason: str
    score: int
    rationale: str
    confidence_tag: str
    sources: list[str]
    evidence: list[dict]


# --------------------------------------------------------------------------- #
# Config / resources
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def load_rubric(path: str = str(RUBRIC_PATH)) -> dict:
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)


@lru_cache(maxsize=1)
def load_system_prompt(path: str = str(PROMPT_PATH)) -> str:
    return Path(path).read_text(encoding="utf-8")


def _eval_as_of() -> str:
    return os.getenv("EVAL_AS_OF") or date.today().isoformat()


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None else float(value)


def map_tag(evidence_score: int) -> str:
    """evidence_score(0~5)를 4단계 신뢰도 태그로 환산한다."""
    if evidence_score >= 5:
        return TAG_STRONG
    if evidence_score >= CONFIRM_THRESHOLD:
        return TAG_MEDIUM
    if evidence_score >= 1:
        return TAG_WEAK
    return TAG_NOT_VERIFIED


# --------------------------------------------------------------------------- #
# Query building
# --------------------------------------------------------------------------- #
def _measurement_terms(tech_info: dict) -> list[str]:
    """TechProfile.measurements에서 metric/value/baseline/condition을 추출한다."""
    terms: list[str] = []
    for measurement in tech_info.get("measurements", []) or []:
        if isinstance(measurement, dict):
            joined = " ".join(
                str(measurement.get(field, ""))
                for field in ("metric", "value", "baseline", "condition")
            ).strip()
            if joined:
                terms.append(joined)
    return terms


def _text_terms(tech_info: dict) -> list[str]:
    """TechProfile의 서술 항목(overview·mechanism·scope·claims·limits)에서 용어를 추출한다."""
    terms: list[str] = []
    overview = tech_info.get("overview")
    if isinstance(overview, str) and overview and overview != "NOT_VERIFIED":
        terms.append(overview)
    for field in ("mechanism", "scope", "claims", "limits_explicit", "limits_implicit"):
        for item in tech_info.get(field, []) or []:
            if isinstance(item, dict) and item.get("text"):
                terms.append(str(item["text"]))
    return terms


def _tech_terms(tech_info: dict, criterion_id: str | None = None) -> list[str]:
    """TechProfile에서 검색 시드 용어를 뽑는다.

    `3-2-b`(비용·성능 효과)는 실험 측정치(`measurements`)를 우선 사용한다. 그 외 항목은
    서술 항목(`overview`·`mechanism`·`scope`·`claims`·`limits`)을 우선한다.
    """
    if criterion_id == "3-2-b":
        measurements = _measurement_terms(tech_info)
        if measurements:
            return measurements + _text_terms(tech_info)
    return _text_terms(tech_info) + _measurement_terms(tech_info)


_TECH_ALIASES: dict[str, list[str]] = {
    "deepseek_v2_mla": ["DeepSeek-V2", "MLA", "Multi-head Latent Attention"],
    "itme": ["ITME", "CXL hybrid memory"],
}

_CRITERION_SEEDS: dict[str, list[str]] = {
    "3-2-a": [
        "data center AI inference market size CAGR",
        "LLM serving market TAM growth",
        "long context inference workload growth",
    ],
    "3-2-b": [
        "LLM inference cost per token",
        "GPU memory TCO reduction",
        "KV cache memory reduction",
    ],
    "3-2-c": [
        "production deployment",
        "hyperscaler adoption",
        "commercial service",
    ],
    "3-2-d": [
        "serving framework support",
        "vLLM SGLang integration",
        "standardization ecosystem",
    ],
}


def _alias_terms(tech_info: dict, technology: str) -> str:
    aliases = _TECH_ALIASES.get(str(tech_info.get("tech_id", "")))
    if not aliases:
        aliases = [technology]
    return " ".join(aliases)


def build_queries(
    criterion: dict, technology: str, tech_info: dict, attempt: int
) -> list[str]:
    """기술 alias·항목별 영문 시장 키워드·TechProfile을 시드로 검색 쿼리를 만든다."""
    criterion_id = criterion.get("id", "")
    alias = _alias_terms(tech_info, technology)
    market_seeds = " ".join(_CRITERION_SEEDS.get(criterion_id, []))
    rubric_terms = " ".join(criterion.get("evidence", []) or [])
    if attempt <= 1:
        return [
            f"{alias} {market_seeds}".strip(),
            f"{alias} {rubric_terms}".strip(),
        ]
    tech_terms = " ".join(_tech_terms(tech_info, criterion_id))
    if attempt == 2:
        return [
            f"{alias} {market_seeds} {tech_terms}".strip(),
            f"{alias} {rubric_terms} market adoption TCO".strip(),
        ]
    return [
        f"{alias} {market_seeds} market analysis report".strip(),
        f"{alias} {rubric_terms} market trend".strip(),
    ]


def _search_settings(criterion: dict, attempt: int) -> dict:
    topic = "news" if criterion.get("id") == "3-2-c" else "general"
    depth = "basic" if attempt <= 1 else "advanced"
    max_results = 5 if attempt <= 1 else 8
    return {"topic": topic, "depth": depth, "max_results": max_results}


def _dedupe_results(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for result in results:
        key = str(result.get("url") or result.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(result)
    return out


# --------------------------------------------------------------------------- #
# Inner subgraph nodes
# --------------------------------------------------------------------------- #
def _query_node() -> Callable[[ItemState], dict]:
    def node(state: ItemState) -> dict:
        attempt = int(state.get("attempt", 0)) + 1
        queries = build_queries(
            state["criterion"], state["technology"], state.get("tech_info", {}), attempt
        )
        return {"attempt": attempt, "queries": queries, "results": []}

    return node


def _search_node(deps: MarketDeps) -> Callable[[ItemState], dict]:
    def node(state: ItemState) -> dict:
        settings = _search_settings(state["criterion"], int(state.get("attempt", 1)))
        results: list[dict] = []
        for query in state.get("queries", []):
            try:
                results.extend(deps.web_search(query, **settings) or [])
            except Exception:  # noqa: BLE001 - 검색 실패는 빈 결과로 처리
                continue
        return {"results": _dedupe_results(results)}

    return node


def _judge_node(system_prompt: str, deps: MarketDeps) -> Callable[[ItemState], dict]:
    def node(state: ItemState) -> dict:
        results = state.get("results", [])
        if not results:
            return {"evidence_score": 0, "judgement_reason": "검색 결과 없음"}
        judgement = deps.judge_evidence(
            system_prompt, state["criterion"], state["technology"], results
        )
        return {
            "evidence_score": int(judgement.evidence_score),
            "judgement_reason": judgement.reason,
        }

    return node


def route_after_judge(state: ItemState) -> str:
    if int(state.get("evidence_score", 0)) >= CONFIRM_THRESHOLD:
        return "score"
    if int(state.get("attempt", 1)) >= MAX_ATTEMPTS:
        return "score"
    return "retry"


def _score_node(system_prompt: str, deps: MarketDeps) -> Callable[[ItemState], dict]:
    def node(state: ItemState) -> dict:
        evidence_score = int(state.get("evidence_score", 0))
        results = state.get("results", [])
        if evidence_score <= 0 or not results:
            return {
                "score": 1,
                "rationale": state.get("judgement_reason", "근거를 확인하지 못함"),
                "confidence_tag": TAG_NOT_VERIFIED,
                "sources": [],
                "evidence": [],
            }
        scored = deps.score_rubric(
            system_prompt,
            state["criterion"],
            state["technology"],
            results,
            evidence_score,
        )
        evidence = (
            _evidence_from_items(scored.evidence, results)
            if scored.evidence
            else _evidence_from_results(results)
        )
        return {
            "score": int(scored.score),
            "rationale": scored.rationale,
            "confidence_tag": map_tag(evidence_score),
            "sources": _sources_from_results(results),
            "evidence": evidence,
        }

    return node


def build_item_graph(deps: MarketDeps, system_prompt: str):
    """항목 1건의 상태 전이를 담당하는 내부 서브그래프."""
    graph = StateGraph(ItemState)
    graph.add_node("query", _query_node())
    graph.add_node("search", _search_node(deps))
    graph.add_node("judge", _judge_node(system_prompt, deps))
    graph.add_node("score", _score_node(system_prompt, deps))

    graph.add_edge(START, "query")
    graph.add_edge("query", "search")
    graph.add_edge("search", "judge")
    graph.add_conditional_edges(
        "judge", route_after_judge, {"retry": "query", "score": "score"}
    )
    graph.add_edge("score", END)
    return graph.compile()


# --------------------------------------------------------------------------- #
# Assembly helpers
# --------------------------------------------------------------------------- #
def _format_results(results: list[dict]) -> str:
    blocks = []
    for idx, result in enumerate(results, 1):
        blocks.append(
            "[{i}] {title} ({date})\n{url}\n{content}".format(
                i=idx,
                title=result.get("title", ""),
                date=result.get("published_date") or "기준 시점 미상",
                url=result.get("url", ""),
                content=result.get("content", ""),
            )
        )
    return "\n\n".join(blocks)


def _sources_from_results(results: list[dict]) -> list[str]:
    return [url for result in results if (url := result.get("url"))]


def _evidence_from_results(results: list[dict]) -> list[dict]:
    return [
        {
            "source": result.get("title", ""),
            "url": result.get("url", ""),
            "as_of": result.get("published_date") or "",
            "unit": "",
            "baseline": "",
            "value": "",
        }
        for result in results
    ]


def _evidence_from_items(scored_evidence: list, results: list[dict]) -> list[dict]:
    """채점 LLM이 구조화한 수치 근거(EvidenceItem)를 검색 결과와 매핑한다."""
    evidence: list[dict] = []
    for entry in scored_evidence:
        index = int(entry.result_index) - 1
        result = results[index] if 0 <= index < len(results) else {}
        evidence.append(
            {
                "source": result.get("title", ""),
                "url": result.get("url", ""),
                "as_of": result.get("published_date") or "",
                "unit": entry.unit,
                "baseline": entry.baseline,
                "value": entry.value,
            }
        )
    return evidence


_PEER_REVIEW_DOMAINS = (
    "arxiv.org",
    "acm.org",
    "ieee.org",
    "usenix.org",
    "openreview.net",
    "springer.com",
    "sciencedirect.com",
    "nature.com",
    "science.org",
    "mlr.press",
    "aclweb.org",
    "neurips.cc",
)
_OFFICIAL_DOMAINS = (
    "github.com",
    "huggingface.co",
    "readthedocs.io",
    "openai.com",
    "deepseek.com",
    "paperswithcode.com",
    "microsoft.com",
    "cloud.google.com",
    "aws.amazon.com",
)
_VENDOR_DOMAINS = (
    "nvidia.com",
    "samsung.com",
    "skhynix.com",
    "intel.com",
    "amd.com",
    "micron.com",
    "hpe.com",
    "dell.com",
)
_NEWS_DOMAINS = (
    "reuters.com",
    "techcrunch.com",
    "theverge.com",
    "zdnet.com",
    "datacenterdynamics.com",
    "theregister.com",
    "bloomberg.com",
    "cnbc.com",
    "arstechnica.com",
    "venturebeat.com",
    "tomshardware.com",
    "anandtech.com",
)
_COMMUNITY_DOMAINS = (
    "medium.com",
    "tistory.com",
    "substack.com",
    "youtube.com",
    "youtu.be",
    "reddit.com",
    "news.ycombinator.com",
    "velog.io",
    "brunch.co.kr",
    "blog.naver.com",
)


def _source_type(url: str) -> str:
    host = urlparse(url).netloc.lower() or url.lower()
    if any(domain in host for domain in _PEER_REVIEW_DOMAINS):
        return "peer_review"
    if any(domain in host for domain in _VENDOR_DOMAINS):
        return "vendor"
    if (
        any(domain in host for domain in _OFFICIAL_DOMAINS)
        or host.startswith("docs.")
        or ".docs." in host
    ):
        return "official"
    if any(domain in host for domain in _NEWS_DOMAINS):
        return "news"
    if any(domain in host for domain in _COMMUNITY_DOMAINS) or host.startswith("blog."):
        return "community"
    return "unknown"


def _to_references(key: str, items: dict[str, dict]) -> list[dict]:
    refs: list[dict] = []
    for item_id, item in items.items():
        for evidence in item.get("evidence", []):
            url = evidence.get("url", "")
            refs.append(
                {
                    "id": f"ref-{key}-{item_id}-{len(refs) + 1}",
                    "technology": key,
                    "item": item_id,
                    "source": evidence.get("source", ""),
                    "source_type": _source_type(url),
                    "as_of": evidence.get("as_of", ""),
                    "url": url,
                }
            )
    return refs


def _dedupe_references(references: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for reference in references:
        key = reference.get("url") or reference.get("id", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(reference)
    return out


def _total_score(items: dict[str, dict], criteria: list[dict]) -> float:
    denominator = len(criteria) * 5
    if not denominator:
        return 0.0
    return round(
        sum(item.get("score", 1) for item in items.values()) / denominator * 100, 2
    )


def _overall_rationale(technology: str, items: dict[str, dict]) -> str:
    parts = [
        f"{item_id}: {item.get('rationale', '').strip()}"
        for item_id, item in items.items()
        if item.get("rationale")
    ]
    return f"{technology} 시장성 종합 — " + " / ".join(parts)


def _resolve_technologies(state: EvaluationState) -> list[dict]:
    """`technical_result`(TechProfile)를 소비해 평가 대상 목록을 만든다.

    `technical_result`와 동일하게 `tech_id`("deepseek_v2_mla" | "itme")를 결과 키로
    사용해 하류 Node와 키 체계를 통일한다. 기술 조사 산출물이 없으면
    `selected_technologies`로 폴백한다(이 경우 키는 그대로 사용).
    """
    technical_result = state.get("technical_result", {}) or {}
    selected = state.get("selected_technologies", {}) or {}
    resolved: list[dict] = []
    if isinstance(technical_result, dict) and technical_result:
        for tech_id, profile in technical_result.items():
            profile = profile if isinstance(profile, dict) else {}
            camp = profile.get("camp", "")
            resolved.append(
                {
                    "tech_id": tech_id,
                    "camp": camp,
                    "key": tech_id,
                    "title": profile.get("title")
                    or selected.get(str(camp).lower())
                    or tech_id,
                    "profile": profile,
                }
            )
    else:
        for key, title in selected.items():
            resolved.append(
                {"tech_id": key, "camp": "", "key": key, "title": title, "profile": {}}
            )
    return resolved


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def run_market_evaluation(
    state: EvaluationState,
    deps: MarketDeps,
    criteria: list[dict] | None = None,
) -> dict:
    rubric = load_rubric()
    system_prompt = load_system_prompt()
    criteria = list(criteria if criteria is not None else rubric.get("criteria", []))
    eval_as_of = _eval_as_of()

    graph = build_item_graph(deps, system_prompt)

    market_result: dict[str, dict] = {}
    references: list[dict] = []

    for tech in _resolve_technologies(state):
        key = tech["key"]
        technology = tech["title"]
        tech_info = tech["profile"]
        items: dict[str, dict] = {}
        for criterion in criteria:
            final = graph.invoke(
                {
                    "criterion": criterion,
                    "technology": technology,
                    "tech_info": tech_info,
                    "attempt": 0,
                }
            )
            items[criterion["id"]] = {
                "item": criterion["id"],
                "score": final.get("score", 1),
                "confidence_tag": final.get("confidence_tag", TAG_NOT_VERIFIED),
                "rationale": final.get("rationale", ""),
                "sources": final.get("sources", []),
                "evidence": final.get("evidence", []),
                "attempts": final.get("attempt", 0),
            }

        market_result[key] = {
            "technology": technology,
            "tech_id": tech["tech_id"],
            "camp": tech["camp"],
            "score": _total_score(items, criteria),
            "rationale": _overall_rationale(technology, items),
            "evidence": [
                ev for item in items.values() for ev in item.get("evidence", [])
            ],
            "items": items,
        }
        references.extend(_to_references(key, items))

    for reference in references:
        if not reference.get("as_of"):
            reference["as_of"] = eval_as_of

    return {
        "market_result": market_result,
        "references": _dedupe_references(references),
    }


def market_evaluation_agent(state: EvaluationState) -> dict:
    """LangGraph 시장 평가 노드."""
    return run_market_evaluation(state, default_deps())


# --------------------------------------------------------------------------- #
# Default (real) dependencies — imported lazily so tests need no network/LLM
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _chat_model():
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        "reasoning_effort": os.getenv("OPENAI_REASONING_EFFORT", "low"),
        "use_responses_api": True,
        "temperature": 0,
        "timeout": _float_env("OPENAI_TIMEOUT_SECONDS", 60.0),
        "max_retries": int(os.getenv("OPENAI_MAX_RETRIES", "0")),
    }
    if os.getenv("OPENAI_API_KEY"):
        kwargs["api_key"] = os.getenv("OPENAI_API_KEY")
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    max_output = os.getenv("OPENAI_MAX_OUTPUT_TOKENS")
    if max_output:
        kwargs["max_tokens"] = int(max_output)
    return ChatOpenAI(**kwargs)


@lru_cache(maxsize=1)
def _tavily_client():
    from tavily import TavilyClient

    return TavilyClient(api_key=os.environ["TAVILY_API_KEY"])


def default_web_search(
    query: str, *, topic: str = "general", depth: str = "basic", max_results: int = 5
) -> list[dict]:
    client = _tavily_client()
    response = client.search(
        query=query,
        topic=topic,
        search_depth=depth,
        max_results=max_results,
        chunks_per_source=int(os.getenv("TAVILY_CHUNKS_PER_SOURCE", "3")),
        include_published_date=_bool_env("TAVILY_INCLUDE_PUBLISHED_DATE", True),
        include_answer=False,
        include_raw_content=False,
        include_usage=True,
        timeout=_float_env("TAVILY_TIMEOUT_SECONDS", 60.0),
    )
    return response.get("results", [])


def default_judge_evidence(
    system_prompt: str, criterion: dict, technology: str, results: list[dict]
) -> EvidenceJudgement:
    from langchain_core.messages import HumanMessage, SystemMessage

    model = _chat_model().with_structured_output(EvidenceJudgement)
    user = (
        f"기술: {technology}\n"
        f"평가 항목: {criterion.get('id')} — {criterion.get('question')}\n"
        f"확인 근거: {', '.join(criterion.get('evidence', []))}\n"
        f"주의: {' '.join(criterion.get('cautions', []))}\n\n"
        f"검색 결과:\n{_format_results(results)}\n\n"
        "위 결과의 근거 품질을 0~5로 판정해 evidence_score와 reason을 반환하라."
    )
    return model.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user)]
    )


def default_score_rubric(
    system_prompt: str,
    criterion: dict,
    technology: str,
    results: list[dict],
    evidence_score: int,
) -> RubricScore:
    from langchain_core.messages import HumanMessage, SystemMessage

    score_table = "\n".join(
        f"{level}: {desc}" for level, desc in criterion.get("scores", {}).items()
    )
    model = _chat_model().with_structured_output(RubricScore)
    user = (
        f"기술: {technology}\n"
        f"평가 항목: {criterion.get('id')} — {criterion.get('question')}\n"
        f"채점 기준:\n{score_table}\n"
        f"주의: {' '.join(criterion.get('cautions', []))}\n"
        f"근거 신뢰도 판정 점수: {evidence_score}\n\n"
        f"확정된 검색 결과:\n{_format_results(results)}\n\n"
        "위 근거에 따라 score(1~5)와 rationale을 반환하라. "
        "또한 판단을 뒷받침하는 정량 수치를 evidence에 구조화하라. 각 항목은 "
        "result_index(수치가 나온 검색 결과 번호), value(수치), unit(단위), "
        "baseline(비교 기준선), note(조건 설명)를 포함한다. "
        "수치가 없으면 evidence는 빈 목록으로 둔다."
    )
    return model.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user)]
    )


def default_deps() -> MarketDeps:
    return MarketDeps(
        web_search=default_web_search,
        judge_evidence=default_judge_evidence,
        score_rubric=default_score_rubric,
    )
