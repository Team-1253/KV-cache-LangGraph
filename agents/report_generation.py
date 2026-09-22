"""보고서 생성 Agent.

평가가 끝난 State 를 최종 보고서 문자열 하나로 바꾼다.
바깥(`app.py`)에서 보면 노드 1개이고 출력 State Key 도 `final_report` 하나지만,
안에서는 장별 작성 -> 검증 -> 재작성 -> 조립을 도는 작은 그래프가 돈다.
작업용 중간 키(`sections`, `issues` 등)는 이 모듈 안의 `_ReportState` 에만 있고
공용 `EvaluationState` 로 새어 나가지 않는다.

    prepare ─┬→ section_1 ┐
             ├→ ...       ├→ summary → validate ─┬→ revise ─┐
             └→ section_6 ┘        ↑              └→ render → END
                                   └──────────────┘

장 구성은 고정이지만 **각 장에서 무엇을 쓸지는 들어온 자료가 정한다.**
작성 항목마다 `requires` 로 "이 항목을 쓰려면 State 의 무엇이 있어야 하는가" 를 선언해 두고,
`_prepare` 가 실제 State 를 훑어 살아남은 항목만 프롬프트에 넣는다.
자료가 없는 항목은 지시 자체를 하지 않으므로 LLM 이 지어낼 여지가 없고,
목표 분량도 살아남은 항목 수에 비례해 줄여 물타기를 막는다.

설계 원칙: 틀리면 안 되는 것은 LLM 에게 맡기지 않는다.
  - 관점별 평가 비교표 → `_score_table` 이 State 값으로 직접 렌더링
  - REFERENCE         → `_render` 가 실제 인용된 출처만 규정 형식으로 출력
  - 근거 통계·편향 방지 조치 → `_prepare` 가 계산해서 넘김
LLM 은 서술만 한다.
"""

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from agents.state import EvaluationState

MODEL_NAME = "gpt-4o-mini"
SUMMARY_MAX_CHARS = 900          # SUMMARY 는 1/2 페이지를 넘지 않는다
CHARS_PER_POINT = 300            # 살아남은 작성 항목 1개당 목표 분량
MAX_EXPANSION = 2.0              # 주입 자료 글자수의 이 배수를 넘겨 쓰라고 요구하지 않는다
MIN_CHARS, MAX_CHARS = 200, 2000
MIN_RATIO = 0.7                  # 목표 분량의 이 비율에 못 미치면 재작성
MAX_REVISION = 2                 # 재작성 상한. 넘으면 문제를 안고 출력한다

CITE = re.compile(r"\[ref:([^\]\s]+)\]")
HONORIFIC = re.compile(r"(습니다|입니다|합니다|됩니다|십시오)")  # 장별 문체가 섞이는 것을 막는다
# 팀 완료 기준: 기술의 우열이나 단일 승자를 결정하지 않는다.
# "단일 승자를 결정하지 않았다" 처럼 부정문에도 걸리는 낱말은 넣지 않는다 (오탐).
RANKING = re.compile(r"(더 나은 선택|더 우수한|가장 우수|선택해야 한다|명백히 앞선|승자는)")
MISSING_NOTE = "이 장을 작성할 자료가 확보되지 않았다. (NOT_VERIFIED)"

_ROOT = Path(__file__).resolve().parent.parent
_PROMPT_PATH = _ROOT / "prompts" / "report_generation.md"


# ──────────────────────────────────────────────────────────────────────
# 목차
#
# points 의 각 원소는 (작성 항목, requires) 이다.
#   requires = None        항상 쓴다
#            = "a.b"       State 의 a.b 가 비어 있지 않을 때만 쓴다
#            = ("a", "b")  둘 중 하나라도 있으면 쓴다
# ──────────────────────────────────────────────────────────────────────

PERSPECTIVES = [                       # (표시명, State Key, 값 필드)
    ("기술 성숙도(TRL)", "trl_result", "trl_value"),
    ("시장", "market_result", "score"),
    ("이해관계자", "stakeholder_result", "score"),
    ("도메인", "domain_result", "score"),
]
PERSPECTIVE_KEYS = [k for _, k, _ in PERSPECTIVES]
RESULT_KEYS = ["technical_result", *PERSPECTIVE_KEYS]

