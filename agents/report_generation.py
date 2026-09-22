"""상류의 실제 자료를 읽어 장별 보고서를 작성하고, 실패해도 원자료를 남긴다."""

import re
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

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
)

# 논문 출처는 상류가 "저자(연도). 제목. 학술지, 번호." 한 줄로 준다. 학술지만 기울임 처리하려고 쪼갠다.
_CITATION = re.compile(r"^(?P<authors>.*?\(\d{4}\))\.\s*(?P<title>.+?)\.\s*(?P<venue>[^,]+),\s*(?P<rest>.+?)\.?$")
_NO_DATE = "n.d."


def _date(value) -> str:
    """발행일을 YYYY-MM-DD 로 맞춘다.

    상류가 ISO(2026-09-22)와 RFC 2822(Wed, 23 Jul 2025 00:00:00 GMT)를 섞어 준다.
    해석되지 않으면 지어내지 않고 n.d. 로 남긴다.
    """
    text = str(value or "").strip()
    if not text:
        return _NO_DATE
    iso = re.match(r"\d{4}-\d{2}(-\d{2})?", text)
    if iso:
        return iso.group(0)
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return _NO_DATE


def _host(url: str) -> str:
    """URL 의 호스트. 웹 출처는 게시 주체가 곧 사이트이므로 기관명·사이트명 자리에 쓴다."""
    netloc = urlparse(url or "").netloc
    return netloc[4:] if netloc.startswith("www.") else netloc


def _paper_line(ref: dict, pages: list) -> str:
    """논문: 저자(YYYY). 논문제목. *학술지/학회명*, 권(호), 페이지."""
    citation = (ref.get("citation") or "").strip()
    tail = f", pp. {', '.join(str(n) for n in sorted(set(pages)))}" if pages else ""
    matched = _CITATION.match(citation)
    if not matched:
        return f"{citation or ref.get('source') or '출처 미상'}{tail}"
    g = matched.groupdict()
    return f"{g['authors']}. {g['title']}. *{g['venue'].strip()}*, {g['rest'].strip()}{tail}."


def _patent_line(ref: dict) -> str:
    """특허: 출원인(YYYY-MM). *특허명*, 특허번호/공개번호, URL"""
    applicant = ref.get("applicant") or ref.get("authors") or "출원인 미상"
    title = ref.get("title") or ref.get("source") or "특허명 미상"
    number = ref.get("number") or ref.get("locator") or "번호 미상"
    return f"{applicant}({_date(ref.get('date'))}). *{title}*, {number}, {ref.get('url', '')}"


def _web_line(ref: dict) -> str:
    """기타(웹페이지): 기관명 또는 작성자(YYYY-MM-DD). *제목*. 사이트명, URL"""
    url = ref.get("url", "")
    site = _host(url) or "사이트명 미상"
    publisher = ref.get("publisher") or ref.get("authors") or site
    title = ref.get("title") or ref.get("source") or "제목 미상"
    return f"{publisher}({_date(ref.get('date') or ref.get('as_of'))}). *{title}*. {site}, {url}"


def _reference_section(state: dict) -> str:
    """REFERENCE 장을 규정 서지 양식으로 직접 만든다.

    상류 Agent 마다 필드 이름이 달라(citation / source / title, as_of / date) 여기서 흡수한다.
    도메인 평가가 남긴 원문 청크 기록은 같은 논문의 인용 페이지로 합친다.
    `references` 에는 각 Agent 가 실제 근거로 사용한 출처만 들어오므로, 그대로 싣는다.
    """
    papers, patents, webs, pages = {}, {}, {}, {}
    for ref in state.get("references") or []:
        if not isinstance(ref, dict):
            continue
        kind = (ref.get("kind") or "").lower()
        if kind == "domain_evidence":          # 원문 청크 기록. 논문의 인용 페이지로 합친다
            pages.setdefault(ref.get("tech_id"), []).extend(ref.get("pages") or [])
        elif kind == "paper":
            papers.setdefault(ref.get("tech_id") or ref.get("url"), ref)
        elif kind == "patent":
            patents.setdefault(ref.get("url") or ref.get("number"), ref)
        else:
            webs.setdefault((ref.get("url") or "").rstrip("/") or ref.get("id"), ref)

    lines = [_paper_line(ref, pages.get(key, [])) for key, ref in papers.items()]
    lines += [_patent_line(ref) for ref in patents.values()]
    lines += [_web_line(ref) for ref in webs.values()]

    if not lines:
        return "## REFERENCE\n\n입력에 출처 정보가 없어 목록을 작성하지 못했다.\n"
    body = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    return f"## REFERENCE\n\n{body}\n"


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
            body += "\n\n" + _reference_section(state)
        except Exception as exc:                # 서지 생성 실패로 보고서 전체를 잃지 않는다
            errors.append(error_record("report/REFERENCE", exc))
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
