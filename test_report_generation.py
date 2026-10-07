"""실제 API 통합테스트: uv run pytest test_report_generation.py -q"""

import json
from html import unescape
from pathlib import Path

from dotenv import load_dotenv

from agents.report_generation import report_generation_agent


def test_report_generation():
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    markdown = (root / "Untitled.md").read_text(encoding="utf-8")
    state = json.loads(unescape(markdown.split("<pre>", 1)[1].split("</pre>", 1)[0]))

    report = report_generation_agent(state)["final_report"]

    output = root / "outputs" / "report_generation_test.md"
    output.parent.mkdir(exist_ok=True)
    output.write_text(report, encoding="utf-8")

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
