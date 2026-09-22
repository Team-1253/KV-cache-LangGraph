"""Integrated LangGraph scaffold for the six team roles."""

from langgraph.graph import END, START, StateGraph

from agents.domain_evaluation import domain_evaluation_agent
from agents.evaluation_synthesis import evaluation_synthesis_agent
from agents.market_evaluation import market_evaluation_agent
from agents.report_generation import report_generation_agent
from agents.stakeholder_evaluation import stakeholder_evaluation_agent
from agents.state import EvaluationState
from agents.technical_research import technical_research_agent, trl_evaluation_node


def build_graph():
    builder = StateGraph(EvaluationState)

    builder.add_node("technical_research", technical_research_agent)
    builder.add_node("trl_evaluation", trl_evaluation_node)
    builder.add_node("market_evaluation", market_evaluation_agent)
    builder.add_node("stakeholder_evaluation", stakeholder_evaluation_agent)
    builder.add_node("domain_evaluation", domain_evaluation_agent)
    builder.add_node("evaluation_synthesis", evaluation_synthesis_agent)
    builder.add_node("report_generation", report_generation_agent)

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
    builder.add_edge("report_generation", END)

    return builder.compile()


if __name__ == "__main__":
    graph = build_graph()
    print(graph.get_graph().draw_mermaid())
