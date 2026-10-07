"""실제 점수·근거는 직접 싣고, 출처가 연결된 해석을 모델에 요청한다."""

import json
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.state import EvaluationState, evaluation_material

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "report_generation.md"
LABELS = {"trl": "기술 성숙도", "market": "시장성", "stakeholder": "이해관계자", "domain": "도메인 적용성"}


class Paragraph(BaseModel):
    text: str = Field(description="입력 평가와 출처에 근거한 문단. 수치를 쓰면 baseline·조건을 함께 밝히고, 미확인 항목을 채택·투자 사실로 바꾸지 않는다.")
    reference_ids: list[str] = Field(max_length=5, description="이 문단의 주장을 직접 뒷받침하는 입력 references의 id만 최대 5개. 전체 출처 목록을 복사하지 않는다.")


class ReportNarrative(BaseModel):
    summary: Paragraph = Field(description="핵심 판단을 3~4문장으로 요약한다. 성능 수치는 별도 측정값 표에 있으므로 반복하지 않는다.")
    background: Paragraph
    selection: Paragraph = Field(description="데이터센터에서 해결하려는 문제와 두 기술의 선정 이유. 성능 수치는 별도 측정값 표에 있으므로 반복하지 않는다.")
    overview: Paragraph
    implications: Paragraph = Field(description="기술·시장·이해관계자·도메인 평가를 연결해 적용 방향을 제시하고 해당 관점의 출처를 인용한다. 성능 수치는 측정값 표에 있으므로 반복하지 않는다.")
    limitations: Paragraph = Field(description="관점별 coverage·미확인 항목·측정 조건에서 한계를 도출하고 해당 근거 출처를 인용한다. 도메인 판정 보류와 상용 채택 근거의 한계를 반드시 구분한다.")


def report_generation_agent(state: EvaluationState) -> dict:
    material = evaluation_material(state)
    material["evaluation_result"] = state.get("evaluation_result", {})
    references = {ref["id"]: ref for ref in material["references"]}
    model = init_chat_model("gpt-5.6-luna", model_provider="openai", reasoning_effort="low",
                            use_responses_api=True, temperature=0, timeout=90, max_retries=2)
    narrative = model.with_structured_output(ReportNarrative).invoke([
        SystemMessage(PROMPT_PATH.read_text(encoding="utf-8")),
        HumanMessage(json.dumps(material, ensure_ascii=False)),
    ])
    used_ids = set()

    def citations(ids):
        valid = list(dict.fromkeys(ref_id for ref_id in ids if ref_id in references))
        used_ids.update(valid)
        return " ".join(f"[^{ref_id}]" for ref_id in valid)

    sections = []
    for title, paragraph in (
        ("SUMMARY", narrative.summary), ("1. 분석 배경", narrative.background),
        ("2. 기술 선정", narrative.selection), ("3. 기술 개요", narrative.overview),
    ):
        sections.append(f"## {title}\n\n{paragraph.text} {citations(paragraph.reference_ids)}")

    # 수치의 비교 대상과 조건을 모델이 생략하거나 바꿔 쓰지 않도록 원자료를 직접 싣는다.
    measurements = ["### 원문 측정값", "", "| 기술 | 지표·값 | baseline | 조건 | 출처 |",
                    "| --- | --- | --- | --- | --- |"]
    for profile in material["technical_result"].values():
        for measurement in profile["measurements"]:
            ref_id = measurement["source"]["reference_id"]
            measurements.append(
                f"| {profile['title']} | {measurement['metric']}: {measurement['value']} "
                f"| {measurement['baseline']} | {measurement['condition']} | {citations([ref_id])} |"
            )
    sections[-1] += "\n\n" + "\n".join(measurements)

    evaluation_text = ["## 4. 관점별 평가"]
    for result in material["evaluations"]:
        score = result["score"]
        display = "미확인" if score is None else "–".join(map(str, score)) if isinstance(score, list) else str(score)
        evaluation_text.extend([
            f"\n### {result['technology']} — {LABELS[result['perspective']]}",
            f"\n점수: **{display} ({result['score_scale']})** · 근거 coverage: **{result['coverage']:.0%}** · {result['verdict'] or result['status']}",
        ])
        if result["perspective"] == "trl":
            meta = result["metadata"]
            evaluation_text.append(f"문헌 발표 {meta['published']}, 기준일 {meta['as_of']}, 경과 {meta['elapsed_months']}개월. {meta['range_derivation']}")
        for criterion in result["criteria"]:
            ids = [e["reference_id"] for e in criterion["evidence"]]
            item_score = "미확인" if criterion["score"] is None else criterion["score"]
            evaluation_text.append(f"\n- **{criterion['name']}**: {item_score} ({criterion['status']}). {criterion['rationale']} {citations(ids)}")
            if criterion["metadata"].get("confidence_tag"):
                evaluation_text.append(f"  근거 신뢰도: {criterion['metadata']['confidence_tag']}, 검색 시도: {criterion['metadata']['attempts']}회.")
            for evidence in criterion["evidence"]:
                if evidence["text"] != criterion["rationale"]:
                    text = evidence["text"].replace("\n", " ")
                    evaluation_text.append(f"  근거 요지: {text[:350]}{'…' if len(text) > 350 else ''} {citations([evidence['reference_id']])}")
                if evidence.get("value"):
                    evaluation_text.append(f"  수치: {evidence['value']} {evidence.get('unit', '')}, baseline: {evidence.get('baseline', '')}, 조건: {evidence.get('condition', '')}.")
    sections.append("\n".join(evaluation_text))
    for title, paragraph in (("5. 시사점", narrative.implications), ("6. 한계점", narrative.limitations)):
        sections.append(f"## {title}\n\n{paragraph.text} {citations(paragraph.reference_ids)}")

    bibliography = ["## REFERENCE"]
    for ref_id in references:
        if ref_id in used_ids:
            ref = references[ref_id]
            page = f", p. {ref['page']}" if ref["page"] is not None else ""
            bibliography.append(f"[^{ref_id}]: {ref['title']} ({ref['date'] or '시점 미확인'}{page}). {ref['url']}")
    sections.append("\n\n".join(bibliography))
    report = "# 기술 다관점 평가 보고서\n\n" + "\n\n".join(sections) + "\n"
    if state.get("run_errors"):
        report += "\n### 실행 중 확인한 오류\n\n" + "\n".join(
            f"- {error['stage']}: {error['error_type']}" for error in state["run_errors"]
        ) + "\n"
    return {"final_report": report}
