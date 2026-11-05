"""The LangGraph workflow that answers a question.

    START -> analyze -+-> contextualize -> retrieve -> generate -> validate -> END    (document question)
                      +-> calculator -> calculator_answer -> END
                      +-> direct_answer -> END

Every node reads the shared state and returns only the keys it changes. Nodes that show
progress (status text, answer tokens) send it through `writer`; the caller receives it when
the graph runs with stream_mode="custom". With invoke() the writer does nothing.

To keep answers fast:
  * a normal answer takes one LLM call (follow-up suggestions are generated later, separately)
  * the LLM only rewrites the question when it looks like a follow-up
  * validation is plain Python, with no LLM call
"""
import logging
import re
import time
from typing import Any, Iterator

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import StreamWriter

from app.agents.state import AgentState
from app.agents.tools import calculator
from app.config import settings
from app.errors import AppError, LLMUnavailable
from app.prompts.prompts import DIRECT_SYSTEM_PROMPT, RAG_SYSTEM_PROMPT, REWRITE_SYSTEM_PROMPT
from app.rag.context import build_context, build_sources, public_source
from app.rag.retriever import retrieve
from app.services import llm_service

logger = logging.getLogger(__name__)

NOT_FOUND_MESSAGE = "I couldn't find that in the uploaded documents."
NO_DOCUMENTS_MESSAGE = ("There are no processed documents to search yet. "
                        "Attach a PDF with the paperclip next to the message box and ask again.")

PROMPT_HISTORY_MESSAGES = 4     # earlier messages put in the answer prompt (follow-ups only)
PROMPT_HISTORY_CHARS = 400      # each earlier message is cut to this many characters
_ADDITIVE_METRICS = {"retrieval_ms", "generation_ms", "llm_calls"}

_GREETINGS = {"hi", "hello", "hey", "thanks", "thank you", "who are you", "what can you do", "help"}
_CALC_VERBS = re.compile(r"\b(calculate|compute|percentage|percent|subtract|multiply|divide|average)\b", re.I)
_PERCENT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)", re.I)
_ARITHMETIC = re.compile(r"\d\s*[+\-*/x×÷^]\s*\d")
_PURE_EXPR = re.compile(r"^[\d\s()+\-*/%.]+$")

# words that usually mean the question depends on earlier messages
_REFERENCE_WORDS = re.compile(
    r"\b(it|its|it's|they|them|their|theirs|this|these|those|he|she|his|her|former|latter|above|previous|earlier)\b", re.I)
_FOLLOW_UP_STARTS = re.compile(r"^(and|but|so|then|also|ok|okay|what about|how about|why)\b", re.I)


# ---------------------------------------------------------------- helpers
def _extract_expression(question: str) -> str | None:
    """Pull a plain arithmetic expression out of a calculation question, or return None."""
    q = question.strip().rstrip("?= ")
    match = _PERCENT_OF.search(q)
    if match:
        return f"({match.group(1)}/100)*({match.group(2)})"
    if _PURE_EXPR.fullmatch(q):
        return q
    plain_arithmetic = _ARITHMETIC.search(q) and len(re.findall(r"[A-Za-z]", q)) <= 12
    if _CALC_VERBS.search(q) or plain_arithmetic:
        expr = q.replace("×", "*").replace("÷", "/").replace("^", "**")
        expr = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", expr, flags=re.I)
        expr = re.sub(r"[^0-9+\-*/%.()\s]", " ", expr)
        expr = re.sub(r"\s+", " ", expr).strip()
        if re.search(r"\d\s*[+\-*/%]\s*\(?\d", expr):
            return expr
    return None


def looks_like_follow_up(question: str, history: list[dict[str, str]] | None) -> bool:
    """Quick guess, without the LLM, at whether a question needs the earlier messages."""
    if not history:
        return False
    q = question.strip()
    return len(q.split()) <= 5 or bool(_REFERENCE_WORDS.search(q)) or bool(_FOLLOW_UP_STARTS.match(q))


def _add_metrics(state: AgentState, **values: Any) -> dict[str, Any]:
    """Return a copy of the metrics with `values` merged in. Timings and counters add up, the rest are replaced."""
    merged = dict(state.get("metrics") or {})
    for key, value in values.items():
        if value is None:
            continue
        merged[key] = merged.get(key, 0) + value if key in _ADDITIVE_METRICS else value
    return merged


