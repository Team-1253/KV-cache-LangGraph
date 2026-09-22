# -*- coding: utf-8 -*-
"""골든 QA 25쌍 — 질의는 한국어(확정 정책), 원문은 영어+중국어.

gold: 정답 청크가 포함해야 하는 문자열 후보(하나라도 매칭되면 정답).
      로더마다 공백·하이픈·마크다운 기호가 달라지므로 compact() 정규화 후 비교한다.
trap: 코퍼스 내 동일 수치가 무관 맥락에 존재하는 함정 문항.
cjk : 정답이 중국어 구간에만 있는 교차언어 문항.
"""
import re

_KEEP = re.compile(r'[^a-z0-9.%一-鿿]')

def compact(s: str) -> str:
    """로더 간 표기 차이(하이픈·개행·마크다운 파이프)를 흡수하는 정규화."""
    return _KEEP.sub('', s.lower())

QA = [
 # ── ITME (HW) ─────────────────────────────────────────────
 dict(q="ITME가 DRAM 캐시 용량을 64GB가 아니라 32GB로 정한 이유는?", tech="itme",
      gold=["86.7% surge"], trap=True),
 dict(q="SSD 용량을 1TB에서 2TB로 늘리면 온칩 SRAM은 얼마나 증가하나?", tech="itme",
      gold=["6.7% increase"]),
 dict(q="CXL-하이브리드 메모리의 DRAM 캐시는 몇 way 세트 연관 구조인가?", tech="itme",
      gold=["16-way set"]),
 dict(q="ITME는 왜 쓰기 작업을 뒤로 미루는가?", tech="itme",
      gold=["read-priority I/O scheduling policy"]),
 dict(q="KV 캐시 미스가 발생하면 어떻게 처리하는가?", tech="itme",
      gold=["resolved through dynamic recomputation"]),
 dict(q="어떤 데이터를 원격 확장 계층에 두고 어떤 것을 GPU에 남기는가?", tech="itme",
      gold=["Long-context KV"]),
 dict(q="FPGA 프로토타입의 읽기와 쓰기 대역폭은 각각 얼마인가?", tech="itme",
      gold=["18 GB/s for reads"]),
 dict(q="CPU 오프로딩 대비 처리량이 몇 퍼센트 향상되었나?", tech="itme",
      gold=["35.7% throughput improvement over the CPU-offload"], trap=True),
 dict(q="evict된 KV 블록을 얼마나 큰 단위로 묶어서 전송하는가?", tech="itme",
      gold=["aggregates evicted KV cache blocks"]),
 dict(q="성능 평가에 사용한 GPU와 네트워크 구성은?", tech="itme",
      gold=["NVIDIA A100"]),
 dict(q="DRAM 4채널과 SSD 2채널 구성을 택한 이유는?", tech="itme",
      gold=["bandwidth mismatch"]),
 dict(q="프리페치 API 호출 한 번의 오버헤드는 얼마인가?", tech="itme",
      gold=["chm_prefetch"]),
 # ── DeepSeek-V2 (SW) ──────────────────────────────────────
 dict(q="MLA는 KV 캐시를 몇 퍼센트 줄이는가?", tech="dsv2",
      gold=["KV cache by 93.3"], trap=True),
 dict(q="MLA가 추론 시점에 실제로 캐시하는 것은 무엇인가?", tech="dsv2",
      gold=["Low-Rank Key-Value Joint Compression"]),
 dict(q="MHA, GQA, MQA, MLA의 토큰당 KV 캐시 크기를 비교하면?", tech="dsv2",
      gold=["KV Cache per Token"]),
 dict(q="KV 압축 차원을 나타내는 기호와 의미는?", tech="dsv2",
      gold=["KV compression dimension"]),
 dict(q="RoPE와 저차원 압축을 양립시키기 위해 도입한 방법은?", tech="dsv2",
      gold=["decoupled Rotary Position"]),
 dict(q="DeepSeek-V2의 전체 파라미터 수와 토큰당 활성 파라미터 수는?", tech="dsv2",
      gold=["236B total parameters"]),
 dict(q="학습 비용을 몇 퍼센트 절감했다고 보고하는가?", tech="dsv2",
      gold=["42.5% of training costs"]),
 dict(q="최대 생성 처리량이 몇 배 향상되었나?", tech="dsv2",
      gold=["5.76 times"]),
 dict(q="긴 컨텍스트로 확장할 때 사용한 기법은?", tech="dsv2",
      gold=["YaRN"]),
 dict(q="MLA와 MHA를 어려운 벤치마크에서 비교한 결과는?", tech="dsv2",
      gold=["MLA and MHA on hard benchmarks"]),
 dict(q="중국어 종합 능력 평가 결과에서 추론과 언어 점수는?", tech="dsv2",
      gold=["推理"], cjk=True),
 dict(q="DeepSeek-V2가 스스로 밝힌 한계는 무엇인가?", tech="dsv2",
      gold=["Conclusion, Limitation"]),
 dict(q="전문가 라우팅에서 통신 비용을 제한하기 위해 쓴 기법은?", tech="dsv2",
      gold=["device-limited routing"]),
]

def match(chunk_text: str, gold_list) -> bool:
    c = compact(chunk_text)
    return any(compact(g) in c for g in gold_list)
