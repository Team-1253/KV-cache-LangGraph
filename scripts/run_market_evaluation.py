"""시장 평가 노드 단독 실행 러너.

상류 에이전트(기술 조사 등)가 준비되기 전까지, fixture `TechProfile`로 시장 평가
노드를 단독 실행해 결과를 확인/저장한다.

사용 예:
    # 오프라인(fake)으로 배선만 확인
    uv run python scripts/run_market_evaluation.py --fake --techs deepseek_v2_mla --items 3-2-a

    # 실제 Tavily + gpt-5.6-luna 로 소량 검증
    uv run python scripts/run_market_evaluation.py --techs deepseek_v2_mla --items 3-2-a

    # 전체 실행 후 저장
    uv run python scripts/run_market_evaluation.py --out outputs/market_result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.market_evaluation import (  # noqa: E402
    EvidenceJudgement,
    MarketDeps,
    RubricScore,
    load_rubric,
    run_market_evaluation,
)
from agents.state import EvaluationState  # noqa: E402


def _source(chunk_id: str, page: int) -> dict:
    return {"chunk_id": chunk_id, "page": page}


def sample_state() -> EvaluationState:
    """`TECHNICAL_RESULT_SCHEMA.md`를 따르는 최소 fixture."""
    payload: dict[str, Any] = {
        "selected_technologies": {"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
        "target_domain": "데이터센터",
        "technical_result": {
            "deepseek_v2_mla": {
                "tech_id": "deepseek_v2_mla",
                "camp": "SW",
                "title": "DeepSeek-V2 MLA",
                "overview": "MLA compresses KV cache into a low-rank latent vector.",
                "mechanism": [
                    {"text": "joint low-rank compression of K/V", "source": _source("c1", 3)}
                ],
                "scope": [
                    {"text": "long-context LLM serving", "source": _source("c2", 2)}
                ],
                "claims": [
                    {
                        "text": "93.3% KV cache reduction",
                        "baseline": "MHA",
                        "source": _source("c3", 1),
                    }
                ],
                "measurements": [
                    {
                        "metric": "KV cache",
                        "value": "93.3%",
                        "baseline": "MHA",
                        "condition": "long context",
                        "source": _source("c3", 5),
                    }
                ],
                "limits_explicit": [],
                "limits_implicit": [
                    {
                        "text": "compute overhead",
                        "basis": "increased FLOPs",
                        "source": _source("c4", 7),
                    }
                ],
                "evidence_level": "strong",
                "retrieval": {"chunks_used": 4},
            },
            "itme": {
                "tech_id": "itme",
                "camp": "HW",
                "title": "ITME",
                "overview": "CXL-Hybrid memory expansion for KV cache offload.",
                "mechanism": [],
                "scope": [],
                "claims": [],
                "measurements": [
                    {
                        "metric": "throughput",
                        "value": "35.7%",
                        "baseline": "CPU-offload",
                        "condition": "expansion turn",
                        "source": _source("c9", 6),
                    }
                ],
                "limits_explicit": [],
                "limits_implicit": [],
                "evidence_level": "limited",
                "retrieval": {},
            },
        },
    }
    return cast(EvaluationState, payload)


def fake_deps() -> MarketDeps:
    def web_search(query, **kwargs):
        return [
            {
                "title": "fixture result",
                "url": "https://example.com/fixture",
                "content": f"fixture content for: {query}",
                "published_date": "2024-05",
            }
        ]

    def judge_evidence(system_prompt, criterion, technology, results):
        return EvidenceJudgement(evidence_score=4, reason="fixture")

    def score_rubric(system_prompt, criterion, technology, results, evidence_score):
        return RubricScore(score=4, rationale="fixture rationale")

    return MarketDeps(web_search, judge_evidence, score_rubric)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="시장 평가 노드 단독 실행")
    parser.add_argument("--input", type=Path, help="state JSON 파일 (기본: 내장 fixture)")
    parser.add_argument("--techs", help="포함할 tech_id (콤마 구분, 기본: 전체)")
    parser.add_argument("--items", help="포함할 criterion id (콤마 구분, 기본: 전체)")
    parser.add_argument("--out", type=Path, help="결과 저장 경로")
    parser.add_argument(
        "--fake", action="store_true", help="네트워크/LLM 없이 fake 의존성 사용"
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()

    state: EvaluationState = (
        json.loads(args.input.read_text(encoding="utf-8")) if args.input else sample_state()
    )

    if args.techs:
        wanted = {t.strip() for t in args.techs.split(",") if t.strip()}
        state["technical_result"] = {
            k: v
            for k, v in state.get("technical_result", {}).items()
            if k in wanted
        }

    criteria = load_rubric().get("criteria", [])
    if args.items:
        wanted_items = {i.strip() for i in args.items.split(",") if i.strip()}
        criteria = [c for c in criteria if c.get("id") in wanted_items]

    deps = fake_deps() if args.fake else None
    if deps is None:
        from agents.market_evaluation import default_deps

        deps = default_deps()

    result = run_market_evaluation(state, deps, criteria)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[saved] {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