def _ms_since(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _prompt_history(history: list[dict[str, str]] | None) -> str:
    recent = (history or [])[-PROMPT_HISTORY_MESSAGES:]
    return "\n".join(f"{m['role']}: {m['content'][:PROMPT_HISTORY_CHARS]}" for m in recent)


def normalize_citations(answer: str) -> str:
    """Small models write [Source 1, 2] or [Sources 1 and 2]. Turn those into [Source 1][Source 2]."""
    def split(match: re.Match) -> str:
        return "".join(f"[Source {n}]" for n in re.findall(r"\d+", match.group(1)))
    return re.sub(r"\[Sources?\s+(\d+(?:\s*(?:,|and|&)\s*(?:Sources?\s*)?\d+)+)\]", split, answer, flags=re.I)


_SOURCES_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:sources?|references?|citations?)\s*:?\s*(?:\*\*|__)?\s*:?\s*$",
    re.I | re.M)
_SOURCES_ONE_LINE = re.compile(r"\n+\s*(?:\*\*)?(?:sources?|references?)(?:\*\*)?\s*:\s*(?:\[Source \d+\][,;\s]*)+\s*$", re.I)


def strip_source_list(answer: str) -> str:
    """Remove a "Sources:" list the model added at the end of its answer."""
    answer = _SOURCES_ONE_LINE.sub("", answer)
    headings = list(_SOURCES_HEADING.finditer(answer))
    if headings:
        last = headings[-1]
        tail = [line for line in answer[last.end():].splitlines() if line.strip()]
        looks_like_list = all(len(line) < 250 and (re.match(r"\s*(?:[-*•]|\d+[.)])\s", line)
                                                   or re.search(r"source|page|p\.|\.pdf", line, re.I))
                              for line in tail)
        if looks_like_list:
            answer = answer[:last.start()]
    return answer.rstrip()


# ---------------------------------------------------------------- nodes
def analyze_question(state: AgentState) -> dict[str, Any]:
    question = state["question"].strip()
    if question.lower().strip("!?. ") in _GREETINGS:
        return {"route": "direct"}
    if _extract_expression(question):
        return {"route": "calculator"}
    # everything that is not a greeting or a calculation is a document question
    return {"route": "document"}


def route_after_analysis(state: AgentState) -> str:
    return state["route"]


def contextualize_question(state: AgentState, writer: StreamWriter) -> dict[str, Any]:
    """Rewrite a follow-up so it can be searched on its own ("What about its cost?" becomes
    "What is the cost of the Pro plan?"). Questions that already stand alone skip this and save an LLM call."""
    question = state["question"]
    history = state.get("history") or []
    if not looks_like_follow_up(question, history):
        return {"standalone_question": question, "is_follow_up": False}

    writer({"type": "status", "text": "Understanding your follow-up question…"})
    recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-4:])
    try:
        rewritten = llm_service.generate([
            SystemMessage(content=REWRITE_SYSTEM_PROMPT),
            HumanMessage(content=f"Conversation:\n{recent}\n\nLatest message: {question}"),
        ], temperature=0, max_tokens=60)
    except LLMUnavailable:
        logger.warning("Question rewrite skipped because the LLM is unavailable")
        return {"standalone_question": question, "is_follow_up": True}
    rewritten = rewritten.strip().strip('"')
    if not rewritten or len(rewritten) > 400 or "\n" in rewritten:
        rewritten = question
    logger.info("Standalone question: %s", rewritten)
    return {"standalone_question": rewritten, "is_follow_up": True,
            "metrics": _add_metrics(state, llm_calls=1)}


def retrieve_documents(state: AgentState, writer: StreamWriter) -> dict[str, Any]:
    if state.get("document_ids") == []:          # no processed documents exist yet
        return {"documents": []}
    writer({"type": "status", "text": "Searching your documents…"})
    query = state.get("standalone_question") or state["question"]
    started = time.perf_counter()
    try:
        hits = retrieve(query, document_ids=state.get("document_ids"), user_id=state.get("user_id"))
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. Ollama is down and the question can't be embedded
        raise llm_service.translate_error(exc) from exc
    return {"documents": hits, "metrics": _add_metrics(state, retrieval_ms=_ms_since(started),
                                                       passages=len(hits))}


def generate_document_answer(state: AgentState, writer: StreamWriter) -> dict[str, Any]:
    if state.get("document_ids") == []:
        return {"answer": NO_DOCUMENTS_MESSAGE, "sources": [], "grounded": False}
    hits = state.get("documents") or []
    if not hits:
        return {"answer": NOT_FOUND_MESSAGE, "sources": [], "grounded": False}

    context, sources = build_context(build_sources(hits), settings.max_context_chars)
    writer({"type": "status", "text": "Reviewing document content…"})
    question = state.get("standalone_question") or state["question"]
    prompt = ""
    if state.get("is_follow_up"):
        history = _prompt_history(state.get("history"))
        if history:
            prompt += f"Earlier conversation:\n{history}\n\n"
    prompt += (f"Retrieved context:\n{context}\n\n"
               f"Question: {question}\n\nAnswer using only the retrieved context.")

    stats: dict[str, Any] = {}
    parts: list[str] = []
    started = time.perf_counter()
    for piece in llm_service.stream([SystemMessage(content=RAG_SYSTEM_PROMPT),
                                     HumanMessage(content=prompt)], stats=stats):
        parts.append(piece)
        writer({"type": "token", "text": piece})
    return {"answer": "".join(parts).strip(), "sources": sources,
            "metrics": _add_metrics(state, generation_ms=_ms_since(started), llm_calls=1,
                                    tokens=stats.get("tokens"), tokens_per_second=stats.get("tokens_per_second"))}


