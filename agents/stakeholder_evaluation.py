import json
from pathlib import Path
from typing import Literal

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field

from agents.resilient import error_record

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "stakeholder_evaluation.md"
RUBRIC_PATH = ROOT / "data" / "3-3_stakeholder_evaluation.json"


class Evidence(BaseModel):
    """이해관계자 평가에 사용한 웹 출처와 출처 주체 구분."""

    url: str
    source_category: Literal["DEVELOPER", "INDEPENDENT", "OTHER"]


class CriterionAssessment(BaseModel):
    """이해관계자 평가 한 항목의 확인 상태, 점수와 근거."""

    criterion_id: Literal["3-3-a", "3-3-b", "3-3-c", "3-3-d", "3-3-e"]
    status: Literal["VERIFIED", "NOT_VERIFIED"]
    score: int | None = Field(default=None, ge=1, le=5)
    rationale: str = Field(
        description="원문 근거와 루브릭 점수 기준의 연결. 논문 소개를 기업 채택·PoC·투자로 추정하거나 다른 모델의 사례를 옮기지 않는다. 직접 근거가 없으면 NOT_VERIFIED, score=null."
    )
    evidence: list[Evidence]


class TechnologyAssessment(BaseModel):
    """한 기술에 대한 다섯 이해관계자 평가 항목."""

    criteria: list[CriterionAssessment] = Field(
        min_length=5,
        max_length=5,
        description="3-3-a, 3-3-b, 3-3-c, 3-3-d, 3-3-e를 정확히 한 번씩 반환한다. 미확인 항목도 포함한다.",
    )


def stakeholder_evaluation_agent(state: dict) -> dict:
    """웹 검색 근거로 채택·투자 등 이해관계자 항목을 평가한다."""

    rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    prompt = PROMPT_PATH.read_text(encoding="utf-8")

    search = TavilySearch(
        max_results=5, search_depth="advanced", handle_tool_error=False
    )
    model = init_chat_model(
        "gpt-5.6-luna",
        model_provider="openai",
        reasoning_effort="low",
        use_responses_api=True,
        temperature=0,
        timeout=90,
        max_retries=2,
    )

    evaluations, references, errors = {}, [], []

    for tech_id, profile in state.get("technical_result", {}).items():
        results, failed = {}, set()

        for criterion in rubric["criteria"]:
            try:
                response = search.invoke(
                    {"query": f"{profile['title']} {criterion['question']}"}
                )
                if "error" in response:
                    raise response["error"]
                results[criterion["id"]] = response["results"]
            except Exception as exc:
                errors.append(
                    error_record(f"stakeholder/search/{tech_id}/{criterion['id']}", exc)
                )
                results[criterion["id"]] = []
                failed.add(criterion["id"])

        assessment = model.with_structured_output(TechnologyAssessment).invoke(
            [
                SystemMessage(prompt),
                HumanMessage(
                    json.dumps(
                        {
                            "technology": profile["title"],
                            "instruction": state.get("instruction", ""),
                            "target_domain": state.get("target_domain", ""),
                            "technical_context": profile,
                            "rubric": rubric,
                            "search_results": results,
                        },
                        ensure_ascii=False,
                    )
                ),
            ]
        )

        by_id = {item.criterion_id: item for item in assessment.criteria}
        if len(assessment.criteria) != len(rubric["criteria"]) or set(by_id) != set(
            results
        ):
            raise ValueError(
                f"이해관계자 평가 항목이 루브릭과 다릅니다: {[item.criterion_id for item in assessment.criteria]}"
            )

        criteria = []

        for criterion in rubric["criteria"]:
            item = by_id[criterion["id"]]
            urls = {row["url"]: row for row in results[item.criterion_id]}
            evidence = []

            if item.status == "VERIFIED" and item.criterion_id not in failed:
                for index, source in enumerate(item.evidence, 1):
                    # 모델의 출처 제안은 실제 검색 결과에 있는 URL만 인정한다.
                    if source.url not in urls:
                        continue

                    row = urls[source.url]
                    ref_id = f"stakeholder-{tech_id}-{item.criterion_id}-{index}"
                    references.append(
                        {
                            "id": ref_id,
                            "tech_id": tech_id,
                            "perspective": "stakeholder",
                            "title": row["title"],
                            "url": row["url"],
                            "date": row.get("published_date") or "",
                            "page": None,
                            "content": row["content"],
                            "metadata": {
                                "kind": "web",
                                "source_category": source.source_category,
                            },
                        }
                    )
                    evidence.append({"reference_id": ref_id, "text": row["content"]})

            verified = bool(evidence) and item.score is not None
            rationale = item.rationale
            if item.status == "VERIFIED" and not verified:
                rationale = "검색 원문에서 점수를 뒷받침하는 근거를 확인하지 못함"

            criteria.append(
                {
                    "id": criterion["id"],
                    "name": criterion["question"],
                    "status": "VERIFIED" if verified else "NOT_VERIFIED",
                    "score": item.score if verified else None,
                    "rationale": (
                        "웹 검색 실패로 평가하지 못함"
                        if item.criterion_id in failed
                        else rationale
                    ),
                    "evidence": evidence,
                    "metadata": {},
                }
            )

        # 미확인은 개별 점수 null로 보존하되, 총점 분모에는 포함해 근거 부족을 반영한다.
        coverage = sum(c["status"] == "VERIFIED" for c in criteria) / len(criteria)
        evaluations[tech_id] = {
            "tech_id": tech_id,
            "technology": profile["title"],
            "perspective": "stakeholder",
            "status": (
                "VERIFIED"
                if coverage == 1
                else "PARTIAL" if coverage else "NOT_VERIFIED"
            ),
            "score": round(
                sum(c["score"] or 0 for c in criteria) / (len(criteria) * 5) * 100, 1
            ),
            "score_scale": "0-100",
            "coverage": coverage,
            "summary": " / ".join(c["rationale"] for c in criteria),
            "verdict": "",
            "criteria": criteria,
            "metadata": {"rubric": RUBRIC_PATH.name},
        }

    return {
        "stakeholder_result": evaluations,
        "references": references,
        "run_errors": errors,
    }
