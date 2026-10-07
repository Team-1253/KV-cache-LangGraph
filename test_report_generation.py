"""실제 API 통합테스트: uv run pytest test_report_generation.py -q"""

import json
from html import unescape
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from agents.synthesizer import synthesizer


def test_report_generation():
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    markdown = (root / "Untitled.md").read_text(encoding="utf-8")
    state = json.loads(unescape(markdown.split("<pre>", 1)[1].split("</pre>", 1)[0]))

    result = synthesizer({
        "run_id": str(uuid4()),
        "target_domain": state.get("target_domain", "데이터센터"),
        "background_facts": state.get("background_facts", {}),
        "results": [{"output": state}],
        "errors": state.get("run_errors", []),
    })
    assert result["status"] != "FAILED"
    report = Path(result["report_uri"]).read_text(encoding="utf-8")

    for heading in (
        "# 기술 다관점 평가 보고서",
        "## 요약",
        "## 4. 관점별 평가",
        "## 6. 한계점",
        "## REFERENCE",
    ):
        assert heading in report
    assert "[^1]:" in report
    for profile in state["technical_result"].values():
        assert profile["title"] in report
