# -*- coding: utf-8 -*-
"""청킹 — 벤치마크와 프로덕션이 공유하는 단일 소스.

`experiments/retrieval_bench`에서 측정한 MRR 0.696은 이 청킹 기준이다.
여기를 고치면 측정값이 무효가 되므로, 변경 시 벤치마크를 재실행할 것.
"""
from __future__ import annotations

import re

TARGET_TOKENS = 900
CHARS_PER_TOKEN = 3.6
MAX_CHARS = int(TARGET_TOKENS * CHARS_PER_TOKEN)

# 표 행 판정: 독립 수치 토큰이 3개 이상인 줄.
# 목차의 점선(dot leader)과 인용 번호([31])는 제외한다 — 과검출 방지.
_NUM_TOKEN = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?:\s*(?:%|GB|MB|TB|KB|B|bit|ms|s|x|×))?(?![\w])")
_DOT_LEADER = re.compile(r"\.{4,}")
_CITATION = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")


def _is_table_row(line: str) -> bool:
    if line.lstrip().startswith("|"):          # 마크다운 표(보조 로더 대비)
        return True
    if _DOT_LEADER.search(line):               # 목차 줄
        return False
    stripped = _CITATION.sub("", line)         # 인용 번호 제거 후 판정
    tokens = _NUM_TOKEN.findall(stripped)
    return len(tokens) >= 3 and len(stripped.split()) >= 4


def blocks_of(text: str) -> list[str]:
    """빈 줄 기준 블록 분할. 연속된 표 행은 하나의 블록으로 묶는다."""
    out: list[str] = []
    buf: list[str] = []
    in_table = False
    for line in text.split("\n"):
        row = _is_table_row(line)
        if row and not in_table:
            if buf:
                out.append("\n".join(buf))
                buf = []
            in_table = True
        elif not row and in_table:
            out.append("\n".join(buf))
            buf = []
            in_table = False
        if line.strip() == "" and not in_table:
            if buf:
                out.append("\n".join(buf))
                buf = []
            continue
        buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return [b for b in out if b.strip()]


def chunk_page(text: str, tech_id: str, page: int) -> list[dict]:
    """한 페이지를 청크 리스트로. 표 블록은 통째로 유지한다."""
    chunks: list[dict] = []
    cur: list[str] = []

    def flush() -> None:
        if not cur:
            return
        body = "\n\n".join(cur).strip()
        if body:
            chunks.append(
                {
                    "tech_id": tech_id,
                    "page": page,
                    "text": body,
                    "has_table": any(_is_table_row(l) for l in body.split("\n")),
                }
            )

    for b in blocks_of(text):
        if cur and sum(len(x) for x in cur) + len(b) > MAX_CHARS:
            flush()
            # 직전 블록을 이월하여 문맥 단절 방지(표 블록은 이월하지 않음)
            cur = cur[-1:] if not _is_table_row(cur[-1].split("\n")[0]) else []
        cur.append(b)
    flush()
    return chunks
