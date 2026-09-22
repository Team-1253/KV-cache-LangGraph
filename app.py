"""Integrated LangGraph scaffold for the six team roles."""

from langgraph.graph import END, START, StateGraph

from agents.resilient import continuing_node
from agents.state import EvaluationState


def build_graph():
    builder = StateGraph(EvaluationState)

    nodes = (
        ("technical_research", "agents.technical_research", "technical_research_agent", "technical_result"),
        ("trl_evaluation", "agents.technical_research", "trl_evaluation_node", "trl_result"),
        ("market_evaluation", "agents.market_evaluation", "market_evaluation_agent", "market_result"),
        ("stakeholder_evaluation", "agents.stakeholder_evaluation", "stakeholder_evaluation_agent", "stakeholder_result"),
        ("domain_evaluation", "agents.domain_evaluation", "domain_evaluation_agent", "domain_result"),
        ("evaluation_synthesis", "agents.evaluation_synthesis", "evaluation_synthesis_agent", "evaluation_result"),
        ("report_generation", "agents.report_generation", "report_generation_agent", "final_report"),
    )
    for name, module, function, result_key in nodes:
        builder.add_node(name, continuing_node(module, function, result_key))

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

    result = graph.invoke({
        "selected_technologies": {"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
        "target_domain": "데이터센터",
        "references": [],
    })
    print(result["final_report"])
