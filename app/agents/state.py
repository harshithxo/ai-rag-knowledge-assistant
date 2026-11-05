from typing import Any, Literal
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    question: str                      # what the user typed
    history: list[dict[str, str]]      # earlier messages in this conversation
    document_ids: list[str] | None     # only search these documents ([] means nothing is searchable)
    user_id: str | None
    route: Literal["document", "calculator", "direct"]
    is_follow_up: bool                 # depends on earlier messages ("what about its cost?")
    standalone_question: str           # the follow-up, rewritten so it can be searched alone
    expression: str
    documents: list[Any]               # retrieved (chunk, score) pairs
    tool_result: str
    answer: str
    sources: list[dict[str, Any]]
    grounded: bool                     # True when the answer is based on retrieved passages
    metrics: dict[str, Any]            # timings and counters
