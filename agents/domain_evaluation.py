"""데이터센터 환경의 KV Cache 기술 도메인 평가 Agent."""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, model_validator

from agents.state import EvaluationState
from rag.embeddings import BGEM3Embeddings
from rag.retriever import TechRetriever, format_chunks


MODEL_NAME = "gpt-4.1-mini"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

PROMPT_PATH = PROJECT_ROOT / "prompts" / "domain_evaluation.md"
RUBRIC_PATH = DATA_DIR / "3-4_domain_evaluation.json"

load_dotenv(
    PROJECT_ROOT / ".env",
    override=False,
)


TECH_PDF_PATHS = {
    "deepseek_v2_mla": DATA_DIR / "DeepSeekV2_2405.04434v5.pdf",
    "itme": DATA_DIR / "ITME_2606.12556v2.pdf",
}


CRITERION_FIELD_BY_ID = {
    "3-4-a": "memory_efficiency",
    "3-4-b": "inference_performance",
    "3-4-c": "scalability",
    "3-4-d": "infrastructure_applicability",
    "3-4-e": "operational_cost_efficiency",
    "3-4-f": "evidence_maturity",
    "3-4-g": "independent_validation",
    "3-4-h": "workload_representativeness",
    "3-4-i": "recency_openness",
}

CRITERION_ID_BY_FIELD = {
    field_name: criterion_id
    for criterion_id, field_name
    in CRITERION_FIELD_BY_ID.items()
}


# ---------------------------------------------------------------------
# Structured Output
# ---------------------------------------------------------------------


class Evidence(BaseModel):
    source: str = Field(
        description="실제 검색된 chunk_id",
    )

    page: Optional[int] = None

    quote: str = Field(
        description="해당 chunk의 원문 근거",
    )


class CriterionResult(BaseModel):
    status: Literal[
        "EVALUATED",
        "NOT_VERIFIED",
    ]

    score: Optional[int] = Field(
        default=0,
        ge=0,
        le=5,
    )

    rationale: str

    evidence: list[Evidence] = Field(
        default_factory=list,
    )

    @model_validator(mode="after")
    def validate_status_and_score(self):
        """status와 score의 기본 일관성을 맞춘다."""

        # 팀 합의:
        # NOT_VERIFIED는 총점 계산 시 0점으로 사용한다.
        if self.status == "NOT_VERIFIED":
            self.score = 0
            self.evidence = []
            return self

        if (
            self.score is None
            or self.score <= 0
        ):
            raise ValueError(
                "EVALUATED 항목은 "
                "1~5점이어야 합니다."
            )

        return self


class TechnologyDomainEvaluation(BaseModel):
    tech_id: str
    technology: str
    camp: str

    memory_efficiency: CriterionResult
    inference_performance: CriterionResult
    scalability: CriterionResult
    infrastructure_applicability: CriterionResult
    operational_cost_efficiency: CriterionResult

    evidence_maturity: CriterionResult
    independent_validation: CriterionResult
    workload_representativeness: CriterionResult
    recency_openness: CriterionResult

    weighted_score: float = 0.0
    coverage: float = 0.0
    verdict: str = ""


class _TechnologyDomainAssessment(BaseModel):
    """LLM이 생성하는 criterion별 평가."""

    technology: str

    memory_efficiency: CriterionResult
    inference_performance: CriterionResult
    scalability: CriterionResult
    infrastructure_applicability: CriterionResult
    operational_cost_efficiency: CriterionResult

    evidence_maturity: CriterionResult
    independent_validation: CriterionResult
    workload_representativeness: CriterionResult
    recency_openness: CriterionResult


# ---------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------


def load_domain_rubric() -> dict[str, Any]:
    if not RUBRIC_PATH.exists():
        raise FileNotFoundError(
            f"Domain rubric not found: {RUBRIC_PATH}"
        )

    with RUBRIC_PATH.open(
        "r",
        encoding="utf-8",
    ) as file:
        rubric = json.load(file)

    if not isinstance(rubric, dict):
        raise ValueError(
            "Domain rubric must be an object."
        )

    if "criteria" not in rubric:
        raise ValueError(
            "Domain rubric has no criteria."
        )

    if "scoring" not in rubric:
        raise ValueError(
            "Domain rubric has no scoring."
        )

    return rubric


