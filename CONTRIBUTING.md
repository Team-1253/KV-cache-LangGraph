# KV Cache 다관점 평가 프로젝트

`수정-설계.md`의 LangGraph 흐름을 구현하기 위한 팀 공용 작업 폴더입니다.

## Directory Structure

```text
work/
├── data/                  # 원문 PDF와 검색 데이터
├── agents/                # Agent 및 State 모듈
├── prompts/               # Agent별 프롬프트
├── outputs/               # 평가 결과와 최종 보고서
├── app.py                 # LangGraph 구성 및 실행
└── README.md              # 협업 가이드
```

## 역할 분담

| 담당 | 역할 | 작업 파일 |
| --- | --- | --- |
| 황영준 | 기술 조사 Agent, TRL 평가 Node | `agents/technical_research.py`, `prompts/technical_research.md` |
| 김인성 | 시장 평가 Agent | `agents/market_evaluation.py`, `prompts/market_evaluation.md` |
| 이윤서 | 이해관계자 평가 Agent | `agents/stakeholder_evaluation.py`, `prompts/stakeholder_evaluation.md` |
| 김령아 | 도메인 평가 Agent | `agents/domain_evaluation.py`, `prompts/domain_evaluation.md` |
| 함형준 | 평가 종합 Agent | `agents/evaluation_synthesis.py`, `prompts/evaluation_synthesis.md` |
| 황정현 | 보고서 생성 Agent | `agents/report_generation.py`, `prompts/report_generation.md` |

## 협업 규칙

1. 각 담당자는 자신의 Agent 파일과 프롬프트 파일을 중심으로 작업합니다.
2. 공통 파일인 `agents/state.py`와 `app.py`를 변경할 때는 팀에 먼저 공유합니다.
3. Agent 함수는 전체 State가 아니라 자신이 생성한 State Key만 반환합니다.
4. 병렬 Agent는 서로 다른 결과 키를 사용합니다.
5. 모든 결과는 JSON으로 저장할 수 있는 `dict`, `list`, `str`, `int`, `float`로 구성합니다.
6. 원문에서 확인하지 못한 내용은 추측하지 않고 `NOT_VERIFIED`로 기록합니다.
7. 수치에는 출처, 기준 시점, 단위와 baseline을 함께 기록합니다.
8. API 키와 `.env` 파일은 커밋하지 않습니다.

## Agent 입출력 계약

| Node | 주요 입력 | 출력 State Key |
| --- | --- | --- |
| 기술 조사 | `selected_technologies` | `technical_result`, `references` |
| TRL 평가 | `technical_result` | `trl_result`, `references` |
| 시장 평가 | `technical_result` | `market_result`, `references` |
| 이해관계자 평가 | `technical_result` | `stakeholder_result`, `references` |
| 도메인 평가 | `technical_result`, `target_domain` | `domain_result`, `references` |
| 평가 종합 | 4개 평가 결과 | `evaluation_result` |
| 보고서 생성 | 전체 결과, `background_facts`, `references` | `final_report` |

`references`는 여러 조사·평가 Node가 함께 추가하므로 `agents/state.py`에서 reducer로 병합합니다.

## 통합 순서

```text
START
  -> 기술 조사
  -> [TRL / 시장 / 이해관계자 / 도메인 평가]
  -> 평가 종합
  -> 보고서 생성
  -> END
```

## 완료 기준

- 각 Agent가 자신에게 할당된 State Key만 반환한다.
- SW와 HW 기술 결과가 모두 포함된다.
- 평가 점수와 판단 근거가 함께 저장된다.
- 보고서가 `SUMMARY`로 시작하고 `REFERENCE`로 끝난다.
- 기술의 우열이나 단일 승자를 결정하지 않는다.


## 브랜치 명명 규칙

역할별 기능 브랜치는 `feature/<역할명>` 형식을 사용합니다.

| 담당 역할 | 브랜치명 |
| --- | --- |
| 기술 조사·TRL | `feature/technical-research` |
| 시장 평가 | `feature/market-evaluation` |
| 이해관계자 평가 | `feature/stakeholder-evaluation` |
| 도메인 평가 | `feature/domain-evaluation` |
| 평가 종합 | `feature/evaluation-synthesis` |
| 보고서 생성 | `feature/report-generation` |
| 공통 Tool 작업 | `feature/share-tools` |

작업 종류에 따라 다음 접두사를 사용합니다.

- 기능 개발: `feature/<작업명>`
- 버그 수정: `fix/<작업명>`
- 문서 수정: `docs/<작업명>`
- 리팩터링: `refactor/<작업명>`

브랜치명은 영문 소문자와 하이픈(`-`)만 사용하고, 한 브랜치에는 한 역할 또는 하나의 작업만 포함합니다.