SECTIONS = [
    {
        "id": "1",
        "title": "분석 배경",
        # technical_result 를 주면 배경 대신 기술 설명을 쓰게 되어 3장과 겹친다
        "keys": ["background_facts", "selected_technologies", "target_domain"],
        "points": [
            ("KV cache 가 LLM 서빙에서 병목이 되는 구조적 이유", "background_facts"),
            ("컨텍스트 길이 증가에 따른 메모리 요구량 추세 (수치가 있으면 기준 시점과 함께)",
             "background_facts"),
            ("SW 접근과 HW 접근을 함께 보아야 하는 이유", "background_facts"),
            ("이 분석이 답하려는 질문 — 기술의 동작 원리나 성능은 여기서 다루지 않는다", None),
        ],
    },
    {
        "id": "2",
        "title": "기술 선정",
        "keys": ["selected_technologies", "background_facts", "technical_result"],
        "points": [
            ("선정한 SW / HW 기술 2건", "selected_technologies"),
            ("각 기술을 선정한 이유 (background_facts 의 선정 근거를 그대로 활용할 것)",
             "background_facts"),
            # 기술명만으로는 이 이유를 쓸 수 없다. 배경이나 기술 내용이 있어야 한다
            ("두 기술을 같은 평가 축에 놓는 것이 타당한 이유",
             ("background_facts", "technical_result")),
        ],
    },
    {
        "id": "3",
        "title": "기술 개요",
        "keys": ["technical_result", "selected_technologies"],
        "points": [
            ("기술별 핵심 접근 방향과 동작 원리", "technical_result"),
            ("보고된 성능 특성 (수치는 baseline 과 함께)", "technical_result"),
            ("기술 자체의 한계점 — 분석의 한계가 아니라 기술의 한계를 쓴다", "technical_result"),
            ("두 기술을 나란히 놓은 비교표", "technical_result"),
        ],
    },
    {
        "id": "4",
        "title": "관점별 평가",
        "keys": ["score_table", *PERSPECTIVE_KEYS, "target_domain"],
        "points": [
            ("참고 자료의 score_table 을 그대로 본문 맨 앞에 옮길 것 (숫자를 다시 쓰지 말 것)",
             tuple(PERSPECTIVE_KEYS)),
            ("기술 성숙도(TRL): 기술별 TRL 값의 판단 근거", "trl_result"),
            ("시장 관점: 시장 규모, 채택 현황, 생태계", "market_result"),
            ("이해관계자 관점: 경쟁사 / 도입기업 / 개발자 / 투자업계", "stakeholder_result"),
            ("도메인 관점: target_domain 기준 적용 적합성", "domain_result"),
        ],
    },
    {
        "id": "5",
        "title": "시사점",
        # selected_technologies 가 없으면 LLM 이 기술명 약어를 임의로 풀어 쓴다
        "keys": ["evaluation_result", "selected_technologies"],
        # 불일치 의견은 건별로 항목을 펼친다. 한 줄 지시로는 LLM 이 일부만 쓴다
        "expand": [("evaluation_result.disagreements",
                    "엇갈리는 지점 {i}: 「{item}」 — 어느 관점이 왜 그렇게 보는지, "
                    "이 엇갈림이 도입 판단에 어떤 의미인지 독립된 문단으로 서술할 것")],
        "points": [
            ("관점 간 일치 의견", "evaluation_result.agreements"),
            ("trade-off 관계", "evaluation_result.tradeoffs"),
            ("도입 조건별 시사점 (어느 쪽이 낫다가 아니라, 어떤 조건에서 어느 쪽이 맞는지)",
             "evaluation_result.implications"),
        ],
    },
    {
        "id": "6",
        "title": "한계점",
        "keys": ["evidence_stats", "method_notes", "technical_result"],
        "points": [
            ("기술별로 확보한 근거의 수와 유형 차이에서 오는 정보 비대칭 "
             "(evidence_stats 의 수치를 인용할 것)", "references"),
            ("공개 정보 기반 추정이 적용된 범위와 해석상 주의점", "technical_result"),
            ("확증편향을 줄이기 위해 실제로 취한 조치 — method_notes 에 있는 항목만 쓸 것. "
             "하지 않은 조치를 지어내지 말 것", None),
        ],
    },
]

