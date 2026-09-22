"""형식이 달라지거나 단계가 실패해도 최종 문서를 반환하는지 확인한다."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import build_graph
from agents import evaluation_synthesis as synthesis
from agents import report_generation as report
from agents.resilient import continuing_node


class FlexibleSynthesisTests(unittest.TestCase):
    def test_prose_with_numeric_citation_is_not_mistaken_for_json(self):
        text = "논문 [1]은 메모리 감소를 보고했다. 추가 검증이 필요하다."
        self.assertEqual(synthesis._read_result(text), text)

    def test_unknown_fields_and_unstructured_response_are_preserved(self):
        model = Mock()
        model.invoke.return_value = SimpleNamespace(content="서로 다른 조건에서 평가되어 직접 비교할 수 없다.")
        state = {"낯선_평가": [{"기술": "실험기술", "결론": "추가 검증 필요"}]}
        with patch.object(synthesis, "init_chat_model", return_value=model):
            result = synthesis.evaluation_synthesis_agent(state)
        self.assertEqual(result["evaluation_result"], model.invoke.return_value.content)
        self.assertIn("낯선_평가", model.invoke.call_args.args[0][1].content)
        self.assertEqual(model.invoke.call_count, 1)

    def test_arbitrary_json_keys_and_block_content_are_accepted(self):
        data = {"새로운_관찰": {"내용": ["A", "B"], "출처": "https://example.test"}}
        model = Mock()
        model.invoke.return_value = SimpleNamespace(content=[{
            "type": "text", "text": "결과:\n```json\n" + json.dumps(data) + "\n```",
        }])
        with patch.object(synthesis, "init_chat_model", return_value=model):
            self.assertEqual(synthesis.evaluation_synthesis_agent({})["evaluation_result"], data)

    def test_api_failure_or_empty_response_produces_incomplete_result(self):
        for response in (RuntimeError("service unavailable"), SimpleNamespace(content="")):
            model = Mock()
            if isinstance(response, Exception):
                model.invoke.side_effect = response
            else:
                model.invoke.return_value = response
            with self.subTest(response=type(response).__name__):
                with patch.object(synthesis, "init_chat_model", return_value=model):
                    result = synthesis.evaluation_synthesis_agent({})
                self.assertEqual(result["evaluation_result"]["status"], "INCOMPLETE")
                self.assertEqual(len(result["run_errors"]), 1)


class FlexibleReportTests(unittest.TestCase):
    def test_each_section_reads_original_data_without_field_mapping(self):
        state = {
            "selected_technologies": ["이름 A", {"이름": "B"}],
            "technical_result": ["자유 서술문"],
            "evaluation_result": "자유로운 종합 결과",
            "references": {"논문": {"링크": "https://example.test/paper"}},
            "extra_field": {"새로운 근거": "고유한 내용"},
        }
        with patch.object(report, "_generate", return_value="확보된 평가 내용이다.") as generate:
            result = report.report_generation_agent(state)
        self.assertIn("## SUMMARY", result["final_report"])
        self.assertIn("## REFERENCE", result["final_report"])
        self.assertEqual(result["run_errors"], [])
        section_calls = [c for c in generate.call_args_list if "작성할 장:" in c.args[1]]
        self.assertEqual(len(section_calls), len(report.SECTIONS))
        for call in section_calls:
            self.assertIn("고유한 내용", call.args[1])
            self.assertIn("자유로운 종합 결과", call.args[1])

    def test_one_section_failure_preserves_successful_sections_and_original_data(self):
        def generate(rules, request):
            if "작성할 장: 4. 관점별 평가" in request:
                raise TimeoutError()
            return "성공한 장의 본문이다."
        with patch.object(report, "_generate", side_effect=generate):
            result = report.report_generation_agent({"technical_result": {"원자료": "남겨야 할 내용"}})
        self.assertEqual(len(result["run_errors"]), 1)
        self.assertIn("성공한 장의 본문이다.", result["final_report"])
        self.assertIn("남겨야 할 내용", result["final_report"])
        self.assertIn("부록", result["final_report"])

    def test_all_generation_calls_fail_but_report_contains_input(self):
        with patch.object(report, "_generate", side_effect=ConnectionError()):
            result = report.report_generation_agent({"market_result": "기존 평가 결과"})
        self.assertIn("기존 평가 결과", result["final_report"])
        self.assertEqual(len(result["run_errors"]), len(report.SECTIONS) + 1)

    def test_missing_prompt_still_returns_document(self):
        with patch.object(report, "_PROMPT_PATH") as path:
            path.read_text.side_effect = FileNotFoundError()
            result = report.report_generation_agent({"evaluation_result": "보존할 관찰"})
        self.assertIn("보존할 관찰", result["final_report"])
        self.assertEqual(result["run_errors"][0]["error_type"], "FileNotFoundError")


class GraphCompletionTests(unittest.TestCase):
    def test_every_node_import_fails_but_graph_reaches_final_report(self):
        with patch("agents.resilient.importlib.import_module", side_effect=ImportError()):
            result = build_graph().invoke({"selected_technologies": {"sw": "A"}, "references": []})
        self.assertTrue(result["final_report"])
        self.assertEqual(len(result["run_errors"]), 7)
        self.assertIn("ImportError", result["final_report"])

    def test_upstream_exceptions_and_llm_outage_still_complete_real_downstream_nodes(self):
        failure = Mock(side_effect=ValueError("unexpected upstream shape"))
        modules = {
            "agents.evaluation_synthesis": synthesis,
            "agents.report_generation": report,
        }
        failed_module = SimpleNamespace(**{name: failure for name in (
            "technical_research_agent", "trl_evaluation_node", "market_evaluation_agent",
            "stakeholder_evaluation_agent", "domain_evaluation_agent",
        )})
        with patch("agents.resilient.importlib.import_module", side_effect=lambda name: modules.get(name, failed_module)):
            with patch.object(synthesis, "init_chat_model", side_effect=ConnectionError()):
                with patch.object(report, "_generate", side_effect=ConnectionError()):
                    result = build_graph().invoke({"references": []})
        self.assertTrue(result["final_report"])
        self.assertEqual(len(result["run_errors"]), 5 + 1 + len(report.SECTIONS) + 1)
        self.assertIn("FAILED", result["final_report"])

    def test_reference_container_is_normalized_for_graph_reducer(self):
        module = SimpleNamespace(run=lambda state: {"market_result": "결과", "references": {"url": "url"}})
        with patch("agents.resilient.importlib.import_module", return_value=module):
            result = continuing_node("module", "run", "market_result")({})
        self.assertEqual(result["references"], [{"url": "url"}])

    def test_user_interrupt_is_not_swallowed(self):
        with patch("agents.resilient.importlib.import_module", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                continuing_node("module", "run", "market_result")({})


if __name__ == "__main__":
    unittest.main()
