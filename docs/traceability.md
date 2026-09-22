# 시장 평가 Agent 코드 추적성(Traceability)

시장 평가 Agent(`agents/market_evaluation.py`)의 각 부분이 어떤 문서·근거에 기반해
구현되었는지 기록한다. 라인 번호는 작성 시점 기준이며, 코드 변경에 따라 달라질 수 있다.

## 1. 출처 문서

| 약칭 | 문서 | 위치 |
| --- | --- | --- |
| D1 | 시장성 관점 설계서 `kv-cache-market-eval-design.md` | 로컬 |
| D2 | 시장 루브릭 `data/3-2_market_evaluation.json` | develop |
| D3 | `agents/TECHNICAL_RESULT_SCHEMA.md` | `feature/technical-research` |
| D4 | 팀 설계 산출물 PDF `RAG-Design_...pdf` | 로컬 |
| D5 | `CONTRIBUTING.md` | 로컬 |
| D6 | `prompts/market_evaluation.md` | 로컬(D1·D2 기반 작성) |
| D7 | `.env.example` / `.env` | 로컬 |
| D8 | OpenAI GPT‑5.6 Luna 문서 | 웹 |
| D9 | Tavily Search API 문서 | 웹 |
| D10 | `agents/state.py`, `app.py` | 로컬 |
| U | 사용자(회의·대화) 확정 사항 | — |

## 2. 코드 추적표

| 코드 위치 | 구현 내용 | 주 출처 | 보조 |
| --- | --- | --- | --- |
| `1-13` 모듈 docstring | 단일 노드·자기 키만 반환·내부 상태 전이 | D5 규칙3 | D1 §6 |
| `28-30` 경로 상수 | 루브릭·프롬프트 로드 대상 | D2, D6 | — |
| `32-33` `MAX_ATTEMPTS=3`, `CONFIRM_THRESHOLD=3` | 검색 종료 조건 | D1 §5 | U(≥3 확정) |
| `35-38` `TAG_*` | 신뢰도 태그 4종 | D1 §5 | D2 `scoring.evidence_confidence_tags` |
| `44-48` `EvidenceJudgement` | 근거 품질(종료) 판정 | D1 §5·§6 | D6 태스크1 |
| `51-59` `EvidenceItem` | 출처·기준시점·단위·baseline | D5 규칙7 | D1 §7.3 |
| `61-66` `RubricScore` | 루브릭 점수·근거 | D1 §6 | D2 `criteria.scores` |
| `75-81` `MarketDeps` | 주입형 LLM·검색 의존성 | U(fake 우선) | — |
| `84-105` `ItemState` | 항목별 내부 상태 전이 | U(단일 노드 내부 전이) | D1 §6 |
| `108-115` 로더 | `load_rubric`/`load_system_prompt` | D2, D6 | — |
| `118-131` `_eval_as_of`/`_bool_env`/`_float_env` | env 설정 | D7 | D1 §7.9 |
| `134-145` `map_tag` | evidence_score → 태그 | D1 §5 | — |
| `148-213` `_measurement_terms`/`_text_terms`/`_tech_terms` | TechProfile에서 쿼리 시드 | D3 | D1 §6 step3, U(3-2-b는 measurements) |
| `187-221` `_TECH_ALIASES`/`_alias_terms` | 기술 alias | U(품질개선 B) | D4 기술명 |
| `224-245` `build_queries`/`_CRITERION_SEEDS` | 항목별 영문 시장 쿼리 | U(품질개선 B) | D1 §6 step2 |
| `249-274` `_MARKET_DOMAINS`/`_search_settings` | 재시도 advanced·도메인 prefer | D1 §7.7 | D9, U(재시도부터) |
| `276-288` `_dedupe_results` | 결과 중복 제거 | 구현 세부 | — |
| `291-395` 내부 서브그래프 노드·엣지 | `query→search→judge→{retry,score}` | D1 §6 | U(서브그래프) |
| `398` `_format_results` | 검색 결과 포맷 | D1 §6 | — |
| `413-460` `_sources*`/`_evidence_from_items` | 규칙7 evidence 매핑·폴백 | D5 규칙7 | D1 §7.2·§7.3 |
| `462-557` 도메인 표 + `_source_type` | source_type 분류 7종 | D1 §5 | D9, U(analyst 추가) |
| `560-588` `_to_references`/`_dedupe_references` | references 정규화 | D1 §7.2 | D5 규칙7 |
| `591` `_total_score` | `합계÷20×100` | D2 `scoring.total_score_formula` | D1 §4 |
| `600` `_overall_rationale` | 기술 종합 요약 | D1 §6 step7 | — |
| `609` `_resolve_technologies` | `tech_id`·`camp` 키 정규화 | D3 | U(tech_id 통일) |
| `645-706` `run_market_evaluation` | 기술 순회·조립·자기 키 반환 | D5 규칙3 | D1 §6 step8, D10 |
| `708` `market_evaluation_agent` | LangGraph 노드 | D10 `app.py` | D5 |
| `717` `_chat_model` | 모델·effort·Responses·구조화 | D8 | D7 |
| `739-745` `_tavily_client`/`default_web_search` | 검색 파라미터·도메인 prefer | D9 | D1 §7.7, D7 |
| `775-793` `default_judge_evidence` | 근거 판정 LLM | D6 태스크1 | D1 §5 |
| `794-824` `default_score_rubric` | 채점 LLM + evidence 구조화 | D6 태스크2 | D5 규칙7 |
| `825` `default_deps` | 기본 의존성 묶음 | U | — |

## 3. 핵심 근거 요약

- **루브릭·배점·태그·항목 id**(`3-2-a`~`3-2-d`) → D2, D1 §4
- **검색 종료 규칙**(≥3 확정 / ≤2 재검색 / 최대 3회) → D1 §5, U
- **상태 전이(서브그래프) 설계** → D1 §6, U 확정
- **규칙 7 evidence 구조화**(출처·기준 시점·단위·baseline) → D5 규칙7, D1 §7.3
- **TechProfile 소비·tech_id 통일** → D3, U
- **LLM 사용법**(gpt-5.6-luna, effort low, Responses API) → D8
- **검색 파라미터**(Tavily) → D9
- **설정값**(모델·검색·기준일) → D7
- **소비 계약**(`market_result`, `references`만 반환) → D5 규칙3, D10

## 4. 참고

- D3는 원격 `feature/technical-research` 브랜치에 있으며, `technical_result`의
  `tech_id`·`TechProfile` 스키마를 정의한다.
- D4(PDF)와 D8·D9(웹 문서)는 코드 저장소 외부 자료다.