SECTION_BY_ID = {s["id"]: s for s in SECTIONS}
BODY_IDS = [s["id"] for s in SECTIONS]
REPORT_ORDER = ["summary"] + BODY_IDS
HEADINGS = {"summary": "SUMMARY", "ref": "REFERENCE",
            **{s["id"]: f"{s['id']}. {s['title']}" for s in SECTIONS}}


# ──────────────────────────────────────────────────────────────────────
# 내부 State
# ──────────────────────────────────────────────────────────────────────

def _merge(left: dict, right: dict) -> dict:
    """섹션 노드가 병렬로 쓰므로 합쳐야 한다. 없으면 InvalidUpdateError."""
    return {**left, **right}


class _ReportState(TypedDict, total=False):
    """보고서 Agent 내부 전용. EvaluationState 를 오염시키지 않으려고 따로 둔다."""

    # EvaluationState 에서 그대로 받는 입력
    background_facts: dict[str, str]
    selected_technologies: dict[str, str]
    target_domain: str
    technical_result: dict[str, Any]
    trl_result: dict[str, Any]
    market_result: dict[str, Any]
    stakeholder_result: dict[str, Any]
    domain_result: dict[str, Any]
    evaluation_result: dict[str, list[str]]
    references: list[dict[str, Any]]

    # 작업 영역
    source: dict[str, Any]       # 섹션 프롬프트에 주입할 자료 묶음
    plan: dict[str, dict]        # 장별 {points, min_chars, keys} — 들어온 자료가 정한다
    ref_index: dict[str, Any]    # 대표 id -> 출처
    ref_alias: dict[str, str]    # 모든 id -> 대표 id
    sections: Annotated[dict[str, str], _merge]
    issues: list[dict[str, str]]
    attempt: int
    final_report: str


# ──────────────────────────────────────────────────────────────────────
# 자료 유무 판정
# ──────────────────────────────────────────────────────────────────────

def _dig(state: dict, path: str) -> Any:
    """'a.b' 형태의 경로로 State 안을 따라 들어간다. 없으면 None."""
    cur: Any = state
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None or cur == "" or cur == [] or cur == {}:
            return None
    return cur


def _has(state: dict, path: str) -> bool:
    """State 에 이 경로의 자료가 실제로 들어왔는지.

    None / 빈 문자열 / 빈 리스트 / 빈 dict 는 모두 "없음" 으로 본다.
    값이 있으나 내용이 NOT_VERIFIED 인 경우는 "있음" 이다.
    그 사실 자체가 한계점으로 서술할 가치가 있기 때문이다.
    """
    return _dig(state, path) is not None


def _present(state: dict, requires) -> bool:
    """작성 항목의 requires 를 판정한다. None 이면 항상 참, 튜플이면 하나만 있어도 참."""
    if requires is None:
        return True
    if isinstance(requires, (tuple, list)):
        return any(_has(state, p) for p in requires)
    return _has(state, requires)


_DERIVED = {"score_table", "evidence_stats", "method_notes"}   # _prepare 가 만드는 자료


def _expand_points(state: dict, section: dict) -> list[str]:
    """리스트로 들어온 자료를 항목 하나씩으로 펼친다.

    "불일치 의견을 빠뜨리지 마라" 라고 한 줄로 지시하면 LLM 은 긴 목록 중 일부만 쓴다.
    항목 자체를 N개로 펼치면 누락이 구조적으로 어려워지고, 항목 수에 비례해 목표 분량도
    같이 늘어난다.
    """
    points = []
    for path, template in section.get("expand", []):
        for i, item in enumerate(_dig(state, path) or [], 1):
            points.append(template.format(i=i, item=str(item)[:150]))
    return points


