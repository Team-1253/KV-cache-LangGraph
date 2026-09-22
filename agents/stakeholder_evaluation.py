"""이해관계자 관점에서 기술을 조사하고 평가하는 LangChain Agent."""

import json
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_tavily import TavilySearch
from pydantic import BaseModel, Field

from agents.resilient import error_record
from agents.state import EvaluationState

# 입력 경로 ------------------------------------------------------------
WORK_DIR = Path(__file__).resolve().parent.parent
RUBRIC_PATH = WORK_DIR / "data" / "3-3_stakeholder_evaluation.json"
PROMPT_PATH = WORK_DIR / "prompts" / "stakeholder_evaluation.md"


# 구조화 출력용 class ------------------------------------------------------------
class Evidence(BaseModel):
    """평가 판단에 사용한 웹 검색 근거."""

    title: str = Field(description="출처 문서 또는 웹페이지 제목")
    url: str = Field(description="웹 검색 결과에서 확인한 실제 URL")
    claim: str = Field(description="이 출처가 평가 판단을 뒷받침하는 핵심 내용")
    source_category: Literal["DEVELOPER", "INDEPENDENT", "OTHER"] = Field(
        description="기술 개발 주체 자료인지, 독립적인 외부 자료인지 구분",
    )


class CriterionAssessment(BaseModel):
    """루브릭의 단일 평가 항목에 대한 결과."""

    criterion_id: Literal["3-3-a", "3-3-b", "3-3-c", "3-3-d", "3-3-e"] = Field(
        description="평가 루브릭 항목 ID",
    )
    status: Literal["VERIFIED", "NOT_VERIFIED"] = Field(
        description="판단 근거 확인 여부",
    )
    score: int | None = Field(
        description="VERIFIED이면 1~5점, NOT_VERIFIED이면 null",
    )
    rationale: str = Field(
        description="수집한 근거와 루브릭을 바탕으로 점수를 선택한 이유",
    )
    evidence: list[Evidence] = Field(
        description="해당 항목의 판단에 실제로 사용한 출처 목록",
    )


class TechnologyAssessment(BaseModel):
    """하나의 기술에 대한 이해관계자 평가 결과."""

    technology: str = Field(description="평가 대상 기술명")
    criteria: list[CriterionAssessment] = Field(
        description="3-3-a부터 3-3-e까지의 평가 결과",
    )
    summary: str = Field(
        description="해당 기술에 대한 이해관계자 관점의 종합 요약",
    )


# 로드 (환경변수, 루브릭, 프롬프트) -------------------------------------------------
def load_environment() -> None:
    """프로젝트 루트의 .env 파일을 불러온다."""

    env_path = WORK_DIR / ".env"
    load_dotenv(env_path)


def load_rubric() -> dict[str, Any]:
    """이해관계자 평가 루브릭 JSON을 읽고 기본 구조를 확인한다."""

    with RUBRIC_PATH.open("r", encoding="utf-8") as file:
        rubric: dict[str, Any] = json.load(file)

    if rubric.get("id") != "3-3":
        raise ValueError("이해관계자 평가 루브릭의 id는 '3-3'이어야 합니다.")

    criteria = rubric.get("criteria")

    if not isinstance(criteria, list) or len(criteria) != 5:
        raise ValueError("이해관계자 평가 루브릭에는 5개 항목이 있어야 합니다.")

    return rubric


def load_system_prompt() -> str:
    """이해관계자 평가 Agent의 시스템 프롬프트를 읽는다."""

    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()

    if not prompt:
        raise ValueError("이해관계자 평가 프롬프트가 비어 있습니다.")

    return prompt

