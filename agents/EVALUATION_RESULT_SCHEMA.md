# `evaluation_result` 출력 스키마

> 평가 종합 Agent의 출력 계약. 보고서 생성 Agent가 `evaluation_result`를 소비한다.
> 이 문서는 현재 `EvaluationState`, 평가 종합 노드 및 프롬프트의 계약을 설명한다.

## 1. State 출력

```python
evaluation_result: EvaluationResult

EvaluationResult = {
    "agreements": list[str],
    "disagreements": list[str],
    "tradeoffs": list[str],
    "implications": list[str],
}
```

| 키 | 타입 | 내용 |
| --- | --- | --- |
| `agreements` | `list[str]` | 같은 기술에 대해 둘 이상 관점이 같은 결론을 뒷받침하는 관찰 |
| `disagreements` | `list[str]` | 같은 기술의 같은 의사결정 관련 사실에 대해 관점별 결론이 상반되는 관찰 |
| `tradeoffs` | `list[str]` | 한 세부 항목의 이득이 다른 항목의 비용·제약·성능 저하와 명시적으로 교환되는 관찰 |
| `implications` | `list[str]` | 종합 시사점 또는 `판단 보류/근거 공백` |

네 키는 모두 있어야 한다. 관계나 시사점이 확인되지 않은 목록은 빈 배열(`[]`)로 둔다.
평가 종합 노드는 다른 State 키를 갱신하지 않고 `{"evaluation_result": EvaluationResult}`만 반환한다.

## 2. 목록 원소 형식

각 원소는 **한국어 문자열 하나**다. 기술명은 `selected_technologies`의 표시용 이름을 사용한다.
상류의 `technical_result`·`trl_result`가 사용하는 `tech_id`를 이 결과의 최상위 키로
추가하지 않는다.

```text
agreements/disagreements:
기술명 | 관점: 관련 관점들 | 사실: 관찰과 판단의 관계 | 근거: 사용한 근거

tradeoffs:
기술명 | 관점: 관련 관점들 | 이득/비용: 명시된 이득과 비용·제약 | 근거: 사용한 근거

implications — 시사점:
기술명 | 관점: 관련 관점들 | 시사점: 종합 해석 | 근거: 사용한 근거

implications — 판단 보류:
기술명 | 관점: 관련 관점들 | 판단 보류/근거 공백: 보류 사유 | 근거: 확인 가능한 근거 또는 출처 연결 미확인
```

`관점:`에는 기술 성숙도(TRL), 시장, 이해관계자, 도메인 중 실제 판단에 사용한 관점을
적는다. `agreements`와 `disagreements`에는 관계를 맺는 둘 이상의 관점이 필요하다.

## 3. 분류와 판단 보류

| 상황 | 기록할 목록 |
| --- | --- |
| 같은 기술에 관한 세부 판단이 같은 결론을 지지함 | `agreements` |
| 같은 기술의 같은 사실에 관해 세부 판단이 상반됨 | `disagreements` |
| 근거에 명시된 이득과 비용·제약이 교환 관계임 | `tradeoffs` |
| 종합으로 얻은 조건부 해석 | `implications`의 `시사점:` |
| `NOT_VERIFIED`, 근거 부족, 평가 누락, 도메인 coverage 부족 등으로 결론을 보류함 | `implications`의 `판단 보류/근거 공백:` |

한 관찰을 여러 목록에 중복하지 않는다. 미확인이나 근거 부족 자체를 관점 간
`disagreements`로 기록하지 않는다. 도메인 `coverage`가 0.6 미만이거나 값이 없으면
적합성 판정을 보류한다. 없는 coverage 값을 임의로 산정하지 않는다. baseline·워크로드·측정 대상·
단위·기준 시점이 다른 수치를 같은 조건처럼 비교하지 않는다.

관점별 총점을 합산하거나 기술의 순위·승자·추천을 선언하지 않는다. TRL은
`공개 정보 기반 추정`으로 취급하고, 구성요소와 전체 시스템의 성숙도를 구분한다.

## 4. 근거와 출처

- 새 판단의 근거는 `trl_result`, `market_result`, `stakeholder_result`, `domain_result`에
  명시된 사실·조건·제약으로 한정한다. `technical_result`는 선행 평가의 입력이며,
  기존 `evaluation_result`나 `final_report`로 새 결론을 만들지 않는다.
- 판단에 사용한 `evidence` 문자열과 출처 식별자는 `근거:`에 보존한다. `references`는
  기존 출처와의 연결 확인에만 읽기 전용으로 사용한다. 연결을 확인할 수 없으면
  `출처 연결 미확인`이라고 적고, 새로운 출처 ID를 만들지 않는다.
- 같은 자료를 두 관점이 인용해도 관점의 일치는 기록할 수 있다. 그 사실만으로
  독립 검증을 받았다고 표현하지 않는다.
- `NOT_VERIFIED`와 `N/A`, 근거 등급 및 원래 평가의 불확실성 표시는 구분해서 유지한다.
  근거가 없는 판단 보류 항목에 출처가 있는 것처럼 쓰지 않는다.

선행 평가 Agent의 세부 중첩 키와 `references`의 출처 ID 형식은 현재 공통 State에서
고정하지 않았다. 이 문서는 `chunk_id`·`page`나 새 `source_id` 필드를
`evaluation_result`에 요구하지 않는다.

## 5. 형식 예시

아래는 **출력 형태만 보여주는 예시**이며 실제 기술 평가 결과가 아니다.

```json
{
  "agreements": [
    "<기술명> | 관점: 시장, 이해관계자 | 사실: <같은 결론을 지지하는 관찰> | 근거: <입력 evidence의 식별자와 내용>"
  ],
  "disagreements": [],
  "tradeoffs": [
    "<기술명> | 관점: 도메인, 이해관계자 | 이득/비용: <근거에 명시된 이득과 제약> | 근거: <입력 evidence의 식별자와 내용>"
  ],
  "implications": [
    "<기술명> | 관점: 도메인 | 판단 보류/근거 공백: coverage가 0.6 미만이라 적합성 판정 보류 | 근거: 출처 연결 미확인"
  ]
}
```

## 6. 검증 범위와 소비 방법

현재 `agents/evaluation_synthesis.py`는 LLM 응답을 JSON으로 파싱하고, 최상위 객체가
**정확히 네 키**를 가지며 각 값이 **문자열 목록**인지 검사한다. JSON 형식이 잘못되면
예외를 내고 결과를 State에 저장하지 않는다. 문장별 사실 관계, 판단 보류 사유 및
출처 연결의 의미상 정확성은 프롬프트 규칙에 따른 생성 결과를 검토해야 한다.

보고서 생성 Agent는 네 목록을 종합 평가 및 시사점 작성에 사용한다. 출처 목록은
`evaluation_result` 안에서 새로 만들지 않고 별도의 `references` State 키를 읽는다.