def _build_plan(state: dict, source: dict) -> dict[str, dict]:
    """들어온 자료를 보고 장별 작성 항목·목표 분량·주입할 자료를 정한다.

    두 가지를 함께 줄여야 한다.

    1. 자료가 없는 항목은 지시 자체를 하지 않는다. 쓰라고 시켜 놓고 자료를 안 주면
       LLM 은 "자료 미확보" 라고 쓰거나 지어낸다.
    2. 목표 분량도 **실제 주입되는 자료의 양** 을 넘지 않게 묶는다. 항목만 줄이고 분량을
       그대로 두면 남은 자료로 분량을 채우려고 지어낸다 (기술명 2개만 주고 600자를 쓰라고
       하면 없는 동작 원리를 만들어 낸다). 구조화된 자료를 문장으로 풀면 길어지는 것이
       정상이므로 상한은 자료 글자수의 MAX_EXPANSION 배로 둔다.
    3. 하한만 주면 남는 분량을 다른 장의 내용이나 지어낸 서술로 채우므로 상한도 함께 준다.
    """
    plan = {}
    for section in SECTIONS:
        points = _expand_points(state, section)
        points += [text for text, req in section["points"] if _present(state, req)]
        keys = [k for k in section["keys"] if k in _DERIVED or _has(state, k)]
        budget = len(json.dumps({k: source.get(k) for k in keys}, ensure_ascii=False))
        low = max(MIN_CHARS, min(MAX_CHARS,
                                 CHARS_PER_POINT * len(points),
                                 int(budget * MAX_EXPANSION)))
        # 상한이 없으면 남는 분량을 다른 장의 내용이나 지어낸 서술로 채운다
        plan[section["id"]] = {"points": points, "keys": keys,
                               "min_chars": low, "max_chars": int(low * 1.8)}
    return plan



# ──────────────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────────────

_RULES: str | None = None


def _load_rules() -> str:
    """prompts/report_generation.md 를 읽어 모든 섹션 프롬프트 앞에 붙인다."""
    global _RULES
    if _RULES is None:
        _RULES = _PROMPT_PATH.read_text(encoding="utf-8")
    return _RULES


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z0-9]+|[가-힣]{2,}", text)}


def _pick(container: dict, tech: str, role: str) -> dict:
    """평가 결과에서 기술 하나의 행을 꺼낸다.

    키를 기술명("ITME")으로 쓸지 역할("hw")로 쓸지가 팀에서 확정되지 않아 둘 다 받는다.
    """
    return (container or {}).get(tech) or (container or {}).get(role) or {}


def _evidence_of(row: dict) -> list:
    """평가 행에 달린 출처 id. 키 이름이 evidence / evidence_ids 로 흔들려서 둘 다 본다."""
    return row.get("evidence") or row.get("evidence_ids") or []


def _format_reference(ref: dict) -> str:
    """출처 1건을 제출 규정 표기 형식으로 만든다. LLM 이 쓰면 형식이 흔들려 코드가 찍는다."""
    kind = ref.get("kind", "web")
    authors, title = ref.get("authors", "미상"), ref.get("title", "제목 미상")
    date, venue = str(ref.get("date", "n.d.")), ref.get("venue", "")
    locator, url = ref.get("locator", ""), ref.get("url", "")

    if kind == "patent":
        return f"{authors}({date}). {title}, {locator}, {url}"
    if kind == "paper":
        return f"{authors}({date[:4]}). {title}. {venue}, {locator}."
    return f"{authors}({date}). {title}. {venue}, {url}"


def _normalize_references(refs: list) -> tuple[dict, dict]:
    """중복 등록된 출처를 url 기준으로 합친다.

    기술 조사가 TECH-03 으로, 시장 평가가 MKT-07 로 같은 논문을 넣는 일이 반드시 생긴다.
    먼저 나온 id 를 대표로 삼아 (대표 id -> 출처), (모든 id -> 대표 id) 두 표를 만든다.
    id 가 없는 항목에는 임시 id 를 붙여 최소한 형식은 유지한다.
    """
    index, alias, by_url = {}, {}, {}
    for n, ref in enumerate(refs or [], 1):
        rid = ref.get("id") or f"AUTO-{n:02d}"
        key = (ref.get("url") or f"__{rid}").rstrip("/")
        if key not in by_url:
            by_url[key] = rid
            index[rid] = ref
        alias[rid] = by_url[key]
    return index, alias


def _score_table(state: dict) -> str:
    """관점별 평가 비교표. 들어온 관점만 행으로 만든다.

    LLM 에게 숫자를 쓰게 하면 반올림하거나 지어내므로 State 값을 그대로 찍는다.
    """
    tech = state.get("selected_technologies", {})
    sw, hw = tech.get("sw", "SW"), tech.get("hw", "HW")
    rows = [(label, key, field) for label, key, field in PERSPECTIVES if _has(state, key)]
    if not rows:
        return ""

    lines = [f"| 관점 | {sw} | {hw} |", "|---|---|---|"]
    for label, key, field in rows:
        cells = [str(_pick(state[key], name, role).get(field, "NOT_VERIFIED"))
                 for name, role in ((sw, "sw"), (hw, "hw"))]
        lines.append(f"| {label} | {cells[0]} | {cells[1]} |")
    return "\n".join(lines)


