"""Integrated LangGraph scaffold for the six team roles."""

from langgraph.graph import END, START, StateGraph
from dotenv import load_dotenv
from pathlib import Path

from agents.evaluation_synthesis import evaluation_synthesis_agent
from agents.market_evaluation import market_evaluation_agent
from agents.resilient import continuing_node
from agents.report_generation import report_generation_agent
from agents.report_quality import report_quality_agent, route_report_quality
from agents.state import EvaluationState
from agents.stakeholder_evaluation import stakeholder_evaluation_agent
from agents.technical_research import technical_research_agent, trl_evaluation_node
from agents.domain_evaluation import domain_evaluation_agent


def build_graph():
    builder = StateGraph(EvaluationState)

    builder.add_node(
        "technical_research",
        continuing_node(technical_research_agent, "technical_result"),
    )
    builder.add_node("trl_evaluation", continuing_node(trl_evaluation_node, "trl_result"))
    builder.add_node("market_evaluation", continuing_node(market_evaluation_agent, "market_result"))
    builder.add_node(
        "stakeholder_evaluation",
        continuing_node(stakeholder_evaluation_agent, "stakeholder_result"),
    )
    builder.add_node("domain_evaluation", continuing_node(domain_evaluation_agent, "domain_result"))
    builder.add_node(
        "evaluation_synthesis",
        continuing_node(evaluation_synthesis_agent, "evaluation_result"),
    )
    builder.add_node("report_generation", continuing_node(report_generation_agent, "final_report"))
    builder.add_node("report_quality", continuing_node(report_quality_agent, "report_quality"))

    builder.add_edge(START, "technical_research")
    builder.add_edge("technical_research", "trl_evaluation")
    builder.add_edge("technical_research", "market_evaluation")
    builder.add_edge("technical_research", "stakeholder_evaluation")
    builder.add_edge("technical_research", "domain_evaluation")

    builder.add_edge(
        [
            "trl_evaluation",
            "market_evaluation",
            "stakeholder_evaluation",
            "domain_evaluation",
        ],
        "evaluation_synthesis",
    )

    builder.add_edge("evaluation_synthesis", "report_generation")
    builder.add_edge("report_generation", "report_quality")
    builder.add_conditional_edges(
        "report_quality", route_report_quality,
        {"retry": "report_generation", "end": END},
    )

    return builder.compile()


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent / ".env")
    graph = build_graph()
    print(graph.get_graph().draw_mermaid())

    result = graph.invoke({
        "selected_technologies": {"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
        "target_domain": "데이터센터",
        "references": [],
    })
    print(result["final_report"])
    print("보고서 품질 평가:", result["report_quality"])
    output = Path(__file__).resolve().parent / "outputs" / "final_report.md"
    output.parent.mkdir(exist_ok=True)
    output.write_text(result["final_report"], encoding="utf-8")
