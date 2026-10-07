# `technical_result` / `trl_result` 출력 스키마

> 기술 조사 Agent(황영준)의 출력 계약. **하류 4개 평가 Node가 공통으로 소비**하므로,
> 각자 구현 전에 이 문서를 기준으로 접근하시면 통합 시 깨지지 않습니다.

## 1. `technical_result` — 기술 조사 Agent 출력

```python
technical_result: dict[tech_id, TechProfile]
# tech_id: "deepseek_v2_mla" | "itme"
```

### TechProfile

| 키 | 타입 | 내용 |
| --- | --- | --- |
| `tech_id` | `str` | `"deepseek_v2_mla"` \| `"itme"` |
| `camp` | `str` | `"SW"` \| `"HW"` |
| `title` | `str` | 표시용 기술명 |
| `overview` | `str` | 핵심 접근 방향 3문장 이내. 근거 없으면 `"NOT_VERIFIED"` |
| `mechanism` | `list[Evidenced]` | 작동 방식 |
| `scope` | `list[Evidenced]` | 적용 전제·범위 (평가 HW, 대상 모델, 워크로드) |
| `claims` | `list[Claim]` | **논문이 주장하는** 효과 |
| `measurements` | `list[Measurement]` | **실험에서 측정된** 수치 |
| `limits_explicit` | `list[Limit]` | 논문이 스스로 밝힌 한계 |
| `limits_implicit` | `list[Limit]` | **평가 조건에서 역산한** 한계 |
| `evidence_level` | `str` | `"strong"` \| `"limited"` \| `"NOT_VERIFIED"` |
| `retrieval` | `dict` | 검색 통계 (아래) |

### 항목 타입

```python
Source      = {"chunk_id": str, "page": int, "reference_id": str}
Evidenced   = {"text": str, "source": Source}
Claim       = {"text": str, "baseline": str, "source": Source}
Measurement = {"metric": str, "value": str, "baseline": str,
               "condition": str, "source": Source}
Limit       = {"text": str, "basis": str, "source": Source}
```

### ⚠️ 소비 시 반드시 지킬 것

**① `claims`와 `measurements`를 섞지 마세요.**
- `claims` = Abstract·Introduction의 요약 주장
- `measurements` = 실험 절(Evaluation)의 측정치

ITME를 예로 들면, Abstract의 `1.80×`는 **NVMe-oF 대비**이고 §6.1의 `1.81×`는 **재계산 대비**입니다.
구분하지 않고 인용하면 보고서가 틀립니다.

**② 수치를 인용할 때 `baseline`과 `condition`을 반드시 함께 쓰세요.**
`measurements`의 모든 항목은 두 필드가 **실질적으로 채워진 것만** 통과합니다
(`-`·`N/A` 등 자리표시자는 파이프라인에서 폐기됩니다). 따라서 값만 떼어 쓰면 안 됩니다.

```
❌ "ITME는 처리량이 35.7% 향상된다"
✅ "ITME는 CPU-offload 대비, 호스트 메모리 한계를 초과한 확장 턴 구간에서 처리량 35.7% 향상"
```

**③ `limits_implicit`는 논문에 없는 내용입니다.**
평가 조건에서 역산한 것이므로 `basis`(역산 근거)를 함께 제시해야 합니다.
벤더 논문은 명시적 한계를 잘 쓰지 않아 `limits_explicit`가 0건일 수 있습니다
(ITME가 실제로 그렇습니다). **0건은 "한계가 없다"가 아니라 "논문이 밝히지 않았다"입니다.**

**④ 모든 항목에 `source`가 붙어 있습니다.**
근거 없는 항목은 파이프라인에서 폐기되므로, 살아남은 항목은 전부 추적 가능합니다. `reference_id`로 공통 출처 목록의 원문 청크에 연결됩니다.
평가 결과에 근거를 달 때 `chunk_id`/`page`를 그대로 인용하시면 됩니다.

### `retrieval` 통계

```python
{"chunks_used": int,          # 근거로 사용한 청크 수
 "pages": list[int],          # 근거가 걸친 페이지
 "counts": dict[str, int],    # 항목별 추출 건수
 "dropped_ungrounded": dict}  # 근거 불일치로 폐기한 건수(환각 차단 증빙)
```

---

## 2. `trl_result` — TRL 평가 Node 출력

TRL도 다른 세 관점과 같은 `PerspectiveResult`를 반환한다.
공통 필드와 계산 기준은 [EVALUATION_RESULT_SCHEMA.md](EVALUATION_RESULT_SCHEMA.md)를 따른다.

- `score`: `[하한, 상한]` 또는 근거 부족 시 `null`. `score_scale`: `"1-9"`.
- `criteria`: 구성요소별 단계·판정 이유·출처를 공통 항목으로 기록한다.
- `criteria[].metadata.is_critical`: 해당 구성요소가 핵심 경로인지 표시한다.
- `metadata`: 문헌 발표일, 기준일, 경과 개월, `paper_only`, 구간 산출 규칙을 기록한다.
- `evidence.reference_id`: 기술 조사에서 확인한 출처 ID를 재사용한다.

하한은 출처를 확인한 핵심 구성요소의 최저 단계, 상한은 출처를 확인한 전체 구성요소의 최고 단계다.
핵심 구성요소의 출처를 확인하지 못하면 구간을 보류한다.

TRL 비교 시 발표 시점과 경과 개월을 함께 읽어야 한다. TRL은 공개 논문 근거 기반 추정이며,
실제 채택 근거는 시장성 관점에서 별도로 다룬다. 기술 주장은 TRL 판정 입력에서 제외하고,
측정치·범위·한계를 원래 출처와 함께 전달한다.
