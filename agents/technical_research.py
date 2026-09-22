# -*- coding: utf-8 -*-
"""기술 조사 Agent와 TRL 평가 Node. (담당: 황영준)

기술 조사는 Fan-out의 출발점이므로, 여기서의 누락·왜곡이 하류 4개 평가 Node에
그대로 전파된다. 따라서 **원문에 쓰여 있는 것만** 넘기고 해석은 하류로 미룬다.

책임 경계
  한다    : 원문에서 사실 추출, 근거(chunk_id·page) 고정, 공개 근거 기반 TRL 추정
  안 한다 : 기술 간 우열 비교(→평가 종합), 시장 현황(→시장), 외부 반응(→이해관계자),
            도메인 적합성(→도메인)

설계 근거: `docs/`(설계 산출물) 및 `prompts/technical_research.md`
"""
from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.state import EvaluationState
from rag.retriever import TechRetriever, format_chunks

MODEL_NAME = "gpt-4.1-mini"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "technical_research.md"
NOT_VERIFIED = "NOT_VERIFIED"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TRL_RUBRIC_PATH = DATA_DIR / "3-1_technology_readiness.json"

# 평가 기준일. 문헌 발표 후 경과 기간을 재는 고정 시점이다.
AS_OF = date(2026, 9, 22)

# ── 기술 레지스트리 ──────────────────────────────────────────
# state["selected_technologies"]는 `dict[str, str]` 계약이라 PDF 경로를 담지 못한다.
# 공용 State를 건드리지 않기 위해 원문 경로·인용 정보는 이 모듈이 보유한다.
TECH_REGISTRY: dict[str, dict[str, str]] = {
    "deepseek_v2_mla": {
        "camp": "SW",
        "name": "DeepSeek-V2 (MLA)",
        "published": "2024-06",
        "pdf": str(DATA_DIR / "DeepSeekV2_2405.04434v5.pdf"),
        "citation": (
            "DeepSeek-AI(2024). DeepSeek-V2: A Strong, Economical, and Efficient "
            "Mixture-of-Experts Language Model. arXiv, 2405.04434."
        ),
        "url": "https://arxiv.org/pdf/2405.04434",
    },
    "itme": {
        "camp": "HW",
        "name": "ITME (CXL-Hybrid Tiered Memory Expansion)",
        "published": "2026-06",
        "pdf": str(DATA_DIR / "ITME_2606.12556v2.pdf"),
        "citation": (
            "Jang, H. et al.(2026). ITME: Inference Tiered Memory Expansion with "
            "Disaggregated CXL-Hybrid Memories. arXiv, 2606.12556."
        ),
        "url": "https://arxiv.org/pdf/2606.12556",
    },
}

# 관점별 고정 질의. 질의 언어는 한국어(팀 확정 정책).
ASPECT_QUERIES: list[str] = [
    "이 기술의 핵심 접근 방식은 무엇인가",
    "어떤 문제를 해결하려고 하는가",
    "어떤 방식으로 동작하는가 구조와 절차",
    "KV 캐시를 구체적으로 어떻게 처리하는가",
    "어떤 하드웨어와 모델에서 평가했는가 실험 환경",
    "적용 전제 조건과 대상 워크로드는 무엇인가",
    "성능 개선 수치와 비교 대상 baseline",
    "메모리 절감량 또는 용량 확장 수치",
    "한계점과 제약 조건",
    "성능이 저하되거나 불리해지는 조건",
    "프로토타입 구현의 성능 격차와 최적화가 필요한 부분",
]


# ── 프롬프트 로딩 ────────────────────────────────────────────
@lru_cache(maxsize=1)
def _trl_rubric() -> dict:
    """TRL 척도는 팀 공용 루브릭(`data/3-1_technology_readiness.json`)을 단일 소스로 쓴다."""
    return json.loads(TRL_RUBRIC_PATH.read_text(encoding="utf-8"))


def _elapsed_months(published: str, as_of: date = AS_OF) -> int:
    """문헌 발표 후 기준일까지 경과 개월. TRL 해석 시 반드시 함께 읽어야 한다."""
    y, m = (int(x) for x in published.split("-")[:2])
    return (as_of.year - y) * 12 + (as_of.month - m)