def get_domain_weights(
    rubric: Optional[dict[str, Any]] = None,
) -> dict[str, float]:
    if rubric is None:
        rubric = load_domain_rubric()

    weights: dict[str, float] = {}

    for criterion in rubric["criteria"]:
        criterion_id = criterion.get(
            "id"
        )

        if criterion_id not in CRITERION_FIELD_BY_ID:
            raise ValueError(
                f"Unknown criterion: {criterion_id}"
            )

        field_name = (
            CRITERION_FIELD_BY_ID[
                criterion_id
            ]
        )

        weights[field_name] = float(
            criterion["weight"]
        )

    total = sum(
        weights.values()
    )

    if abs(total - 1.0) > 1e-9:
        raise ValueError(
            f"Domain weights must sum to 1.0, got {total}"
        )

    return weights


def get_coverage_threshold(
    rubric: Optional[dict[str, Any]] = None,
) -> float:
    if rubric is None:
        rubric = load_domain_rubric()

    threshold = float(
        rubric["scoring"][
            "coverage_threshold"
        ]
    )

    if not 0 <= threshold <= 1:
        raise ValueError(
            "coverage threshold must be 0~1"
        )

    return threshold


def get_default_target_domain(
    rubric: Optional[dict[str, Any]] = None,
) -> str:
    if rubric is None:
        rubric = load_domain_rubric()

    domain = rubric.get(
        "target_domain"
    )

    if not domain:
        raise ValueError(
            "target_domain missing"
        )

    return str(domain)


def load_domain_prompt() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Prompt not found: {PROMPT_PATH}"
        )

    return PROMPT_PATH.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------
# Retrieval Noise Filter
# ---------------------------------------------------------------------


_DOT_LEADER = re.compile(
    r"(?:\.\s*){4,}"
)

_BIB_LINE = re.compile(
    r"^\s*\[\d+\]\s"
)

_REFERENCE_HEADING = re.compile(
    r"^\s*(references|bibliography)\s*$",
    flags=re.IGNORECASE,
)

_CITATION_START = re.compile(
    r"^\s*(?:[A-Z]\.\s+[A-Z][A-Za-z-]+,\s*){2,}"
)

_REFERENCE_TERMS = re.compile(
    r"\b("
    r"arxiv preprint|"
    r"proceedings of|"
    r"transactions on|"
    r"conference on|"
    r"association for computational linguistics"
    r")\b",
    flags=re.IGNORECASE,
)


def _is_noise_document(
    doc: Document,
) -> bool:
    lines = [
        line.strip()
        for line in doc.page_content.split("\n")
        if line.strip()
    ]

    if not lines:
        return True

    full_text = " ".join(
        lines
    )

    lower_text = (
        full_text.casefold()
    )

    non_evidence_phrases = (
        "contributions and acknowledgments",
        "contributions and acknowledgements",
        "author contributions",
    )

    if any(
        phrase in lower_text
        for phrase in non_evidence_phrases
    ):
        return True

    if any(
        _REFERENCE_HEADING.match(
            line
        )
        for line in lines[:3]
    ):
        return True

    if _CITATION_START.match(
        lines[0]
    ):
        return True

    count = len(
        lines
    )

    toc_ratio = (
        sum(
            bool(
                _DOT_LEADER.search(
                    line
                )
            )
            for line in lines
        )
        / count
    )

    bib_ratio = (
        sum(
            bool(
                _BIB_LINE.match(
                    line
                )
            )
            for line in lines
        )
        / count
    )

    citation_ratio = (
        sum(
            bool(
                _CITATION_START.match(
                    line
                )
            )
            for line in lines
        )
        / count
    )

    reference_hits = len(
        _REFERENCE_TERMS.findall(
            full_text
        )
    )

    return (
        toc_ratio >= 0.3
        or bib_ratio >= 0.5
        or citation_ratio >= 0.3
        or reference_hits >= 2
    )


# ---------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------


def build_domain_queries(
    rubric: dict[str, Any],
) -> list[str]:
    queries: list[str] = []

    for criterion in rubric[
        "criteria"
    ]:
        question = criterion.get(
            "question",
            "",
        )

        evidence_terms = (
            criterion.get(
                "evidence",
                [],
            )
        )

        evidence_text = ", ".join(
            str(item)
            for item in evidence_terms
        )

        queries.append(
            f"{question} "
            "관련 실험 결과, 수치, 제약 조건을 찾아라. "
            f"확인 항목: {evidence_text}"
        )

    return queries