def _evidence_stats(state: dict, index: dict, alias: dict) -> dict:
    """기술별로 확보된 근거의 수와 유형.

    한계점 장에서 "SW 는 5건, HW 는 특허 1건뿐" 같은 정보 비대칭을 수치로 말하기 위한 근거다.
    이 값이 있어야 한계점을 지어내지 않고 쓸 수 있다.
    """
    tech = state.get("selected_technologies", {})
    stats = {}
    for role in ("sw", "hw"):
        name = tech.get(role)
        if not name:
            continue
        ids = set()
        for key in RESULT_KEYS:
            ids.update(alias.get(e, e) for e in _evidence_of(_pick(state.get(key, {}), name, role)))
        kinds: dict[str, int] = {}
        for e in ids:
            k = index.get(e, {}).get("kind", "미등록")
            kinds[k] = kinds.get(k, 0) + 1
        stats[name] = {"확보 근거 수": len(ids), "유형별": kinds}
    return stats


def _method_notes(state: dict) -> list[str]:
    """한계점 장에 쓸 수 있는 '실제로 취한 조치'.

    파이프라인이 실제로 한 일만 담는다. 예를 들어 평가 관점이 하나만 들어왔다면
    "독립 실행" 을 말할 수 없으므로 그 항목을 넣지 않는다.
    프롬프트에서 "여기 없는 조치는 쓰지 마라" 로 묶어, 하지도 않은 조치를 주장하는 것을 막는다.
    """
    notes = []
    done = [label for label, key, _ in PERSPECTIVES if _has(state, key)]
    if len(done) >= 2:
        notes.append(f"{' · '.join(done)} 평가는 서로의 결과를 참조하지 않고 병렬로 독립 실행되었다.")
        notes.append(f"두 기술에 동일한 평가 축({' / '.join(done)})과 동일한 지시문을 적용했다.")
        notes.append("관점별 비교표는 서술이 아니라 State 값에서 직접 렌더링해 수치 왜곡 가능성을 차단했다.")

    n = len((state.get("evaluation_result") or {}).get("disagreements", []))
    if n:
        notes.append(f"평가 종합 단계에서 나온 불일치 의견 {n}건을 축약하지 않고 시사점 장에 명시했다.")
    if state.get("references"):
        notes.append("본문의 모든 인용을 references 에 등록된 id 와 자동 대조했고, 미등록 인용은 반려했다.")
    notes.append("보고서 전 과정에서 기술의 우열이나 단일 승자를 결정하지 않았다.")
    return notes


def _section_prompt(section: dict, plan: dict, source: dict, extra: str = "") -> str:
    """살아남은 작성 항목과 그 항목이 쓸 자료만 담은 프롬프트를 만든다.

    필요한 State Key 는 SECTIONS 에 이미 적혀 있으므로 LLM 이 도구로 찾게 하지 않고
    처음부터 넣어 준다 (LLM 호출 1회로 끝난다).
    """
    context = {k: source.get(k) for k in plan["keys"]}
    points = "\n".join(f"- {p}" for p in plan["points"])
    return f"""{_load_rules()}

---

## 작성할 섹션

{section["id"]}. {section["title"]}

## 반드시 다룰 항목

아래 항목은 실제로 확보된 자료에 맞춰 추려진 것이다. 여기 없는 내용은 쓰지 않는다.

{points}

## 분량

한글 {plan["min_chars"]}자 이상 {plan["max_chars"]}자 이하. 항목마다 근거와 해석을 함께 쓴다.
위 항목에 해당하지 않는 내용으로 분량을 채우지 마라. 쓸 자료가 없으면 하한에 못 미쳐도 된다.

## 참고 자료

아래 JSON 안에 있는 내용만 사용한다.

```json
{json.dumps(context, ensure_ascii=False, indent=2)}
```
{extra}
장 제목 없이 본문만 마크다운으로 출력하라."""


def _llm():
    return init_chat_model(MODEL_NAME, temperature=0)


# ──────────────────────────────────────────────────────────────────────
# 노드
# ──────────────────────────────────────────────────────────────────────