# 웹 검색 결과 바탕으로 평가 요청 메세지 생성 -----------------------
def build_evaluation_request(
    technology: str,
    target_domain: str,
    technical_context: Any,
    rubric: dict[str, Any],
) -> str:
    """기술 정보와 평가 루브릭을 Agent 입력 메시지로 만든다."""

    rubric_text = json.dumps(
        rubric,
        ensure_ascii=False,
        indent=2,
    )

    technical_context_text = json.dumps(
        technical_context,
        ensure_ascii=False,
        indent=2,
        default=str,
    )

    return f"""
다음 기술을 이해관계자 관점에서 평가하세요.

평가 대상 기술:
{technology}

적용 대상 도메인:
{target_domain}

이전 기술 조사 Agent가 전달한 정보:
{technical_context_text}

반드시 적용해야 하는 평가 루브릭:
{rubric_text}

수행 요구사항:

1. 루브릭의 3-3-a부터 3-3-e까지 모든 항목을 평가하세요.
2. 각 항목에 제공된 웹 검색 결과를 근거로 평가하세요. 검색은 호출 측에서 수행합니다.
3. 각 평가 항목은 정확히 한 번씩 결과에 포함하세요.
4. 확인된 근거가 있는 경우에만 VERIFIED와 1~5점 점수를 부여하세요.
5. 조사했지만 근거를 확인하지 못한 경우 NOT_VERIFIED와 null 점수를 반환하세요.
6. 검색 결과에 실제로 존재하는 URL만 evidence에 포함하세요.
7. 총점은 계산하지 마세요.
""".strip()

# Agent 실행 및 결과 검증 ------------
EXPECTED_CRITERION_IDS = {
    "3-3-a",
    "3-3-b",
    "3-3-c",
    "3-3-d",
    "3-3-e",
}

