import json
import os
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field

from agents.resilient import error_record

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "market_evaluation.md"
RUBRIC_PATH = ROOT / "data" / "3-2_market_evaluation.json"
MAX_ATTEMPTS = 3


class EvidenceJudgement(BaseModel):
    """시장 검색 근거의 신뢰도 점수와 판단 이유."""

    evidence_score: int = Field(ge=0, le=5)
    reason: str


class NumericEvidence(BaseModel):
    """검색 결과에서 채점에 사용한 수치와 비교 조건."""

    result_index: int = Field(ge=1)
    value: str
    unit: str = ""
    baseline: str = ""
    note: str = ""


class RubricScore(BaseModel):
    """시장 평가 한 항목의 점수와 사용한 검색 근거."""

    score: int = Field(ge=1, le=5)
    rationale: str
    source_indices: list[int] = Field(
        description="실제로 채점에 사용한 검색 결과 번호. 정성 근거도 포함"
    )
    evidence: list[NumericEvidence] = Field(default_factory=list)


SEEDS = {
    "3-2-a": "AI inference market size CAGR LLM serving TAM growth",
    "3-2-b": "LLM inference cost per token GPU memory TCO KV cache reduction",
    "3-2-c": "production deployment hyperscaler adoption commercial service",
    "3-2-d": "serving framework vLLM SGLang integration ecosystem",
}


def build_queries(criterion: dict, profile: dict, attempt: int) -> list[str]:
    """평가 항목과 검색 시도에 맞춰 두 개의 검색 질의를 만든다."""

    title = profile["title"]
    seed = SEEDS[criterion["id"]]
    terms = " ".join(criterion["evidence"])

    extra = "" if attempt == 1 else profile["overview"][:250]
    if attempt == 3:
        extra = "market analysis report"

    return [f"{title} {seed} {extra}"[:500], f"{title} {terms}"[:500]]


def market_evaluation_agent(
    state: dict, criteria: list[dict] | None = None
) -> dict:
    """항목별 웹 근거의 신뢰도를 판단한 뒤 시장성 점수와 출처를 반환한다."""

    rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    if criteria is not None:
        rubric["criteria"] = criteria

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    model = init_chat_model(
        os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        model_provider="openai",
        reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low"),
        use_responses_api=True,
        temperature=0,
        timeout=90,
        max_retries=2,
    )

    evaluations, references, errors = {}, [], []

    for tech_id, profile in state.get("technical_result", {}).items():
        criteria = []

        for criterion in rubric["criteria"]:
            results, reason, confidence = [], "검색 결과 없음", 0

            for attempt in range(1, MAX_ATTEMPTS + 1):
                search = TavilySearch(
                    max_results=5 if attempt == 1 else 8,
                    search_depth="basic" if attempt == 1 else "advanced",
                    topic="news" if criterion["id"] == "3-2-c" else "general",
                    include_answer=False,
                    include_raw_content=False,
                    handle_tool_error=False,
                )

                found = {}
                for query in build_queries(criterion, profile, attempt):
                    try:
                        response = search.invoke({"query": query})
                        if "error" in response:
                            raise response["error"]
                        found.update({row["url"]: row for row in response["results"]})
                    except Exception as exc:
                        error = error_record(
                            f"market/search/{tech_id}/{criterion['id']}", exc
                        )
                        errors.append(error)
                        reason = f"검색 실패: {error['error_type']}"

                results = list(found.values())
                if not results:
                    continue

                request = json.dumps(
                    {
                        "technology": profile["title"],
                        "instruction": state.get("instruction", ""),
                        "technical_context": profile,
                        "criterion": criterion,
                        "results": results,
                    },
                    ensure_ascii=False,
                )
                judgement = model.with_structured_output(EvidenceJudgement).invoke(
                    [
                        SystemMessage(prompt),
                        HumanMessage("태스크 1 — 근거 판정\n" + request),
                    ]
                )
                confidence, reason = judgement.evidence_score, judgement.reason
                if confidence >= 3:
                    break

            item = {
                "id": criterion["id"],
                "name": criterion["question"],
                "status": "NOT_VERIFIED",
                "score": None,
                "rationale": reason,
                "evidence": [],
                "metadata": {"confidence_tag": "NOT_VERIFIED", "attempts": attempt},
            }

            # 근거 신뢰도는 검색 재시도 기준이며, 약한 근거도 신뢰도 표기를 붙여 채점한다.
            if results and confidence:
                try:
                    scored = model.with_structured_output(RubricScore).invoke(
                        [
                            SystemMessage(prompt),
                            HumanMessage("태스크 2 — 루브릭 채점\n" + request),
                        ]
                    )

                    indices = set(scored.source_indices) | {
                        e.result_index for e in scored.evidence
                    }
                    for index in sorted(indices):
                        if not 1 <= index <= len(results):
                            continue

                        row = results[index - 1]
                        ref_id = f"market-{tech_id}-{criterion['id']}-{index}"
                        references.append(
                            {
                                "id": ref_id,
                                "tech_id": tech_id,
                                "perspective": "market",
                                "title": row["title"],
                                "url": row["url"],
                                "date": row.get("published_date") or "",
                                "page": None,
                                "content": row["content"],
                                "metadata": {"kind": "web"},
                            }
                        )

                        numeric = [
                            e for e in scored.evidence if e.result_index == index
                        ]
                        for evidence in numeric or [None]:
                            entry = {"reference_id": ref_id, "text": scored.rationale}
                            if evidence:
                                entry.update(
                                    value=evidence.value,
                                    unit=evidence.unit,
                                    baseline=evidence.baseline,
                                    condition=evidence.note,
                                )
                            item["evidence"].append(entry)

                    if item["evidence"]:
                        item.update(
                            status="VERIFIED",
                            score=scored.score,
                            rationale=scored.rationale,
                        )
                        item["metadata"]["confidence_tag"] = (
                            "강함"
                            if confidence == 5
                            else "보통" if confidence >= 3 else "약함"
                        )
                except Exception as exc:
                    error = error_record(
                        f"market/score/{tech_id}/{criterion['id']}", exc
                    )
                    errors.append(error)
                    item["rationale"] = f"채점 실패: {error['error_type']}"

            criteria.append(item)

        # 미확인은 개별 점수 null로 보존하되, 총점 분모에는 포함해 근거 부족을 반영한다.
        coverage = sum(c["status"] == "VERIFIED" for c in criteria) / len(criteria)
        evaluations[tech_id] = {
            "tech_id": tech_id,
            "technology": profile["title"],
            "perspective": "market",
            "status": (
                "VERIFIED"
                if coverage == 1
                else "PARTIAL" if coverage else "NOT_VERIFIED"
            ),
            "score": round(
                sum(c["score"] or 0 for c in criteria) / (len(criteria) * 5) * 100, 2
            ),
            "score_scale": "0-100",
            "coverage": coverage,
            "summary": " / ".join(c["rationale"] for c in criteria),
            "verdict": "",
            "criteria": criteria,
            "metadata": {"rubric": RUBRIC_PATH.name},
        }

    return {
        "market_result": evaluations,
        "references": references,
        "run_errors": errors,
    }