def _prepare(state: _ReportState) -> dict:
    """섹션 작성 전 준비. LLM 을 쓰지 않는다.

    1. 중복 출처를 합쳐 ref_index / ref_alias 생성
    2. LLM 에게 맡기면 안 되는 값을 미리 계산 (score_table, evidence_stats, method_notes)
    3. 들어온 자료를 보고 장별 작성 계획(plan)을 세운다
    """
    index, alias = _normalize_references(state.get("references", []))
    source: dict[str, Any] = {
        k: state.get(k) for k in
        ["background_facts", "selected_technologies", "target_domain",
         *RESULT_KEYS, "evaluation_result"]
    }
    source["score_table"] = _score_table(state)
    source["evidence_stats"] = _evidence_stats(state, index, alias)
    source["method_notes"] = _method_notes(state)
    plan = _build_plan(state, source)

    raw = len(state.get("references") or [])
    print(f"[report] 출처 {raw}건 → {len(index)}건 (중복 제거)")
    if raw and not any(r.get("id") for r in state["references"]):
        print("[report] 경고: references 에 id 가 없다. 본문 인용을 출처와 연결할 수 없다.")
    for sid in BODY_IDS:
        p = plan[sid]
        if not p["points"]:
            print(f"[report] 계획 {HEADINGS[sid]}: 자료 없음 → 장 생략")
        else:
            print(f"[report] 계획 {HEADINGS[sid]}: 항목 {len(p['points'])}개, "
                  f"분량 {p['min_chars']}~{p['max_chars']}자")

    return {"source": source, "plan": plan, "ref_index": index, "ref_alias": alias,
            "sections": {}, "attempt": 0}


def _make_writer(section_id: str):
    """장 하나를 쓰는 노드를 만든다. 본문 장들이 이 팩토리로 만들어져 병렬 실행된다."""

    def write(state: _ReportState) -> dict:
        section, plan = SECTION_BY_ID[section_id], state["plan"][section_id]
        if not plan["points"]:
            # 쓸 자료가 하나도 없다. LLM 을 부르면 지어낼 뿐이므로 부르지 않는다.
            print(f"[report] {HEADINGS[section_id]} — 자료 미확보로 생략")
            return {"sections": {section_id: MISSING_NOTE}}

        draft = _llm().invoke(
            [HumanMessage(content=_section_prompt(section, plan, state["source"]))]
        ).content.strip()
        print(f"[report] {HEADINGS[section_id]} — {len(draft)}자")
        return {"sections": {section_id: draft}}

    write.__name__ = f"section_{section_id}"
    return write


def _body_text(sections: dict) -> str:
    return "\n\n".join(f"## {HEADINGS[sid]}\n\n{sections[sid]}"
                       for sid in BODY_IDS if sections.get(sid))


def _write_summary(state: _ReportState) -> dict:
    """SUMMARY. 개요가 아니라 결론 요약이라 본문이 다 나온 뒤에 쓴다."""
    draft = _llm().invoke([HumanMessage(content=f"""{_load_rules()}

---

아래는 이 보고서의 본문이다. 맨 앞에 놓일 SUMMARY 를 작성하라.

- {SUMMARY_MAX_CHARS}자 이내. 개요 장표가 아니라 평가 결론의 요약이다.
- 본문에 실제로 서술된 내용만 요약한다. 자료가 없어 생략된 장은 언급하지 않는다.
- 본문의 [ref:...] 인용 표기는 그대로 유지하라.
- 장 제목 없이 본문만 출력하라.

{_body_text(state["sections"])}""")]).content.strip()
    print(f"[report] SUMMARY — {len(draft)}자")
    return {"sections": {"summary": draft}}