def validate_answer(state: AgentState) -> dict[str, Any]:
    """Clean up the model's answer. Citations are never shown, so any the model writes get removed."""
    answer = (state.get("answer") or "").strip()
    sources = state.get("sources") or []
    if not sources:
        return {"grounded": False, "sources": []}
    if answer.lower().startswith("i couldn't find"):
        return {"answer": NOT_FOUND_MESSAGE, "sources": [], "grounded": False}

    # Local models sometimes add citation markers even though the prompt says not to.
    # Strip them from the text, but keep the source details for follow-up suggestions.
    answer = strip_source_list(normalize_citations(answer))
    answer = re.sub(r"\[Source\s+\d+\]", "", answer, flags=re.I)
    answer = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", answer)
    answer = re.sub(r"\s+([.,;:!?])", r"\1", answer).strip()
    return {"answer": answer, "grounded": True,
            "sources": [public_source(source) for source in sources]}


def run_calculator(state: AgentState) -> dict[str, Any]:
    expression = _extract_expression(state["question"]) or ""
    try:
        return {"expression": expression, "tool_result": calculator.invoke({"expression": expression})}
    except Exception as exc:  # noqa: BLE001 - division by zero, unsupported expression, etc.
        return {"expression": expression, "tool_result": f"error: {exc}"}


def generate_calculator_answer(state: AgentState) -> dict[str, Any]:
    result = state.get("tool_result", "")
    if result.startswith("error"):
        answer = f"I couldn't calculate that ({result.removeprefix('error: ')})."
    else:
        answer = f"`{state.get('expression', '')}` = **{result}**"
    return {"answer": answer, "sources": [], "grounded": False}


def generate_direct_answer(state: AgentState, writer: StreamWriter) -> dict[str, Any]:
    parts: list[str] = []
    started = time.perf_counter()
    for piece in llm_service.stream([SystemMessage(content=DIRECT_SYSTEM_PROMPT),
                                     HumanMessage(content=state["question"])]):
        parts.append(piece)
        writer({"type": "token", "text": piece})
    return {"answer": "".join(parts).strip(), "sources": [], "grounded": False,
            "metrics": _add_metrics(state, generation_ms=_ms_since(started), llm_calls=1)}


# ---------------------------------------------------------------- graph
def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("analyze", analyze_question)
    graph.add_node("contextualize", contextualize_question)
    graph.add_node("retrieve", retrieve_documents)
    graph.add_node("document_answer", generate_document_answer)
    graph.add_node("validate", validate_answer)
    graph.add_node("calculator", run_calculator)
    graph.add_node("calculator_answer", generate_calculator_answer)
    graph.add_node("direct_answer", generate_direct_answer)

    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", route_after_analysis, {
        "document": "contextualize", "calculator": "calculator", "direct": "direct_answer"})
    graph.add_edge("contextualize", "retrieve")
    graph.add_edge("retrieve", "document_answer")
    graph.add_edge("document_answer", "validate")
    graph.add_edge("validate", END)
    graph.add_edge("calculator", "calculator_answer")
    graph.add_edge("calculator_answer", END)
    graph.add_edge("direct_answer", END)
    return graph.compile()


agent_graph = build_graph()


def _initial_state(question: str, history: list[dict[str, str]] | None,
                   document_ids: list[str] | None, user_id: str | None = None) -> dict[str, Any]:
    return {"question": question.strip(), "history": history or [], "document_ids": document_ids, "user_id": user_id, "metrics": {}}


def run_agent(question: str, history: list[dict[str, str]] | None = None,
              document_ids: list[str] | None = None, user_id: str | None = None) -> dict[str, Any]:
    """Run the graph start to finish and return the final state."""
    return agent_graph.invoke(_initial_state(question, history, document_ids, user_id))


def stream_agent(question: str, history: list[dict[str, str]] | None = None,
                 document_ids: list[str] | None = None, user_id: str | None = None) -> Iterator[tuple[str, Any]]:
    """Run the graph, yielding ("custom", event) as it works and ("values", state) at each step.
    The events are the dicts nodes send through `writer`: {"type": "status" | "token", ...}."""
    yield from agent_graph.stream(_initial_state(question, history, document_ids, user_id),
                                  stream_mode=["custom", "values"])
