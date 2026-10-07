# 관점별 결과와 출처 계약

`trl_result`, `market_result`, `stakeholder_result`, `domain_result`는 모두
`tech_id`를 키로 한 dict다. 아래 계약은 Worker가 반환하는 데이터 형식이다.
각 평가 함수는 자기 결과와 `references`를 반환하며, 실행 오류는 `run_errors`에 남긴다.
부모 State의 `results[].output`에 이 결과 dict를 누적한다.

## 공통 평가 결과

| 필드 | 내용 |
| --- | --- |
| `tech_id`, `technology`, `perspective` | 기술 ID, 표시명, 관점(`trl`, `market`, `stakeholder`, `domain`) |
| `status` | `VERIFIED`, `PARTIAL`, `NOT_VERIFIED` |
| `score`, `score_scale` | 관점 고유의 점수와 척도. TRL은 `[하한, 상한]` 또는 `null` |
| `coverage` | 확인한 근거 비율, **모든 관점에서 0~1** |
| `summary`, `verdict` | 평가 요약과 판정. 별도 판정이 없는 관점은 빈 문자열 |
| `criteria` | 아래 공통 항목의 목록 |
| `metadata` | 루브릭, 기준일 등 관점에 필요한 부가 정보 |

`criteria`의 필드는 `id`, `name`, `status`, `score`, `rationale`, `evidence`, `metadata`다.
확인한 항목은 `VERIFIED`, 확인하지 못한 항목은 `NOT_VERIFIED`와 `score=null`이다.
미확인 항목은 합계 계산에서 0점 기여로 처리하며, 부정적인 평가와 구분한다.

`evidence`는 `reference_id`와 판단을 뒷받침하는 `text`를 갖는다.
수치 근거에는 `value`, `unit`, `baseline`, `condition`을 함께 남긴다.
시장 항목의 신뢰도·검색 횟수, 도메인 항목의 가중치, TRL의 핵심 구성요소 여부는
항목의 `metadata`에 둔다.

## 척도와 coverage

| 관점 | 점수 | coverage 기준 |
| --- | --- | --- |
| TRL | 핵심 구성요소 최저 단계~전체 구성요소 최고 단계, 1~9 | 출처를 확인한 구성요소 수 / 전체 구성요소 수 |
| 시장성 | 항목 합계 / (항목 수 × 5) × 100 | 확인한 항목 수 / 전체 항목 수 |
| 이해관계자 | 항목 합계 / (항목 수 × 5) × 100 | 확인한 항목 수 / 전체 항목 수 |
| 도메인 | 항목 점수 × 가중치의 합, 0~5 | 확인한 항목의 가중치 합 |

TRL의 핵심 구성요소 출처를 확인하지 못하면 구간을 보류한다.
도메인은 coverage가 루브릭 기준에 미달하면 점수와 coverage를 기록하되 최종 판정을 보류한다.
관점 사이의 점수를 합산하거나 하나의 승자를 정하지 않는다.

## 공통 출처

`references`의 필드는 `id`, `tech_id`, `perspective`, `title`, `url`, `date`, `page`,
`content`, `metadata`다. 웹 출처의 `page`는 `null`, 발표일을 모르면 `date`는 빈 문자열이다.
`content`에는 실제 검색 결과 또는 PDF 청크의 내용을 보존한다.
`metadata`에는 문서 종류와 청크 ID, 이해관계자 출처의 개발 주체/독립 출처 구분을 둔다.

근거와 출처는 `evidence.reference_id == references.id`로 연결된다.
시장 평가는 모델이 실제로 사용한 검색 결과 번호만 채택하고, 이해관계자 평가는 검색 결과에
존재하는 URL만 채택하고 근거 텍스트에는 해당 검색 원문을 직접 넣는다. 도메인 인용은 청크 ID와 원문 문자열을 대조한다.
TRL은 기술 조사에서 이미 확인한 출처 ID를 재사용한다.

## 취합과 보고서

`agents/synthesizer.py`의 `evaluation_material()`은 부모의 `results`를 읽어
네 관점을 같은 평가 목록으로 모으고, 평가와 기술 조사에서 실제로 연결된 출처만 전달한다.

Synthesizer는 모델에 한 번 요청해 관점 간 종합 판단과 요약·배경·선정·기술 개요·시사점·한계를 작성한다.
관점별 점수·coverage·판정·항목 근거와 측정값의 baseline·조건은 Python이 원자료 그대로 싣는다.
문단과 항목에 실제 출처 ID의 각주를 붙이고, **본문에서 사용한 출처만** REFERENCE에 싣는다.

노드 실패는 공통 실행 경계에서 기록하며 다음 단계로 진행한다.
보고서 모델 호출 전체가 실패하면 확보한 원자료와 오류를 담은 대체 보고서를 저장한다.
Synthesizer는 보고서 파일 경로를 `report_uri`로 반환한다.
이전처럼 장마다 모델을 따로 호출하거나 개별 장을 재시도하지 않는다.
