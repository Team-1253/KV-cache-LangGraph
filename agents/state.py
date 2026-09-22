"""Common State shared by all LangGraph nodes."""

import operator
from typing import Annotated, Any, TypedDict


class EvaluationState(TypedDict, total=False):
    background_facts: dict[str, str]
    selected_technologies: dict[str, str]
    target_domain: str

    technical_result: dict[str, Any]
    trl_result: dict[str, Any]
    market_result: dict[str, Any]
    stakeholder_result: dict[str, Any]
    domain_result: dict[str, Any]

    evaluation_result: dict[str, list[str]]
    references: Annotated[list[dict[str, Any]], operator.add]
    final_report: str
