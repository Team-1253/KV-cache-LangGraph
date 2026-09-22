"""상류의 실제 자료를 읽어 장별 보고서를 작성하고, 실패해도 원자료를 남긴다."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

from agents.resilient import (
    dump_data, error_record, fallback_report, response_text, source_appendix, source_data,
)
from agents.state import EvaluationState

_ROOT = Path(__file__).resolve().parent.parent
_PROMPT_PATH = _ROOT / "prompts" / "report_generation.md"
MODEL_NAME = "gpt-4o-mini"
SECTIONS = (
    ("1. 분석 배경", "자료에 나타난 분석 목적과 배경"),
    ("2. 기술 선정", "실제로 선정된 기술과 자료에 명시된 선정 이유"),
    ("3. 기술 개요", "기술별 동작 원리, 성능, 적용 범위와 제약"),
    ("4. 관점별 평가", "실제 확보된 관점별 평가와 근거, 점수와 척도"),
    ("5. 시사점", "평가 간 일치와 불일치, 이득과 비용, 적용 조건"),
    ("6. 한계점", "근거 공백, 불확실성, 누락되거나 실패한 단계"),
    ("REFERENCE", "입력에 실제로 있는 출처 정보와 인용 연결"),
)


def _generate(rules: str, request: str) -> str:
    llm = init_chat_model(
        MODEL_NAME, model_provider="openai", temperature=0, timeout=90, max_retries=1,
    )
    text = response_text(llm.invoke([
        SystemMessage(content=rules), HumanMessage(content=request),
    ]))
    if not text:
        raise ValueError("보고서 응답이 비어 있습니다.")
    return text


def _write_section(section, rules: str, material: str):
    title, subject = section
    try:
        text = _generate(rules, (
            f"작성할 장: {title}\n주제: {subject}\n"
            "전체 입력에서 이 장에 관련된 내용을 직접 찾아 연결하라. "
            "장 제목 없이 본문만 작성하라.\n\n확보된 입력 자료:\n" + material
        ))
        print(f"[report] {title} — {len(text)}자")
        return title, text, None
    except Exception as exc:
        error = error_record(f"report/{title}", exc)
        return title, "이 장의 자동 작성에 실패했다. 확보된 자료는 부록에 보존했다.", error


def report_generation_agent(state: EvaluationState) -> dict:
    """자유 형식의 평가 결과를 받아 가능한 장을 끝까지 작성한다."""
    errors = []
    try:
        rules = _PROMPT_PATH.read_text(encoding="utf-8")
        material = dump_data(source_data(state))
        with ThreadPoolExecutor(max_workers=3) as pool:
            sections = list(pool.map(
                lambda section: _write_section(section, rules, material), SECTIONS,
            ))
        errors = [error for _, _, error in sections if error]
        body = "\n\n".join(f"## {title}\n\n{text}" for title, text, _ in sections)
        try:
            summary = _generate(rules, (
                "아래 완성된 본문을 한국어 900자 이내로 요약하라. "
                "실패한 장이 있으면 부분 결과임을 명시하라. 제목 없이 본문만 작성하라.\n\n" + body
            ))
        except Exception as exc:
            errors.append(error_record("report/SUMMARY", exc))
            summary = "자동 요약에 실패했다. 아래 본문에 작성 가능한 결과를 수록했다."
        report = f"# 기술 다관점 평가 보고서\n\n## SUMMARY\n\n{summary}\n\n{body}\n"
        if errors or state.get("run_errors"):
            report += source_appendix({**state, "report_errors": errors})
        return {"final_report": report, "run_errors": errors}
    except Exception as exc:
        errors.append(error_record("report_generation", exc))
        return {
            "final_report": fallback_report(
                {**state, "report_errors": errors}, "보고서 자동 작성을 완료하지 못했다.",
            ),
            "run_errors": errors,
        }
