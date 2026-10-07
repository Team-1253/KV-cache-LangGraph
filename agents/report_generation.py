import json
import re
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.state import EvaluationState, evaluation_material

PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "report_generation.md"
)
LABELS = {
    "trl": "기술 성숙도",
    "market": "시장성",
    "stakeholder": "이해관계자",
    "domain": "도메인 적용성",
}
STATUS_LABELS = {
    "VERIFIED": "확인됨",
    "NOT_VERIFIED": "미확인",
    "PARTIAL": "일부 확인",
}


class Paragraph(BaseModel):
    """보고서 문단과 해당 내용을 뒷받침하는 출처 ID."""

    text: str = Field(
        description="입력 평가와 출처에 근거한 문단. 수치를 쓰면 baseline·조건을 함께 밝히고, 미확인 항목을 채택·투자 사실로 바꾸지 않는다."
    )
    reference_ids: list[str] = Field(
        max_length=5,
        description="이 문단의 주장을 직접 뒷받침하는 입력 references의 id만 최대 5개. 전체 출처 목록을 복사하지 않는다.",
    )


class CriterionRationale(BaseModel):
    key: str = Field(description="입력 평가 항목의 report_key를 그대로 반환한다.")
    text: str = Field(description="해당 평가 항목의 판단 이유와 한계를 한국어 평서체로 작성한다.")


class ReportNarrative(BaseModel):
    """모델이 작성할 보고서의 여섯 서술 영역."""

    summary: Paragraph = Field(
        description="핵심 판단을 3~4문장으로 요약한다. 성능 수치는 별도 측정값 표에 있으므로 반복하지 않는다."
    )
    background: Paragraph
    selection: Paragraph = Field(
        description="데이터센터에서 해결하려는 문제와 두 기술의 선정 이유. 성능 수치는 별도 측정값 표에 있으므로 반복하지 않는다."
    )
    overview: Paragraph
    implications: Paragraph = Field(
        description="기술·시장·이해관계자·도메인 평가를 연결해 적용 방향을 제시하고 해당 관점의 출처를 인용한다. 성능 수치는 측정값 표에 있으므로 반복하지 않는다."
    )
    limitations: Paragraph = Field(
        description="관점별 coverage·미확인 항목·측정 조건에서 한계를 도출하고 해당 근거 출처를 인용한다. 도메인 판정 보류와 상용 채택 근거의 한계를 반드시 구분한다."
    )
    criterion_rationales: list[CriterionRationale] = Field(
        description="evaluations의 모든 criteria에 대해 report_key를 key로, 한국어 서술을 text로 반환한다. 확인된 항목은 핵심 판단 이유와 한계를 2~3문장으로 압축한다. 미확인 항목은 미확인인 대상과 사유를 1문장으로 작성한다. 점수 산정에 필요한 수치·비교 대상·조건은 유지하며 점수와 확인 상태를 변경하지 않는다. text에 원문 발췌, 내부 출처 ID, 각주, 상태 코드는 넣지 않는다. 기술명과 약어는 유지할 수 있다."
    )