def retrieve_domain_evidence(
    retriever: TechRetriever,
    rubric: dict[str, Any],
    per_query_k: int = 3,
) -> list[Document]:
    seen: dict[
        str,
        Document,
    ] = {}

    for query in build_domain_queries(
        rubric
    ):
        docs = retriever.search(
            query,
            k=per_query_k,
        )

        for doc in docs:
            if _is_noise_document(
                doc
            ):
                continue

            chunk_id = (
                doc.metadata.get(
                    "chunk_id"
                )
            )

            if not chunk_id:
                continue

            seen.setdefault(
                str(chunk_id),
                doc,
            )

    return sorted(
        seen.values(),
        key=lambda doc: (
            int(
                doc.metadata.get(
                    "page",
                    0,
                )
            ),
            str(
                doc.metadata.get(
                    "chunk_id",
                    "",
                )
            ),
        ),
    )


# ---------------------------------------------------------------------
# Technical Result
# ---------------------------------------------------------------------


def build_technical_digest(
    profile: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tech_id": profile.get(
            "tech_id"
        ),
        "camp": profile.get(
            "camp"
        ),
        "title": profile.get(
            "title"
        ),
        "overview": profile.get(
            "overview"
        ),
        "mechanism": profile.get(
            "mechanism",
            [],
        ),
        "scope": profile.get(
            "scope",
            [],
        ),
        "claims": profile.get(
            "claims",
            [],
        ),
        "measurements": profile.get(
            "measurements",
            [],
        ),
        "limits_explicit": profile.get(
            "limits_explicit",
            [],
        ),
        "limits_implicit": profile.get(
            "limits_implicit",
            [],
        ),
        "evidence_level": profile.get(
            "evidence_level",
            "NOT_VERIFIED",
        ),
    }


# ---------------------------------------------------------------------
# Quote Validation
# ---------------------------------------------------------------------