def _validate(state: _ReportState) -> dict:
    """완성본을 기계적으로 검사한다. LLM 을 쓰지 않는다.

    자료가 없어 생략된 장은 분량·문체 검사를 건너뛴다. 검사 항목은 다음과 같다.

    1. 분량 미달 / 초과                        → 얇아지거나 다른 장을 침범하는 것을 막는다
    2. 존댓말 종결                             → 장별로 문체가 섞이는 것을 막는다
    3. 우열 판정 표현                          → 팀 완료 기준 위반
    4. references 에 없는 출처 인용            → 환각 인용
    4. SUMMARY 분량 초과
    5. 시사점 장이 disagreements 를 빠뜨림     → 엇갈림을 뭉개는 것을 막는다
    6. 관점별 평가의 점수가 score_table 과 불일치 → 수치 환각
    """
    issues: list[dict[str, str]] = []
    sections, index, alias = state["sections"], state["ref_index"], state["ref_alias"]
    plan = state["plan"]
    written = {sid: t for sid, t in sections.items() if t != MISSING_NOTE}

    for sid in BODY_IDS:
        if sections.get(sid) == MISSING_NOTE:
            continue
        text, goal = (sections.get(sid) or "").strip(), plan[sid]["min_chars"]
        if not text:
            issues.append({"section": sid, "problem": "본문이 비어 있다"})
        elif len(text) < goal * MIN_RATIO:
            issues.append({"section": sid, "problem":
                           f"분량이 {len(text)}자로 목표 {goal}자에 크게 못 미친다. "
                           f"항목마다 근거와 해석을 덧붙여 더 구체적으로 쓰라"})
        elif len(text) > plan[sid]["max_chars"]:
            issues.append({"section": sid, "problem":
                           f"분량이 {len(text)}자로 상한 {plan[sid]['max_chars']}자를 넘었다. "
                           f"이 장의 작성 항목에 해당하지 않는 서술을 덜어내라"})

    for sid, text in written.items():
        polite = len(HONORIFIC.findall(text))
        if polite:
            issues.append({"section": sid, "problem":
                           f"존댓말 종결이 {polite}곳 있다. 문체를 평서체 '~다' 로 통일하라"})

        ranked = RANKING.findall(text)
        if ranked:
            issues.append({"section": sid, "problem":
                           f"우열을 판정하는 표현이 있다: {', '.join(sorted(set(ranked)))}. "
                           f"조건에 따라 어느 쪽이 맞는지로 바꿔 쓰라"})

        unknown = sorted({c for c in CITE.findall(text) if alias.get(c, c) not in index})
        if unknown:
            issues.append({"section": sid, "problem":
                           f"references 에 없는 출처를 인용했다: {', '.join(unknown)}"})

    if len(sections.get("summary", "")) > SUMMARY_MAX_CHARS:
        issues.append({"section": "summary",
                       "problem": f"SUMMARY 가 {SUMMARY_MAX_CHARS}자를 넘었다"})

    disagreements = (state.get("evaluation_result") or {}).get("disagreements", [])
    if disagreements and "5" in written:
        body = _tokenize(written["5"])
        covered = sum(1 for d in disagreements
                      if len(_tokenize(d) & body) >= max(2, len(_tokenize(d)) // 3))
        if covered < len(disagreements):
            issues.append({"section": "5", "problem":
                           f"불일치 의견 {len(disagreements)}건 중 {covered}건만 반영됐다. "
                           f"누락된 항목을 모두 서술하라"})

    table = state["source"]["score_table"]
    if table and "4" in written:
        values = [c.strip() for row in table.splitlines()[2:] for c in row.split("|")[2:4]]
        missing = [v for v in values if v not in ("NOT_VERIFIED", "") and v not in written["4"]]
        if missing:
            issues.append({"section": "4", "problem":
                           f"비교표 값 {', '.join(missing)} 이 본문에 없다. "
                           f"참고 자료의 score_table 표를 그대로 옮겨라"})

    print(f"[report] 검증: 문제 {len(issues)}건")
    for i in issues:
        print(f"         [{i['section']}] {i['problem']}")
    return {"issues": issues, "attempt": state.get("attempt", 0) + 1}


def _route(state: _ReportState) -> Literal["revise", "render"]:
    """문제가 남았으면 revise, 아니면 render.

    MAX_REVISION 을 넘기면 문제를 안은 채 render 로 보낸다.
    보고서가 아예 안 나오는 것보다 낫고, 남은 문제는 로그에 찍혀 있다.
    """
    if not state["issues"]:
        return "render"
    if state["attempt"] >= MAX_REVISION:
        print("[report] 재작성 상한 도달 — 남은 문제를 안고 출력한다")
        return "render"
    return "revise"


def _revise(state: _ReportState) -> dict:
    """지적된 장만 다시 쓴다. 통과한 장은 그대로 둔다."""
    fixed: dict[str, str] = {}
    for issue in state["issues"]:
        sid = issue["section"]
        if sid == "summary":
            merged = {**state["sections"], **fixed}
            fixed.update(_write_summary({**state, "sections": merged})["sections"])
            continue
        prompt = _section_prompt(
            SECTION_BY_ID[sid], state["plan"][sid], state["source"],
            extra=f"\n## 재작성 사유\n\n{issue['problem']}\n\n"
                  f"## 이전 초안\n\n{state['sections'].get(sid, '')}\n",
        )
        fixed[sid] = _llm().invoke([HumanMessage(content=prompt)]).content.strip()
        print(f"[report] {HEADINGS[sid]} 재작성 — {len(fixed[sid])}자")
    return {"sections": fixed}


def _render(state: _ReportState) -> dict:
    """최종 마크다운 조립. LLM 을 쓰지 않는다.

    본문의 [ref:id] 를 등장 순서대로 [1], [2] ... 로 바꾸고, 그렇게 실제로 인용된
    출처만 REFERENCE 에 싣는다. "실제 사용한 출처만 포함한다" 가 자동으로 지켜진다.
    """
    sections, index, alias = state["sections"], state["ref_index"], state["ref_alias"]
    body = "\n\n".join(f"## {HEADINGS[sid]}\n\n{(sections.get(sid) or '').strip()}"
                       for sid in REPORT_ORDER)

    order: list[str] = []
    number: dict[str, int] = {}

    def renumber(match: re.Match) -> str:
        rid = alias.get(match.group(1), match.group(1))
        if rid not in number:
            if rid not in index:          # validate 가 놓친 경우의 방어선
                return ""
            order.append(rid)
            number[rid] = len(order)
        return f"[{number[rid]}]"

    body = CITE.sub(renumber, body)
    refs = "\n".join(f"[{i}] {_format_reference(index[rid])}"
                     for i, rid in enumerate(order, 1)) or "본문에서 인용한 자료가 없다."

    tech = state.get("selected_technologies", {})
    header = (f"# KV Cache 최적화 기술 다관점 평가 보고서\n\n"
              f"**대상 기술** SW: {tech.get('sw', '-')} / HW: {tech.get('hw', '-')}　·　"
              f"**적용 도메인** {state.get('target_domain', '-')}")

    print(f"[report] 렌더링: 인용 {len(order)}건 → REFERENCE {len(order)}건")
    return {"final_report": f"{header}\n\n{body}\n\n## {HEADINGS['ref']}\n\n{refs}\n"}


# ──────────────────────────────────────────────────────────────────────
# 그래프 / 진입점
# ──────────────────────────────────────────────────────────────────────

_GRAPH = None


def _build_graph():
    """보고서 내부 그래프. 여러 번 호출돼도 한 번만 컴파일한다."""
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH

    wf = StateGraph(_ReportState)
    wf.add_node("prepare", _prepare)
    for sid in BODY_IDS:
        wf.add_node(f"section_{sid}", _make_writer(sid))
    wf.add_node("summary", _write_summary)
    wf.add_node("validate", _validate)
    wf.add_node("revise", _revise)
    wf.add_node("render", _render)

    wf.add_edge(START, "prepare")
    for sid in BODY_IDS:
        wf.add_edge("prepare", f"section_{sid}")                    # 병렬 fan-out
    wf.add_edge([f"section_{sid}" for sid in BODY_IDS], "summary")  # 전부 끝나야 SUMMARY
    wf.add_edge("summary", "validate")
    wf.add_conditional_edges("validate", _route,
                             {"revise": "revise", "render": "render"})
    wf.add_edge("revise", "validate")
    wf.add_edge("render", END)

    _GRAPH = wf.compile()
    return _GRAPH


def report_generation_agent(state: EvaluationState) -> dict:
    """전체 State를 최종 다관점 평가 보고서로 구성한다."""
    result = _build_graph().invoke(dict(state))
    return {"final_report": result["final_report"]}


if __name__ == "__main__":
    # 다른 Agent 없이 이 Agent 만 단독 실행한다.
    #   python -m agents.report_generation [state.json] [out.md]
    import sys

    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=True)

    src = Path(sys.argv[1]) if len(sys.argv) > 1 else _ROOT / "data" / "sample_state.json"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else _ROOT / "outputs" / "final_report.md"

    report = report_generation_agent(json.loads(src.read_text(encoding="utf-8")))["final_report"]
    dst.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[report] 저장: {dst}")