def report_generation_agent(state: EvaluationState) -> dict:
    """모델이 작성한 해석에 평가 원자료와 각주를 결합해 보고서를 만든다."""

    material = evaluation_material(state)
    material["evaluation_result"] = state.get("evaluation_result", {})
    if state.get("report_quality"):
        material["previous_report"] = state.get("final_report", "")
        material["quality_feedback"] = state["report_quality"]
    # 공유 State를 변경하지 않고 보고서 작성에만 사용할 항목 키를 붙인다.
    material["evaluations"] = [
        {**result, "criteria": [
            {**criterion, "report_key": f"evaluation_{i}_criterion_{j}"}
            for j, criterion in enumerate(result["criteria"])
        ]}
        for i, result in enumerate(material["evaluations"])
    ]
    references = {ref["id"]: ref for ref in material["references"]}

    model = init_chat_model(
        "gpt-5.6-luna",
        model_provider="openai",
        reasoning_effort="low",
        use_responses_api=True,
        temperature=0,
        timeout=90,
        max_retries=2,
    )

    narrative = model.with_structured_output(ReportNarrative).invoke(
        [
            SystemMessage(
                PROMPT_PATH.read_text(encoding="utf-8")
                + "\n\n보고서 압축 규칙: 기존 장 구성은 유지한다. 각 서술 영역은 "
                "2~3문장, 요약은 3문장 이내로 작성한다. 관점별 항목은 핵심 근거와 "
                "한계만 남기고 점수를 다시 선언하거나 루브릭을 풀어 쓰지 않는다. "
                "같은 성능 수치는 측정값 표에 모으며 평가 이유에 꼭 필요한 경우만 "
                "본문에서 반복한다. 미확인 항목은 대상과 사유를 한 문장으로 쓴다. "
                "점수, 근거 확보율, 판정 보류, 수치의 비교 조건은 보존한다. "
                "REFERENCE에는 인용한 문헌의 서지 정보만 코드가 배치하며 "
                "긴 원문 발췌나 평가 서술은 넣지 않는다."
            ),
            HumanMessage(json.dumps(material, ensure_ascii=False)),
        ]
    )

    rationales = {item.key: item.text.strip() for item in narrative.criterion_rationales}
    citation_numbers = {}
    bibliography_refs = {}

    def prose(text):
        # 원자료에 붙은 내부 ID는 본문 각주와 중복되므로 제거한다.
        for ref_id in references:
            text = text.replace(ref_id, "")
        text = re.sub(r"\(\s*[,\s]*\)", "", text)
        return text.strip()

    def citations(ids):
        """유효한 출처에 최초 인용 순서대로 각주 번호를 부여한다."""

        valid = list(dict.fromkeys(ref_id for ref_id in ids if ref_id in references))
        markers = []
        for ref_id in valid:
            ref = references[ref_id]
            # URL이 없는 출처는 서로 다른 문헌일 수 있으므로 ID별로 유지한다.
            key = ref["url"] or ref_id
            if key not in citation_numbers:
                citation_numbers[key] = len(citation_numbers) + 1
                bibliography_refs[key] = ref
            page = f" (p. {ref['page']})" if ref["page"] is not None else ""
            marker = f"[^{citation_numbers[key]}]{page}"
            if marker not in markers:
                markers.append(marker)
        return " ".join(markers)

    sections = []

    for title, paragraph in (
        ("요약", narrative.summary),
        ("1. 분석 배경", narrative.background),
        ("2. 기술 선정", narrative.selection),
        ("3. 기술 개요", narrative.overview),
    ):
        sections.append(
            f"## {title}\n\n{paragraph.text} {citations(paragraph.reference_ids)}"
        )

    # 모델의 재서술로 점수·측정값·비교 조건이 달라지지 않도록 표와 항목은 직접 구성한다.
    evaluation_text = ["## 4. 관점별 평가"]
    for result in material["evaluations"]:
        score = result["score"]
        display = (
            "미확인"
            if score is None
            else "–".join(map(str, score)) if isinstance(score, list) else str(score)
        )

        evaluation_text.extend(
            [
                f"\n### {result['technology']} — {LABELS[result['perspective']]}",
                f"\n점수: **{display} ({result['score_scale']})** · 근거 확보율: **{result['coverage']:.0%}** · {STATUS_LABELS.get(result['verdict'] or result['status'], result['verdict'] or result['status'])}",
            ]
        )

        if result["perspective"] == "trl":
            meta = result["metadata"]
            evaluation_text.append(
                f"문헌 발표 {meta['published']}, 기준일 {meta['as_of']}, 경과 {meta['elapsed_months']}개월. {meta['range_derivation']}"
            )

        for criterion in result["criteria"]:
            rationale = rationales.get(criterion["report_key"]) or criterion["rationale"]
            ids = [e["reference_id"] for e in criterion["evidence"]]
            item_score = "미확인" if criterion["score"] is None else criterion["score"]
            evaluation_text.append(
                f"\n- **{criterion['name']}**: {item_score} ({STATUS_LABELS.get(criterion['status'], criterion['status'])}). {prose(rationale)} {citations(ids)}"
            )

    sections.append("\n".join(evaluation_text))

    for title, paragraph in (
        ("5. 시사점", narrative.implications),
        ("6. 한계점", narrative.limitations),
    ):
        sections.append(
            f"## {title}\n\n{paragraph.text} {citations(paragraph.reference_ids)}"
        )

    measurements = [
        "### 원문 측정값",
        "",
        "| 기술 | 지표·값 | 비교 기준 | 조건 | 출처 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for profile in material["technical_result"].values():
        for measurement in profile["measurements"]:
            ref_id = measurement["source"]["reference_id"]
            measurements.append(
                f"| {profile['title']} | {measurement['metric']}: {measurement['value']} "
                f"| {measurement['baseline']} | {measurement['condition']} | {citations([ref_id])} |"
            )

    bibliography = ["## REFERENCE", "\n".join(measurements)]
    for key, number in citation_numbers.items():
        ref = bibliography_refs[key]
        bibliography.append(
            f"[^{number}]: {ref['title']} ({ref['date'] or '시점 미확인'}). {ref['url']}"
        )

    sections.append("\n\n".join(bibliography))

    report = "# 기술 다관점 평가 보고서\n\n" + "\n\n".join(sections) + "\n"

    if state.get("run_errors"):
        report += (
            "\n### 실행 중 확인한 오류\n\n"
            + "\n".join(
                f"- {error['stage']}: {error['error_type']}"
                for error in state["run_errors"]
            )
            + "\n"
        )

    return {"final_report": report}