def validate_assessment(
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Agent 결과가 루브릭의 필수 조건을 만족하는지 확인한다."""

    criteria = assessment.get("criteria", [])

    if len(criteria) != 5:
        raise ValueError("이해관계자 평가 결과는 정확히 5개 항목이어야 합니다.")

    actual_ids = [item["criterion_id"] for item in criteria]

    if set(actual_ids) != EXPECTED_CRITERION_IDS:
        raise ValueError(
            "이해관계자 평가 결과에는 "
            "3-3-a부터 3-3-e까지 모든 항목이 포함되어야 합니다."
        )

    if len(actual_ids) != len(set(actual_ids)):
        raise ValueError("이해관계자 평가 항목 ID가 중복되었습니다.")

    for item in criteria:
        criterion_id = item["criterion_id"]
        status = item["status"]
        score = item["score"]
        evidence = item["evidence"]

        if status == "VERIFIED":
            if not isinstance(score, int) or not 1 <= score <= 5:
                raise ValueError(
                    f"{criterion_id}의 VERIFIED 점수는 1~5 사이의 정수여야 합니다."
                )

            if not evidence:
                raise ValueError(
                    f"{criterion_id}가 VERIFIED이지만 출처가 없습니다."
                )

        if status == "NOT_VERIFIED" and score is not None:
            raise ValueError(
                f"{criterion_id}가 NOT_VERIFIED이면 점수는 null이어야 합니다."
            )

        for source in evidence:
            if not source["url"].startswith(("http://", "https://")):
                raise ValueError(
                    f"{criterion_id}에 올바르지 않은 출처 URL이 있습니다."
                )

    return assessment


def run_technology_assessment(
    technology: str,
    target_domain: str,
    technical_context: Any,
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """항목별로 한 번 검색하고 한 번 평가한다. 미확인 항목은 null로 반환한다."""

    load_environment()
    search = TavilySearch(
        max_results=5, search_depth="advanced",
        include_answer=False, include_raw_content=False,
        handle_tool_error=False,
    )

    request = build_evaluation_request(
        technology=technology,
        target_domain=target_domain,
        technical_context=technical_context,
        rubric=rubric,
    )

    search_results: dict[str, Any] = {}
    failed_criteria: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for criterion in rubric["criteria"]:
        criterion_id = criterion["id"]
        query = f"{technology} {criterion['question']}"
        try:
            result = search.invoke({"query": query})
            # TavilySearch는 API 예외를 raise 대신 {"error": 예외 객체}로 반환한다.
            if isinstance(result, dict) and "error" in result:
                failure = result["error"]
                if isinstance(failure, Exception):
                    raise failure
                raise RuntimeError("웹 검색이 실패했습니다.")
            if not isinstance(result, dict):
                raise ValueError("웹 검색 결과가 객체가 아닙니다.")
        except Exception as exc:
            error = error_record(f"stakeholder/search/{technology}/{criterion_id}", exc)
            errors.append(error)
            result = {"results": [], "error": error}
            failed_criteria[criterion_id] = {
                "criterion_id": criterion_id,
                "status": "NOT_VERIFIED",
                "score": None,
                "rationale": f"웹 검색 실패({error['error_type']})로 평가하지 못했습니다.",
                "evidence": [],
            }
        search_results[criterion_id] = {
            "query": query,
            "result": result,
        }

    if len(failed_criteria) == len(rubric["criteria"]):
        return validate_assessment({
            "technology": technology,
            "criteria": list(failed_criteria.values()),
            "summary": "모든 항목의 웹 검색이 실패해 이해관계자 평가를 완료하지 못했습니다.",
            "run_errors": errors,
        })

    model = init_chat_model(
        "gpt-4o-mini", model_provider="openai", temperature=0,
    ).with_structured_output(TechnologyAssessment)
    # Pydantic v2로 필수 필드와 nullable 점수를 보존하고, state에는 dict만 전달한다.
    response = model.invoke([
        SystemMessage(content=load_system_prompt()),
        HumanMessage(content=request + "\n\n" + json.dumps({
            "search_results": search_results,
            "instruction": "다섯 항목을 모두 반환하세요. 검색 실패 항목과 미확인 항목은 NOT_VERIFIED와 null로 반환하고, 검색 실패를 요약에도 명시하세요. 재검색·재평가는 없습니다.",
        }, ensure_ascii=False)),
    ]).model_dump()
    response["technology"] = technology
    for item in response["criteria"]:
        if item["criterion_id"] in failed_criteria:
            item.update(failed_criteria[item["criterion_id"]])
        if item["status"] == "NOT_VERIFIED":
            item["score"] = None
    response["run_errors"] = errors
    return validate_assessment(response)


# 총점 계산 및 출처 정리
def calculate_total_score(
    criteria: list[dict[str, Any]],
    rubric: dict[str, Any],
) -> float:
    """NOT_VERIFIED를 0점으로 처리해 100점 환산 총점을 계산한다."""

    scoring = rubric["scoring"]

    item_count = scoring["item_count"]
    maximum_item_score = max(scoring["item_score_range"])
    maximum_total = item_count * maximum_item_score

    missing_policy = scoring.get("missing_data_policy", {})
    penalty_value = missing_policy.get(
        "penalty_calculation_value",
        0,
    )

    scores = [
        item["score"]
        if item["status"] == "VERIFIED"
        and item["score"] is not None
        else penalty_value
        for item in criteria
    ]

    return round(
        sum(scores) / maximum_total * 100,
        1,
    )


def prepare_assessment_for_state(
    technology_id: str,
    assessment: dict[str, Any],
    rubric: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Agent 평가 결과를 stakeholder_result와 references 형태로 변환한다."""

    references: list[dict[str, Any]] = []
    normalized_criteria: list[dict[str, Any]] = []

    for criterion in assessment["criteria"]:
        criterion_id = criterion["criterion_id"]
        evidence_ids: list[str] = []

        for index, source in enumerate(criterion["evidence"], start=1):
            evidence_id = (f"stakeholder-{technology_id}-{criterion_id}-{index:02d}")   

            evidence_ids.append(evidence_id)

            references.append(
                {
                    "evidence_id": evidence_id,
                    "agent": "stakeholder_evaluation",
                    "technology": assessment["technology"],
                    "criterion_id": criterion_id,
                    "title": source["title"],
                    "url": source["url"],
                    "claim": source["claim"],
                    "source_category": source["source_category"],
                }
            )

        normalized_criteria.append(
            {
                "criterion_id": criterion_id,
                "status": criterion["status"],
                "score": criterion["score"],
                "rationale": criterion["rationale"],
                "evidence_ids": evidence_ids,
            }
        )

    total_score = calculate_total_score(
        criteria=assessment["criteria"],
        rubric=rubric,
    )

    verified_count = sum(
        criterion["status"] == "VERIFIED"
        for criterion in assessment["criteria"]
    )

    coverage = round(
        verified_count / len(assessment["criteria"]) * 100,
        1,
    )

    state_result = {
        "technology": assessment["technology"],
        "criteria": normalized_criteria,
        "score": total_score,
        "total_score": total_score,
        "coverage": coverage,
        "summary": assessment["summary"],
    }

    return state_result, references


# LangGraph 노드 완성

REQUIRED_TECH_IDS = (
    "deepseek_v2_mla",
    "itme",
)


def stakeholder_evaluation_agent(
    state: EvaluationState,
) -> dict[str, Any]:
    """기술 조사 결과를 이용해 기술별 이해관계자 평가를 수행한다."""

    technical_result = state.get("technical_result")
    target_domain = state.get("target_domain")

    if not technical_result:
        raise ValueError("State에 technical_result가 없습니다.")

    if not target_domain:
        raise ValueError("State에 target_domain이 없습니다.")

    missing_tech_ids = [
        tech_id
        for tech_id in REQUIRED_TECH_IDS
        if tech_id not in technical_result
    ]

    if missing_tech_ids:
        raise ValueError(
            "technical_result에 다음 기술이 없습니다: "
            + ", ".join(missing_tech_ids)
        )

    rubric = load_rubric()

    stakeholder_results: dict[str, Any] = {}
    all_references: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for tech_id in REQUIRED_TECH_IDS:
        tech_profile = technical_result[tech_id]

        if tech_profile.get("tech_id") != tech_id:
            raise ValueError(
                f"technical_result의 키와 TechProfile.tech_id가 다릅니다: {tech_id}"
            )

        assessment = run_technology_assessment(
            technology=tech_profile["title"],
            target_domain=target_domain,
            technical_context=tech_profile,
            rubric=rubric,
        )

        state_result, references = prepare_assessment_for_state(
            technology_id=tech_id,
            assessment=assessment,
            rubric=rubric,
        )

        stakeholder_results[tech_id] = state_result
        all_references.extend(references)
        errors.extend(assessment.get("run_errors", []))

    return {
        "stakeholder_result": stakeholder_results,
        "references": all_references,
        "run_errors": errors,
    }

# --------- 테스트 코드
if __name__ == "__main__":
    test_state: EvaluationState = {
        "selected_technologies": {
            "sw": "deepseek_v2_mla",
            "hw": "itme",
        },
        "target_domain": "데이터센터",
        "technical_result": {
            "deepseek_v2_mla": {
                "tech_id": "deepseek_v2_mla",
                "camp": "SW",
                "title": "DeepSeek-V2 MLA",
                "overview": (
                    "DeepSeek-V2 MLA는 Key와 Value 정보를 저차원 latent vector로 "
                    "압축해 KV Cache 크기를 줄이는 Attention 구조다."
                ),
                "mechanism": [
                    {
                        "text": (
                            "여러 Attention Head의 Key와 Value 정보를 공통 "
                            "저차원 latent vector로 압축한다."
                        ),
                        "source": {
                            "chunk_id": "deepseek-v2-page-4-chunk-1",
                            "page": 4,
                        },
                    }
                ],
                "scope": [
                    {
                        "text": (
                            "DeepSeek-V2 모델의 장문 Context LLM 추론 환경을 "
                            "평가 대상으로 한다."
                        ),
                        "source": {
                            "chunk_id": "deepseek-v2-page-6-chunk-1",
                            "page": 6,
                        },
                    }
                ],
                "claims": [
                    {
                        "text": (
                            "MLA가 기존 Multi-Head Attention보다 KV Cache "
                            "저장량을 크게 줄일 수 있다고 주장한다."
                        ),
                        "baseline": "Multi-Head Attention",
                        "source": {
                            "chunk_id": "deepseek-v2-page-5-chunk-2",
                            "page": 5,
                        },
                    }
                ],
                "measurements": [
                    {
                        "metric": "KV Cache reduction",
                        "value": "93.3%",
                        "baseline": "Multi-Head Attention",
                        "condition": "논문에 제시된 DeepSeek-V2 모델 구성",
                        "source": {
                            "chunk_id": "deepseek-v2-page-5-chunk-3",
                            "page": 5,
                        },
                    }
                ],
                "limits_explicit": [
                    {
                        "text": (
                            "압축된 latent vector에 RoPE를 직접 적용하기 어려워 "
                            "Decoupled RoPE 구조가 추가된다."
                        ),
                        "basis": "논문의 MLA 아키텍처 설명",
                        "source": {
                            "chunk_id": "deepseek-v2-page-6-chunk-2",
                            "page": 6,
                        },
                    }
                ],
                "limits_implicit": [
                    {
                        "text": (
                            "기존 모델에 MLA를 적용하려면 모델 구조 변경이나 "
                            "재학습이 필요할 수 있다."
                        ),
                        "basis": "MLA가 모델 Attention 구조에 포함된다는 평가 조건",
                        "source": {
                            "chunk_id": "deepseek-v2-page-4-chunk-1",
                            "page": 4,
                        },
                    }
                ],
                "evidence_level": "strong",
                "retrieval": {
                    "query_count": 5,
                    "retrieved_chunk_count": 12,
                    "used_chunk_count": 6,
                },
            },
            "itme": {
                "tech_id": "itme",
                "camp": "HW",
                "title": "SK hynix ITME",
                "overview": (
                    "ITME는 CXL 기반 Hybrid Memory를 활용해 LLM 추론 시 "
                    "사용할 수 있는 메모리 계층을 확장하는 하드웨어 접근이다."
                ),
                "mechanism": [
                    {
                        "text": (
                            "GPU 메모리와 CXL 기반 확장 메모리 사이에서 "
                            "데이터를 배치하고 이동시킨다."
                        ),
                        "source": {
                            "chunk_id": "itme-page-3-chunk-1",
                            "page": 3,
                        },
                    }
                ],
                "scope": [
                    {
                        "text": (
                            "데이터센터 LLM 추론 환경과 CXL Hybrid Memory "
                            "구성을 평가 대상으로 한다."
                        ),
                        "source": {
                            "chunk_id": "itme-page-4-chunk-1",
                            "page": 4,
                        },
                    }
                ],
                "claims": [
                    {
                        "text": (
                            "제한된 GPU 메모리 용량을 확장하고 LLM 추론의 "
                            "메모리 병목을 완화할 수 있다고 주장한다."
                        ),
                        "baseline": "GPU 로컬 메모리만 사용하는 구성",
                        "source": {
                            "chunk_id": "itme-page-2-chunk-2",
                            "page": 2,
                        },
                    }
                ],
                "measurements": [
                    {
                        "metric": "LLM inference throughput",
                        "value": "논문 평가값 참조",
                        "baseline": "기존 GPU 메모리 기반 시스템",
                        "condition": "논문의 CXL Hybrid Memory 평가 환경",
                        "source": {
                            "chunk_id": "itme-page-7-chunk-1",
                            "page": 7,
                        },
                    }
                ],
                "limits_explicit": [
                    {
                        "text": (
                            "메모리 계층 확장을 위해 CXL 장비와 별도의 "
                            "시스템 구성이 필요하다."
                        ),
                        "basis": "논문의 시스템 구성 설명",
                        "source": {
                            "chunk_id": "itme-page-4-chunk-2",
                            "page": 4,
                        },
                    }
                ],
                "limits_implicit": [
                    {
                        "text": (
                            "프로토타입 평가 결과와 실제 양산 데이터센터 환경의 "
                            "성능 사이에 차이가 존재할 수 있다."
                        ),
                        "basis": "논문의 평가 플랫폼과 상용 환경 차이",
                        "source": {
                            "chunk_id": "itme-page-8-chunk-1",
                            "page": 8,
                        },
                    }
                ],
                "evidence_level": "limited",
                "retrieval": {
                    "query_count": 5,
                    "retrieved_chunk_count": 15,
                    "used_chunk_count": 6,
                },
            },
        },
    }

    test_result = stakeholder_evaluation_agent(test_state)

    print(
        json.dumps(
            test_result,
            ensure_ascii=False,
            indent=2,
        )
    )
