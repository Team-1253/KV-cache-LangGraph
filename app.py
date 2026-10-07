"""기술 조사 → 작업 배분 → 병렬 평가 → 종합 → 보고서."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from langchain_teddynote import logging
from pydantic import BaseModel, Field

from agents.domain_evaluation import domain_evaluation_agent
from agents.market_evaluation import market_evaluation_agent
from agents.resilient import error_record
from agents.stakeholder_evaluation import stakeholder_evaluation_agent
from agents.state import OrchestratorState, WorkerState
from agents.synthesizer import synthesizer
from agents.technical_research import technical_research_agent, trl_evaluation_node

AGENTS = {
    "technical_result": technical_research_agent,
    "trl_result": trl_evaluation_node,
    "market_result": market_evaluation_agent,
    "stakeholder_result": stakeholder_evaluation_agent,
    "domain_result": domain_evaluation_agent,
}
PLANNER_PROMPT = Path(__file__).resolve().parent / "prompts" / "orchestrator.md"


class Plan(BaseModel):
    tasks: list[dict] = Field(
        description="작업 목록. 각 항목은 worker, tech_ids(기술 ID 목록), instruction(작업 지시), reason(분할·배정 이유)을 갖는다.",
    )


def worker(state: WorkerState) -> dict:
    """작업 하나를 실행하고 부모의 results에 결과를 반환한다."""

    task = state["task"]
    key = task["worker"]
    function = AGENTS[key]
    data = state["task_input"]
    status = "SUCCESS"
    try:
        output = function(data)
    except Exception as exc:
        error = error_record(function.__name__, exc)
        output = {key: {}, "run_errors": [error]}
        status = "FAILED"
    return {
        "results": [{
            "task_id": task["task_id"], "agent": function.__name__,
            "task": task, "output": output, "status": status,
        }],
        "errors": output.get("run_errors", []),
    }


def technical_research(state: OrchestratorState) -> dict:
    return worker({
        "task": {"task_id": "technical_research", "worker": "technical_result"},
        "task_input": state,
    })


def orchestrator(state: OrchestratorState) -> dict:
    """조사 결과와 평가 목표를 읽어 독립적으로 실행할 작업을 계획한다."""

    planner = init_chat_model("gpt-4.1-nano", model_provider="openai", temperature=0)
    plan = planner.with_structured_output(Plan, method="function_calling").invoke([
        SystemMessage(PLANNER_PROMPT.read_text(encoding="utf-8")),
        HumanMessage(json.dumps({
            "selected_technologies": state.get("selected_technologies", {}),
            "target_domain": state.get("target_domain", ""),
            "technical_result": state["results"][0]["output"].get("technical_result", {}),
            "errors": state.get("errors", []),
        }, ensure_ascii=False)),
    ])

    return {
        "run_id": state.get("run_id", str(uuid4())),
        "status": "WORKING",
        "plan": [{"task_id": f"task-{index}", **task}
                 for index, task in enumerate(plan.tasks, 1)],
    }


def assign_workers(state: OrchestratorState) -> list[Send | str]:
    research = state["results"][0]["output"]
    sends = []
    for task in state["plan"]:
        data = {
            "target_domain": state.get("target_domain", ""),
            "background_facts": state.get("background_facts", {}),
            "instruction": task["instruction"],
            "technical_result": {
                tech_id: research["technical_result"][tech_id]
                for tech_id in task["tech_ids"]
            },
            "references": [ref for ref in research.get("references", [])
                           if ref["tech_id"] in task["tech_ids"]],
        }
        sends.append(Send("worker", {"task": task, "task_input": data}))
    return sends or ["synthesizer"]


def build_graph():
    builder = StateGraph(OrchestratorState)
    builder.add_node("technical_research", technical_research)
    builder.add_node("orchestrator", orchestrator)
    builder.add_node("worker", worker)
    builder.add_node("synthesizer", synthesizer)
    builder.add_edge(START, "technical_research")
    builder.add_edge("technical_research", "orchestrator")
    builder.add_conditional_edges("orchestrator", assign_workers, ["worker", "synthesizer"])
    builder.add_edge("worker", "synthesizer")
    builder.add_edge("synthesizer", END)
    return builder.compile()


def run_evaluation(inputs: OrchestratorState) -> OrchestratorState:
    """교재의 LangSmith 설정을 적용하고 State와 Trace에 같은 실행 ID를 전달한다."""

    load_dotenv(Path(__file__).resolve().parent / ".env")
    logging.langsmith(
        os.getenv("LANGSMITH_PROJECT", "KV-Cache-Report"),
        set_enable=os.getenv("LANGSMITH_TRACING", "true").lower() == "true",
    )
    run_id = UUID(inputs["run_id"]) if inputs.get("run_id") else uuid4()
    return build_graph().invoke(
        {**inputs, "run_id": str(run_id)},
        config={"run_id": run_id, "run_name": "KV-Cache-Orchestrator-Workers",
                "metadata": {"run_id": str(run_id), "pattern": "orchestrator-workers"}},
    )


if __name__ == "__main__":
    result = run_evaluation({
        "selected_technologies": {"sw": "DeepSeek-V2 MLA", "hw": "ITME"},
        "target_domain": "데이터센터",
    })
    print(Path(result["report_uri"]).read_text(encoding="utf-8"))
    print(f"보고서 저장 위치: {result['report_uri']}")
    print(f"LangSmith 실행 ID: {result['run_id']}")
    wait_for_all_tracers()