@lru_cache(maxsize=1)
def _prompt_sections() -> dict[str, str]:
    """`prompts/technical_research.md`의 `## [SECTION]` 블록을 파싱한다."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    parts = re.split(r"^## \[([A-Z_]+)\]\s*$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


# ── 구조화 출력 스키마 ──────────────────────────────────────
class _Source(BaseModel):
    chunk_id: str = Field(description="근거 청크의 id. 제공된 <chunk id=...> 값 그대로")
    page: int = Field(description="근거 청크의 page 값")


class _Evidenced(BaseModel):
    text: str
    source: _Source


class _Claim(BaseModel):
    text: str = Field(description="논문이 주장하는 효과")
    baseline: str = Field(description="무엇 대비인가. 원문에 없으면 항목을 출력하지 말 것")
    source: _Source


class _Measurement(BaseModel):
    metric: str = Field(description="지표명 (예: TTFT speedup, KV cache reduction)")
    value: str = Field(description="측정값 (단위 포함)")
    baseline: str = Field(description="비교 기준. 필수")
    condition: str = Field(description="실험 조건 (하드웨어·모델·워크로드·구간)")
    source: _Source


class _Limit(BaseModel):
    text: str
    basis: str = Field(description="명시적 한계면 원문 근거, 암묵적 한계면 역산 근거")
    source: _Source


class _Extraction(BaseModel):
    overview: str
    mechanism: list[_Evidenced] = Field(default_factory=list)
    scope: list[_Evidenced] = Field(default_factory=list)
    claims: list[_Claim] = Field(default_factory=list)
    measurements: list[_Measurement] = Field(default_factory=list)
    limits_explicit: list[_Limit] = Field(default_factory=list)


class _ImplicitLimits(BaseModel):
    limits_implicit: list[_Limit] = Field(default_factory=list)


class _TrlComponent(BaseModel):
    component: str = Field(description="구성요소 이름 (예: 핵심 디바이스, 시스템 전체)")
    trl: int = Field(description="해당 구성요소의 TRL 1~9")
    evidence: str = Field(description="이 단계로 판정한 근거")


class _TrlComponentCritical(_TrlComponent):
    is_critical: bool = Field(
        description="이 구성요소 없이는 기술이 성립하지 않는가. 핵심 경로면 true"
    )


class _TrlVerdict(BaseModel):
    """구간은 LLM이 단언하지 않고 구성요소 판정에서 계산한다(재현성 확보)."""

    components: list[_TrlComponentCritical] = Field(default_factory=list)
    rationale: str = Field(description="판정 요약. 공개 정보 기반 추정임을 명시할 것")


# ── 내부 유틸 ────────────────────────────────────────────────
def _resolve_techs(selected: Any) -> list[str]:
    """`selected_technologies`(dict[str,str] 계약)를 레지스트리 id로 해석한다.

    허용 형태: {"SW": "DeepSeek-V2 MLA", "HW": "ITME"} / {"itme": "..."} / ["itme", ...]
    해석 실패 시 레지스트리 전체를 사용한다.
    """
    if not selected:
        return list(TECH_REGISTRY)

    tokens = list(selected.values()) + list(selected.keys()) if isinstance(selected, dict) else list(selected)
    hit: list[str] = []
    for tid, meta in TECH_REGISTRY.items():
        keys = {tid, meta["camp"].lower(), meta["name"].lower()}
        for t in tokens:
            t = str(t).lower()
            if t in keys or tid in t or (tid == "itme" and "itme" in t) or (
                tid == "deepseek_v2_mla" and ("mla" in t or "deepseek" in t)
            ):
                hit.append(tid)
                break
    return hit or list(TECH_REGISTRY)


# 목차(dot leader)·참고문헌 목록은 근거 가치가 없으므로 검색 결과에서 제외한다.
# ※ 인덱스 자체는 건드리지 않는다(벤치마크 측정값과 동일한 인덱스를 유지하기 위함).
_DOT_LEADER = re.compile(r"(?:\.\s*){4,}")
_BIB_LINE = re.compile(r"^\s*\[\d+\]\s")


def _is_noise(doc: Document) -> bool:
    lines = [l for l in doc.page_content.split("\n") if l.strip()]
    if not lines:
        return True
    toc = sum(bool(_DOT_LEADER.search(l)) for l in lines)
    bib = sum(bool(_BIB_LINE.match(l)) for l in lines)
    return toc / len(lines) >= 0.3 or bib / len(lines) >= 0.5


def _retrieve(retriever: TechRetriever, per_query_k: int = 4) -> list[Document]:
    """관점별 고정 질의를 모두 던지고 chunk_id 기준으로 중복 제거한다."""
    seen: dict[str, Document] = {}
    for q in ASPECT_QUERIES:
        for d in retriever.search(q, k=per_query_k):
            if _is_noise(d):
                continue
            seen.setdefault(d.metadata["chunk_id"], d)
    return sorted(seen.values(), key=lambda d: (d.metadata["page"], d.metadata["chunk_id"]))


# baseline/condition 을 형식상으로만 채운 값. 이 경우 수치를 인용할 수 없다.
_PLACEHOLDER = {"", "-", "--", "n/a", "na", "none", "없음", "미상", "불명", "not specified", "unknown"}


def _is_placeholder(value: str) -> bool:
    return str(value).strip().lower() in _PLACEHOLDER


def _drop_ungrounded(items: list, valid_ids: set[str]) -> tuple[list[dict], int]:
    """근거 chunk_id가 실제 검색 결과에 없는 항목을 버린다(환각 출처 차단)."""
    kept = [i for i in items if i.source.chunk_id in valid_ids]
    return [i.model_dump() for i in kept], len(items) - len(kept)


def _drop_unusable_measurements(items: list, valid_ids: set[str]) -> tuple[list[dict], int]:
    """측정치는 baseline·condition 이 모두 실질적으로 채워져 있어야 인용 가능하다.

    '무엇 대비'와 '어떤 조건'이 없는 수치는 보고서에서 오인용을 만든다.
    (예: ITME abstract의 1.80×는 NVMe-oF 대비이고, §6.1의 1.81×는 재계산 대비다)
    """
    kept = [
        i
        for i in items
        if i.source.chunk_id in valid_ids
        and not _is_placeholder(i.baseline)
        and not _is_placeholder(i.condition)
    ]
    return [i.model_dump() for i in kept], len(items) - len(kept)


def _evidence_level(counts: dict[str, int]) -> str:
    if sum(counts.values()) == 0:
        return NOT_VERIFIED
    if counts["measurements"] >= 3 and counts["limits_explicit"] >= 1:
        return "strong"
    return "limited"


# ── Agent 1. 기술 조사 ───────────────────────────────────────
def technical_research_agent(state: EvaluationState) -> dict:
    """원문 PDF에서 두 기술의 구조, 성능, 범위와 한계를 추출한다.

    출력 State Key: `technical_result`, `references`
    """
    sec = _prompt_sections()
    model = init_chat_model(MODEL_NAME, model_provider="openai", temperature=0)
    tech_ids = _resolve_techs(state.get("selected_technologies"))

    # 임베딩 모델은 한 번만 로드해 기술 간 공유
    from rag.embeddings import BGEM3Embeddings

    embeddings = BGEM3Embeddings()

    results: dict[str, Any] = {}
    references: list[dict[str, Any]] = []

    for tid in tech_ids:
        meta = TECH_REGISTRY[tid]
        print(f"\n[기술 조사] {meta['name']}")
        retriever = TechRetriever(tid, meta["pdf"], embeddings=embeddings).build()
        docs = _retrieve(retriever)
        ctx = format_chunks(docs)
        valid_ids = {d.metadata["chunk_id"] for d in docs}

        ex: _Extraction = model.with_structured_output(_Extraction).invoke(
            [
                SystemMessage(sec["SYSTEM"]),
                HumanMessage(sec["EXTRACT"].format(tech_name=meta["name"], context=ctx)),
            ]
        )
        im: _ImplicitLimits = model.with_structured_output(_ImplicitLimits).invoke(
            [
                SystemMessage(sec["SYSTEM"]),
                HumanMessage(
                    sec["IMPLICIT_LIMITS"].format(tech_name=meta["name"], context=ctx)
                ),
            ]
        )

        dropped: dict[str, int] = {}
        mechanism, dropped["mechanism"] = _drop_ungrounded(ex.mechanism, valid_ids)
        scope, dropped["scope"] = _drop_ungrounded(ex.scope, valid_ids)
        claims, dropped["claims"] = _drop_ungrounded(ex.claims, valid_ids)
        measurements, dropped["measurements"] = _drop_unusable_measurements(ex.measurements, valid_ids)
        limits_e, dropped["limits_explicit"] = _drop_ungrounded(ex.limits_explicit, valid_ids)
        limits_i, dropped["limits_implicit"] = _drop_ungrounded(im.limits_implicit, valid_ids)

        counts = {
            "mechanism": len(mechanism),
            "scope": len(scope),
            "claims": len(claims),
            "measurements": len(measurements),
            "limits_explicit": len(limits_e),
            "limits_implicit": len(limits_i),
        }

        results[tid] = {
            "tech_id": tid,
            "camp": meta["camp"],
            "title": meta["name"],
            "overview": ex.overview or NOT_VERIFIED,
            "mechanism": mechanism,
            "scope": scope,
            "claims": claims,
            "measurements": measurements,
            "limits_explicit": limits_e,
            "limits_implicit": limits_i,
            "evidence_level": _evidence_level(counts),
            "retrieval": {
                "chunks_used": len(docs),
                "pages": sorted({d.metadata["page"] for d in docs}),
                "counts": counts,
                "dropped_ungrounded": dropped,
            },
        }
        references.append(
            {"tech_id": tid, "kind": "paper", "citation": meta["citation"], "url": meta["url"]}
        )
        print(
            f"  근거 청크 {len(docs)}개 · 추출 {counts} · "
            f"근거불일치 폐기 {sum(dropped.values())}건 · evidence={results[tid]['evidence_level']}"
        )

    return {"technical_result": results, "references": references}


# ── Agent 2. TRL 평가 ────────────────────────────────────────
def trl_evaluation_node(state: EvaluationState) -> dict:
    """공개 근거를 이용해 기술별 TRL을 추정한다.

    출력 State Key: `trl_result`, `references`
    """
    sec = _prompt_sections()
    model = init_chat_model(MODEL_NAME, model_provider="openai", temperature=0)
    technical = state.get("technical_result") or {}

    if not technical:
        return {
            "trl_result": {"error": NOT_VERIFIED, "note": "technical_result가 비어 있습니다."},
            "references": [],
        }

    results: dict[str, Any] = {}
    for tid, profile in technical.items():
        name = profile.get("title", tid)
        print(f"\n[TRL 평가] {name}")

        # 판정 입력은 조사 결과의 사실 항목만. 주장(claims)은 단계 판정 근거로 쓰지 않는다.
        digest = {
            "overview": profile.get("overview"),
            "scope": [s.get("text") for s in profile.get("scope", [])],
            "measurements": [
                f"{m.get('metric')}={m.get('value')} (vs {m.get('baseline')}, {m.get('condition')})"
                for m in profile.get("measurements", [])
            ],
            "limits_explicit": [l.get("text") for l in profile.get("limits_explicit", [])],
            "limits_implicit": [l.get("text") for l in profile.get("limits_implicit", [])],
        }

        verdict: _TrlVerdict = model.with_structured_output(_TrlVerdict).invoke(
            [
                SystemMessage(sec["TRL_SYSTEM"]),
                HumanMessage(
                    sec["TRL_USER"].format(
                        tech_name=name,
                        profile="\n".join(f"- {k}: {v}" for k, v in digest.items()),
                        rubric=json.dumps(_trl_rubric(), ensure_ascii=False, indent=2),
                    )
                ),
            ]
        )

        # 시스템 전체의 성숙도는 '가장 덜 성숙한 핵심 구성요소'에 묶인다.
        #   하한 = 핵심 구성요소 중 최저 단계 (이 단계를 넘었다고 보장할 수 없다)
        #   상한 = 전체 구성요소 중 최고 단계 (시연된 최고 수준)
        comps = [c for c in verdict.components if 1 <= c.trl <= 9]
        if comps:
            critical = [c.trl for c in comps if c.is_critical] or [c.trl for c in comps]
            lo, hi = min(critical), max(c.trl for c in comps)
            if lo > hi:
                lo, hi = hi, lo
        else:
            lo = hi = 0  # 판정 불가

        meta = TECH_REGISTRY.get(tid, {})
        published = meta.get("published", NOT_VERIFIED)
        elapsed = _elapsed_months(published) if published != NOT_VERIFIED else None

        results[tid] = {
            "tech_id": tid,
            "trl_range": [lo, hi] if comps else NOT_VERIFIED,
            "evidence_scope": "paper_only",
            "published": published,
            "as_of": AS_OF.isoformat(),
            "elapsed_months": elapsed,
            "interpretation_caveat": (
                "논문 근거만으로 판정한 값이다. 서로 다른 시점에 발표된 문헌의 TRL을 나란히 읽을 때는 "
                "경과 기간(elapsed_months)을 함께 보아야 한다. 값이 같다는 것은 두 기술의 성숙도가 "
                "비슷하다는 뜻이 아니라, 논문이라는 매체가 보여줄 수 있는 성숙도의 상한이 같다는 뜻이다. "
                "실제 채택 근거는 시장성 관점(market_result)에서 별도로 다룬다."
            ),
            "rubric_source": TRL_RUBRIC_PATH.name,
            "range_derivation": (
                "하한=핵심 구성요소 최저 단계, 상한=전체 구성요소 최고 단계. "
                "LLM이 구간을 단언하지 않고 구성요소 판정에서 계산한다."
            ),
            "components": [c.model_dump() for c in verdict.components],
            "rationale": verdict.rationale,
            "estimation_basis": "공개 정보 기반 추정",
            "caveat": (
                "TRL 4~6 구간은 수율·공정 파라미터·실제 성능 수치가 영업 비밀에 해당하여 "
                "공개 정보와 실제 수준의 차이가 가장 크게 발생한다. "
                "또한 KV cache 기술은 논문 발표와 실제 채택 사이에 시차가 있다."
            ),
            "source_evidence_level": profile.get("evidence_level", NOT_VERIFIED),
        }
        print(f"  TRL [{lo}, {hi}] · 구성요소 {len(verdict.components)}건 "
              f"(핵심 {sum(c.is_critical for c in verdict.components)}건) · "
              f"발표 {published} · 경과 {elapsed}개월")

    return {"trl_result": results, "references": []}