def _normalize_text(
    text: str,
) -> str:
    text = unicodedata.normalize(
        "NFKC",
        str(text),
    )

    text = text.replace(
        "\u00ad",
        "",
    )

    # PDF line break:
    # "infer-\nence" -> "inference"
    text = re.sub(
        r"(?<=\w)-\s+(?=\w)",
        "",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return (
        text.strip().casefold()
    )


def _find_fuzzy_quote(
    quote: str,
    document: Document,
) -> Optional[str]:
    """PDF 추출상의 사소한 차이만 허용한다."""

    raw_text = (
        document.page_content
    )

    normalized_quote = (
        _normalize_text(
            quote
        )
    )

    if (
        len(normalized_quote)
        < 30
    ):
        return None

    token_matches = list(
        re.finditer(
            r"\S+",
            raw_text,
        )
    )

    quote_token_count = len(
        re.findall(
            r"\S+",
            quote,
        )
    )

    if (
        not token_matches
        or quote_token_count < 5
    ):
        return None

    candidate_sizes = range(
        max(
            5,
            quote_token_count - 3,
        ),
        quote_token_count + 4,
    )

    best_ratio = 0.0
    best_text: Optional[
        str
    ] = None

    for size in candidate_sizes:
        if (
            size
            > len(token_matches)
        ):
            continue

        for start in range(
            0,
            len(token_matches)
            - size
            + 1,
        ):
            end = start + size

            char_start = (
                token_matches[
                    start
                ].start()
            )

            char_end = (
                token_matches[
                    end - 1
                ].end()
            )

            candidate = (
                raw_text[
                    char_start:
                    char_end
                ]
            )

            ratio = (
                SequenceMatcher(
                    None,
                    normalized_quote,
                    _normalize_text(
                        candidate
                    ),
                ).ratio()
            )

            if (
                ratio
                > best_ratio
            ):
                best_ratio = ratio
                best_text = (
                    candidate
                )

    if (
        len(normalized_quote)
        < 80
    ):
        threshold = 0.97

    elif (
        len(normalized_quote)
        < 160
    ):
        threshold = 0.95

    else:
        threshold = 0.94

    if (
        best_text is not None
        and best_ratio >= threshold
    ):
        return best_text

    return None


def _resolve_grounded_quote(
    quote: str,
    document: Document,
) -> Optional[str]:
    if not quote.strip():
        return None

    if (
        quote
        in document.page_content
    ):
        return quote

    return _find_fuzzy_quote(
        quote,
        document,
    )


# ---------------------------------------------------------------------
# LLM Result Validation
# ---------------------------------------------------------------------


def _rubric_by_field(
    rubric: dict[str, Any],
) -> dict[
    str,
    dict[str, Any],
]:
    result: dict[
        str,
        dict[str, Any],
    ] = {}

    for criterion in rubric[
        "criteria"
    ]:
        field_name = (
            CRITERION_FIELD_BY_ID[
                criterion["id"]
            ]
        )

        result[
            field_name
        ] = criterion

    return result


def _set_not_verified(
    result: CriterionResult,
    reason: str,
) -> None:
    result.status = (
        "NOT_VERIFIED"
    )

    result.score = 0

    result.rationale = (
        reason
    )

    result.evidence = []


def _detect_scalability_axes(
    evidence_text: str,
) -> tuple[
    bool,
    bool,
    bool,
]:
    """Scalability의 세 정량 축을 탐지한다.

    반환 순서:
    context, batch, concurrency
    """

    context_axis = bool(
        re.search(
            r"\bcontext length\b|"
            r"\bcontext window\b|"
            r"\bsequence length\b|"
            r"\binput sequence\b|"
            r"\b\d+\s*k\b",
            evidence_text,
        )
    )

    batch_axis = bool(
        re.search(
            r"\bbatch size\b|"
            r"\bbatching\b|"
            r"\bbatch\s+\d+\b",
            evidence_text,
        )
    )

    concurrency_axis = bool(
        re.search(
            r"\bconcurrent\b|"
            r"\bconcurrency\b|"
            r"\bsimultaneous users?\b|"
            r"\bparallel requests?\b|"
            r"\bconcurrent requests?\b",
            evidence_text,
        )
    )

    return (
        context_axis,
        batch_axis,
        concurrency_axis,
    )


def _apply_scalability_guardrail(
    result: CriterionResult,
) -> None:
    """3-4-c의 3/4/5점을 정량 축 개수와 일치시킨다."""

    if (
        result.status
        != "EVALUATED"
    ):
        return

    # 1점과 2점은 각각
    # 명시적 한계 / 정성적 방향을 나타내므로
    # 이 guardrail의 대상이 아니다.
    if (
        result.score is None
        or result.score < 3
    ):
        return

    evidence_text = (
        _normalize_text(
            " ".join(
                evidence.quote
                for evidence
                in result.evidence
            )
        )
    )

    (
        context_axis,
        batch_axis,
        concurrency_axis,
    ) = _detect_scalability_axes(
        evidence_text
    )

    axis_count = sum(
        (
            context_axis,
            batch_axis,
            concurrency_axis,
        )
    )

    # 3점 이상을 주었는데
    # 실제 정량 축을 하나도 확인할 수 없으면
    # 점수 자체를 검증할 수 없다.
    if axis_count == 0:
        _set_not_verified(
            result,
            "3점 이상의 scalability 점수를 "
            "뒷받침하는 정량 확장 축을 "
            "원문 근거에서 확인하지 못해 "
            "NOT_VERIFIED 처리함",
        )
        return

    expected_score = {
        1: 3,
        2: 4,
        3: 5,
    }[axis_count]

    old_score = result.score

    # 핵심:
    # 단순 max-score 제한이 아니라
    # 정량 축 개수와 점수를 정확히 일치시킨다.
    result.score = (
        expected_score
    )

    if (
        old_score
        != expected_score
    ):
        result.rationale += (
            " / Python rubric 검증 결과 "
            f"정량적으로 확인된 scalability 축이 "
            f"{axis_count}개이므로 "
            f"{old_score}점에서 "
            f"{expected_score}점으로 조정함"
        )


def _apply_evidence_maturity_guardrail(
    result: CriterionResult,
) -> None:
    """3-4-f evidence maturity의 rubric gap을 방어한다.

    5점:
        peer-reviewed + direct measurement

    4점:
        paper simulation/modeling

    2점:
        direct measurement 없는 vendor spec/whitepaper

    1점:
        secondary/blog/inference

    직접 실험은 확인되지만 peer-review 여부가
    확인되지 않는 자료는 현재 rubric의
    1/2/4/5 어디에도 정확히 대응하지 않으므로
    임의 점수 대신 NOT_VERIFIED로 처리한다.
    """

    if (
        result.status
        != "EVALUATED"
    ):
        return

    evidence_text = (
        _normalize_text(
            " ".join(
                evidence.quote
                for evidence
                in result.evidence
            )
        )
    )

    score = result.score

    if score is None:
        return

    peer_review_markers = (
        "peer-reviewed",
        "peer reviewed",
        "accepted at",
        "published in",
        "proceedings of",
        "journal",
    )

    simulation_markers = (
        "simulation",
        "simulated",
        "modeling",
        "modelling",
        "analytical model",
        "modeled",
        "modelled",
    )

    vendor_document_markers = (
        "whitepaper",
        "white paper",
        "specification",
        "spec sheet",
        "datasheet",
        "data sheet",
    )

    direct_measurement_markers = (
        "we evaluate",
        "our evaluation",
        "we validate",
        "our measurements",
        "our measurement",
        "we measure",
        "measured",
        "prototype",
        "hardware prototype",
        "throughput improvement",
        "performance evaluation",
        "evaluation shows",
    )

    peer_review_verified = any(
        marker in evidence_text
        for marker in peer_review_markers
    )

    simulation_verified = any(
        marker in evidence_text
        for marker in simulation_markers
    )

    vendor_document_verified = any(
        marker in evidence_text
        for marker in vendor_document_markers
    )

    direct_measurement_verified = any(
        marker in evidence_text
        for marker in direct_measurement_markers
    )

    # E3
    if score == 5:
        if not (
            peer_review_verified
            and direct_measurement_verified
        ):
            _set_not_verified(
                result,
                "5점 기준인 "
                "'피어리뷰 논문의 직접 측정치'를 "
                "제공된 원문 근거에서 모두 확인할 수 없어 "
                "NOT_VERIFIED 처리함",
            )
        return

    # E2
    if score == 4:
        if not simulation_verified:
            _set_not_verified(
                result,
                "4점 기준인 논문의 "
                "시뮬레이션·모델링 근거를 "
                "제공된 원문에서 확인하지 못해 "
                "NOT_VERIFIED 처리함",
            )
        return

    # E1
    if score == 2:
        # 2점은 직접 측정이 없는
        # vendor spec / whitepaper 수준이어야 한다.
        #
        # ITME처럼 실제 prototype 및 직접 평가가 존재하지만
        # peer-review 여부가 불명확한 경우에는
        # 2점으로 낮춰 넣지 않는다.
        if direct_measurement_verified:
            _set_not_verified(
                result,
                "직접 실험·prototype 근거는 확인되지만 "
                "peer-review 여부는 확인되지 않으며, "
                "이는 현재 rubric의 2점 기준인 "
                "'직접 측정 없는 vendor spec/whitepaper'와 "
                "일치하지 않아 NOT_VERIFIED 처리함",
            )
            return

        if not vendor_document_verified:
            _set_not_verified(
                result,
                "2점 기준인 "
                "vendor spec 또는 whitepaper 수준의 "
                "근거임을 원문에서 확인하지 못해 "
                "NOT_VERIFIED 처리함",
            )
        return

    # E0인 1점은
    # structured LLM 평가와 실제 evidence가
    # 이미 검증된 경우 그대로 유지한다.


def _apply_semantic_guardrails(
    field_name: str,
    result: CriterionResult,
) -> None:
    """criterion별 추가 rubric 검증."""

    if (
        result.status
        != "EVALUATED"
    ):
        return

    if (
        field_name
        == "scalability"
    ):
        _apply_scalability_guardrail(
            result
        )

    elif (
        field_name
        == "evidence_maturity"
    ):
        _apply_evidence_maturity_guardrail(
            result
        )


def validate_llm_assessment(
    assessment: _TechnologyDomainAssessment,
    docs: list[Document],
    rubric: dict[str, Any],
) -> _TechnologyDomainAssessment:
    """LLM 평가를 rubric과 실제 PDF 근거로 검증한다."""

    docs_by_id = {
        str(
            doc.metadata["chunk_id"]
        ): doc
        for doc in docs
        if doc.metadata.get(
            "chunk_id"
        )
    }

    rubric_fields = (
        _rubric_by_field(
            rubric
        )
    )

    for field_name in (
        CRITERION_FIELD_BY_ID.values()
    ):
        result: CriterionResult = getattr(
            assessment,
            field_name,
        )

        # NOT_VERIFIED는 팀 합의대로 0점
        if (
            result.status
            == "NOT_VERIFIED"
        ):
            result.score = 0
            result.evidence = []
            continue

        criterion = (
            rubric_fields[
                field_name
            ]
        )

        allowed_scores = {
            int(score)
            for score
            in criterion.get(
                "scores",
                {},
            ).keys()
        }

        # EVALUATED 상태에서
        # rubric에 존재하지 않는 점수는 인정하지 않는다.
        if (
            result.score
            not in allowed_scores
        ):
            _set_not_verified(
                result,
                "루브릭에 정의되지 않은 "
                "점수가 생성되어 "
                "NOT_VERIFIED 처리함",
            )
            continue

        valid_evidence: list[
            Evidence
        ] = []

        for evidence in (
            result.evidence
        ):
            chunk_id = (
                evidence.source.strip()
            )

            document = (
                docs_by_id.get(
                    chunk_id
                )
            )

            # 실제 검색된 chunk가 아니면 제외
            if document is None:
                continue

            grounded_quote = (
                _resolve_grounded_quote(
                    evidence.quote,
                    document,
                )
            )

            # 실제 원문에서 검증 불가능
            if grounded_quote is None:
                continue

            # fuzzy 복구된 경우에도
            # 최종 JSON에는 실제 PDF 문자열 저장
            evidence.quote = (
                grounded_quote
            )

            page = (
                document.metadata.get(
                    "page"
                )
            )

            if page is not None:
                evidence.page = int(
                    page
                )

            valid_evidence.append(
                evidence
            )

        if not valid_evidence:
            _set_not_verified(
                result,
                "평가 점수를 뒷받침하는 "
                "원문 근거를 검증하지 못함",
            )
            continue

        result.evidence = (
            valid_evidence
        )

        # quote/source 검증이 끝난 뒤
        # rubric 의미 검증 수행
        _apply_semantic_guardrails(
            field_name,
            result,
        )

    return assessment


# ---------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------


def calculate_domain_score(
    evaluation: TechnologyDomainEvaluation,
    rubric: Optional[
        dict[str, Any]
    ] = None,
) -> TechnologyDomainEvaluation:
    """Domain 총점을 계산한다.

    팀 합의:
    - EVALUATED: rubric score
    - NOT_VERIFIED: 0점
    - 모든 criterion의 weight 유지
    - 최종 score 범위: 0~5

    coverage:
    - 실제 EVALUATED된 criterion의 weight 합
    """

    if rubric is None:
        rubric = (
            load_domain_rubric()
        )

    weights = (
        get_domain_weights(
            rubric
        )
    )

    coverage_threshold = (
        get_coverage_threshold(
            rubric
        )
    )

    weighted_sum = 0.0
    covered_weight = 0.0

    for (
        field_name,
        weight,
    ) in weights.items():
        result = getattr(
            evaluation,
            field_name,
        )

        if (
            result.status
            == "NOT_VERIFIED"
        ):
            score = 0

        else:
            score = int(
                result.score or 0
            )

            covered_weight += (
                weight
            )

        weighted_sum += (
            score
            * weight
        )

    evaluation.weighted_score = round(
        weighted_sum,
        2,
    )

    evaluation.coverage = round(
        covered_weight,
        3,
    )

    # score는 계산하되,
    # coverage가 부족하면 verdict는 보류
    if (
        covered_weight
        < coverage_threshold
    ):
        evaluation.verdict = (
            "판정 보류 — 근거 불충분"
        )

        return evaluation

    score = (
        evaluation.weighted_score
    )

    if (
        score >= 4.0
        and covered_weight >= 0.8
    ):
        evaluation.verdict = (
            "적합 — 근거 충분"
        )

    elif (
        score >= 4.0
    ):
        evaluation.verdict = (
            "적합 — 근거 보강 필요"
        )

    elif (
        score >= 3.0
    ):
        evaluation.verdict = (
            "조건부 적합"
        )

    elif (
        score >= 2.0
    ):
        evaluation.verdict = (
            "제한적 적합"
        )

    else:
        evaluation.verdict = (
            "부적합"
        )

    return evaluation


# ---------------------------------------------------------------------
# Domain Result JSON
# ---------------------------------------------------------------------


def build_domain_output(
    evaluation: TechnologyDomainEvaluation,
    target_domain: str,
) -> dict[str, Any]:
    """다른 Agent가 사용하기 쉬운 JSON 구조로 변환한다."""

    criteria: dict[
        str,
        dict[str, Any],
    ] = {}

    flattened_evidence: list[
        dict[str, Any]
    ] = []

    rationale_parts: list[
        str
    ] = []

    for field_name in (
        CRITERION_FIELD_BY_ID.values()
    ):
        criterion_id = (
            CRITERION_ID_BY_FIELD[
                field_name
            ]
        )

        result: CriterionResult = getattr(
            evaluation,
            field_name,
        )

        criteria[
            field_name
        ] = {
            "criterion_id": (
                criterion_id
            ),
            **result.model_dump(),
        }

        rationale_parts.append(
            f"{criterion_id}: "
            f"{result.rationale}"
        )

        for evidence in (
            result.evidence
        ):
            flattened_evidence.append(
                {
                    "criterion_id": (
                        criterion_id
                    ),
                    "criterion": (
                        field_name
                    ),
                    **evidence.model_dump(),
                }
            )

    return {
        "technology": (
            evaluation.technology
        ),
        "tech_id": (
            evaluation.tech_id
        ),
        "camp": (
            evaluation.camp
        ),
        "target_domain": (
            target_domain
        ),
        "score": (
            evaluation.weighted_score
        ),
        "score_scale": "0-5",
        "coverage": (
            evaluation.coverage
        ),
        "verdict": (
            evaluation.verdict
        ),
        "rationale": " / ".join(
            rationale_parts
        ),
        "criteria": (
            criteria
        ),
        "evidence": (
            flattened_evidence
        ),
    }


# ---------------------------------------------------------------------
# References
# ---------------------------------------------------------------------


def build_domain_references(
    tech_id: str,
    docs: list[Document],
    evaluation: TechnologyDomainEvaluation,
) -> list[dict[str, Any]]:
    """실제 최종 평가에 사용된 chunk만 references에 기록한다."""

    used_chunk_ids: set[
        str
    ] = set()

    for field_name in (
        CRITERION_FIELD_BY_ID.values()
    ):
        result: CriterionResult = getattr(
            evaluation,
            field_name,
        )

        for evidence in (
            result.evidence
        ):
            used_chunk_ids.add(
                evidence.source
            )

    used_docs = [
        doc
        for doc in docs
        if str(
            doc.metadata.get(
                "chunk_id",
                "",
            )
        )
        in used_chunk_ids
    ]

    grouped: dict[
        str,
        dict[str, Any],
    ] = {}

    for doc in used_docs:
        source = str(
            doc.metadata.get(
                "source",
                "UNKNOWN",
            )
        )

        group = (
            grouped.setdefault(
                source,
                {
                    "tech_id": (
                        tech_id
                    ),
                    "kind": (
                        "domain_evidence"
                    ),
                    "source": (
                        source
                    ),
                    "pages": set(),
                    "chunk_ids": [],
                },
            )
        )

        page = (
            doc.metadata.get(
                "page"
            )
        )

        if page is not None:
            group[
                "pages"
            ].add(
                int(page)
            )

        chunk_id = (
            doc.metadata.get(
                "chunk_id"
            )
        )

        if chunk_id:
            group[
                "chunk_ids"
            ].append(
                str(chunk_id)
            )

    references: list[
        dict[str, Any]
    ] = []

    for group in (
        grouped.values()
    ):
        references.append(
            {
                "tech_id": (
                    group["tech_id"]
                ),
                "kind": (
                    group["kind"]
                ),
                "source": (
                    group["source"]
                ),
                "pages": sorted(
                    group[
                        "pages"
                    ]
                ),
                "chunk_ids": sorted(
                    set(
                        group[
                            "chunk_ids"
                        ]
                    )
                ),
            }
        )

    return references


# ---------------------------------------------------------------------
# LangGraph Agent
# ---------------------------------------------------------------------


def domain_evaluation_agent(
    state: EvaluationState,
) -> dict:
    """데이터센터 환경의 Domain Evaluation node."""

    technical_result = (
        state.get(
            "technical_result"
        )
    )

    if not technical_result:
        raise ValueError(
            "domain_evaluation_agent "
            "requires technical_result"
        )

    rubric = (
        load_domain_rubric()
    )

    system_prompt = (
        load_domain_prompt()
    )

    default_domain = (
        get_default_target_domain(
            rubric
        )
    )

    target_domain = (
        state.get(
            "target_domain"
        )
        or default_domain
    )

    if (
        default_domain
        not in target_domain
    ):
        raise ValueError(
            "현재 Domain rubric은 "
            f"'{default_domain}' 기준입니다. "
            f"received={target_domain}"
        )

    model = init_chat_model(
        MODEL_NAME,
        model_provider="openai",
        temperature=0,
    )

    embeddings = (
        BGEM3Embeddings()
    )

    domain_result: dict[
        str,
        Any,
    ] = {}

    references: list[
        dict[str, Any]
    ] = []

    for (
        tech_id,
        profile,
    ) in technical_result.items():
        if (
            tech_id
            not in TECH_PDF_PATHS
        ):
            raise ValueError(
                "Unknown technology: "
                f"{tech_id}"
            )

        tech_name = (
            profile.get(
                "title",
                tech_id,
            )
        )

        camp = str(
            profile.get(
                "camp",
                "",
            )
        )

        print(
            f"\n[도메인 평가] "
            f"{tech_name}"
        )

        retriever = TechRetriever(
            tech_id=tech_id,
            pdf_path=str(
                TECH_PDF_PATHS[
                    tech_id
                ]
            ),
            embeddings=embeddings,
        ).build()

        docs = (
            retrieve_domain_evidence(
                retriever,
                rubric,
                per_query_k=3,
            )
        )

        print(
            "  Domain RAG 근거 "
            f"{len(docs)}개 청크 검색"
        )

        context = (
            format_chunks(
                docs
            )
        )

        technical_digest = (
            build_technical_digest(
                profile
            )
        )

        user_prompt = f"""
평가 기술: {tech_name}
평가 도메인: {target_domain}

아래는 technical_result이다.

{json.dumps(
    technical_digest,
    ensure_ascii=False,
    indent=2,
)}

아래는 공식 Domain Evaluation Rubric이다.

{json.dumps(
    rubric,
    ensure_ascii=False,
    indent=2,
)}

아래는 원문 PDF에서 검색된 RAG 근거이다.

{context}

평가 규칙:

1. 각 EVALUATED 항목에는 최소 하나의
   실제 evidence가 있어야 한다.

2. evidence.source에는 제공된 RAG context의
   실제 chunk_id만 사용한다.

3. evidence.quote는 해당 chunk의 문장을
   가능한 한 그대로 복사한다.

4. technical_result는 보조 정보이며
   최종 점수는 RAG 원문으로 검증 가능해야 한다.

5. 수치는 baseline과 condition을 함께 고려한다.

6. claims와 measurements를 구분한다.

7. 확인되지 않은 내용을 추론하지 않는다.

8. 근거가 부족하면 NOT_VERIFIED로 기록한다.

9. NOT_VERIFIED의 score는 반드시 0이다.

10. EVALUATED 항목에서는 rubric에 정의된
    score 값만 사용할 수 있다.

11. scalability는
    Context Length / Batch Size / 동시 사용자 중
    정량적으로 확인된 축의 개수를 기준으로 한다.

    1개 축 = 3점
    2개 축 = 4점
    3개 축 = 5점

12. independent_validation은 제안자 논문만으로
    제3자 검증으로 판단하지 않는다.

13. evidence_maturity는 rubric을 엄격히 따른다.

    5점:
    peer-reviewed 논문의 직접 측정치

    4점:
    논문의 simulation/modeling 결과

    2점:
    직접 측정이 없는 vendor specification/whitepaper

    1점:
    secondary source/blog/inference

    직접 실험은 확인되지만
    peer-review 여부를 확인할 수 없는 경우에는
    임의로 2점 또는 5점을 주지 말고
    NOT_VERIFIED로 기록한다.

14. recency_openness의 코드·모델 공개 여부를
    근거 없이 추정하지 않는다.

15. 최종 score, coverage, verdict는
    LLM이 계산하지 않는다.
    Python 코드에서 계산한다.

16. 기술 간 최종 승자를 판단하지 않는다.
"""

        structured_model = (
            model.with_structured_output(
                _TechnologyDomainAssessment
            )
        )

        assessment = (
            structured_model.invoke(
                [
                    SystemMessage(
                        system_prompt
                    ),
                    HumanMessage(
                        user_prompt
                    ),
                ]
            )
        )

        assessment = (
            validate_llm_assessment(
                assessment,
                docs,
                rubric,
            )
        )

        assessment_data = (
            assessment.model_dump()
        )

        # 이름은 LLM 값이 아니라
        # upstream technical_result 값을 사용한다.
        assessment_data[
            "technology"
        ] = tech_name

        evaluation = (
            TechnologyDomainEvaluation(
                tech_id=tech_id,
                camp=camp,
                **assessment_data,
            )
        )

        evaluation = (
            calculate_domain_score(
                evaluation,
                rubric,
            )
        )

        domain_result[
            tech_id
        ] = (
            build_domain_output(
                evaluation,
                target_domain,
            )
        )

        references.extend(
            build_domain_references(
                tech_id,
                docs,
                evaluation,
            )
        )

        print(
            "  "
            f"score="
            f"{evaluation.weighted_score} "
            f"coverage="
            f"{evaluation.coverage} "
            f"verdict="
            f"{evaluation.verdict}"
        )

    return {
        "domain_result": (
            domain_result
        ),
        "references": (
            references
        ),
    }