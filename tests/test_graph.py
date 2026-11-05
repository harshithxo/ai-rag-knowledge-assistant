from langchain_core.documents import Document

from app.agents import graph as g
from app.agents.graph import analyze_question

TEST_USER_ID = "graph-test-user"


import pytest


@pytest.fixture(autouse=True)
def valid_retrieval_owner(monkeypatch):
    """Graph retrieval is owner scoped, so every fake indexed row has a real owner."""
    from app import db
    with db.connect() as conn:
        conn.execute("INSERT INTO users(id,full_name,email,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (TEST_USER_ID, "Graph Test User", "graph-test@example.test", "test", db.now_iso(), db.now_iso()))
    original_run, original_stream = g.run_agent, g.stream_agent

    def run_agent(*args, user_id=TEST_USER_ID, **kwargs):
        return original_run(*args, user_id=user_id, **kwargs)

    def stream_agent(*args, user_id=TEST_USER_ID, **kwargs):
        yield from original_stream(*args, user_id=user_id, **kwargs)

    monkeypatch.setattr(g, "run_agent", run_agent)
    monkeypatch.setattr(g, "stream_agent", stream_agent)


def doc(text, page=1, name="a.pdf", doc_id="d1"):
    return Document(page_content=text, metadata={"filename": name, "page_number": page, "chunk_id": f"{doc_id}:0", "document_id": doc_id, "user_id": TEST_USER_ID})


# ----- routing -----
def test_routes_calculation():
    assert analyze_question({"question": "Calculate 15% of 2500"})["route"] == "calculator"


def test_routes_document():
    assert analyze_question({"question": "What does the uploaded document say about internships?"})["route"] == "document"


def test_routes_greeting_to_direct():
    assert analyze_question({"question": "Hello!"})["route"] == "direct"


def test_questions_with_numbers_but_no_arithmetic_stay_document_questions():
    for q in ["What is section 3 about?", "Summarize the 2024-2025 report in detail", "Who won in 1950?"]:
        assert analyze_question({"question": q})["route"] == "document", q


def test_plain_arithmetic_routes_to_calculator():
    assert analyze_question({"question": "What is 20% of 500?"})["route"] == "calculator"
    assert analyze_question({"question": "12 * (3 + 4)"})["route"] == "calculator"


# ----- full graph runs with the fake LLM and fake vector store -----
def _index(store, text, **kw):
    d = doc(text, **kw)
    store.add_documents([d], ids=[d.metadata["chunk_id"]])


def test_document_question_returns_grounded_answer_without_visible_citations(store, llm):
    _index(store, "Interns receive a monthly stipend of 5000 rupees", page=4)
    result = g.run_agent("What stipend do interns receive?", document_ids=["d1"], user_id=TEST_USER_ID)
    assert result["grounded"] is True
    assert result["sources"][0]["filename"] == "a.pdf" and result["sources"][0]["page"] == 4
    assert "[Source" not in result["answer"]
    assert 0 <= result["sources"][0]["score"] <= 1 and result["sources"][0]["passages"]


def test_unrelated_question_says_not_found_without_calling_llm(store, llm):
    _index(store, "Interns receive a monthly stipend")
    result = g.run_agent("zebra xylophone quantum", document_ids=["d1"], user_id=TEST_USER_ID)
    assert result["answer"] == g.NOT_FOUND_MESSAGE
    assert result["sources"] == [] and result["grounded"] is False
    assert llm.calls == []


def test_no_documents_gives_helpful_message(store, llm):
    result = g.run_agent("What is in my files?", document_ids=[])
    assert "no processed documents" in result["answer"].lower()


def test_scope_limits_retrieval_to_selected_document(store, llm):
    _index(store, "alpha policy covers travel", doc_id="d1", name="one.pdf")
    store.add_documents([doc("alpha policy covers hiring", doc_id="d2", name="two.pdf")], ids=["d2:0"])
    result = g.run_agent("alpha policy", document_ids=["d2"], user_id=TEST_USER_ID)
    assert {s["filename"] for s in result["sources"]} == {"two.pdf"}


def test_follow_up_is_rewritten_using_history(store, llm):
    _index(store, "The enterprise plan costs 40 dollars per month")
    history = [{"role": "user", "content": "Tell me about the enterprise plan"},
               {"role": "assistant", "content": "It is the top tier."}]
    result = g.run_agent("What about its cost?", history, document_ids=["d1"], user_id=TEST_USER_ID)
    assert result["standalone_question"].startswith("standalone: What about its cost?")


def test_citation_markers_are_removed_and_retrieved_answers_remain_grounded(store, llm):
    _index(store, "Interns receive a monthly stipend")
    llm.answer = "They get paid [Source 1] and also a car [Source 9]."
    result = g.run_agent("interns stipend", document_ids=["d1"], user_id=TEST_USER_ID)
    assert "[Source 9]" not in result["answer"] and result["grounded"] is True

    llm.answer = "They get paid something."
    result = g.run_agent("interns stipend", document_ids=["d1"], user_id=TEST_USER_ID)
    assert result["grounded"] is True and "verify" not in result["answer"]


def test_model_saying_not_found_returns_no_sources(store, llm):
    _index(store, "Interns receive a monthly stipend")
    llm.answer = "I couldn't find that in the uploaded documents."
    result = g.run_agent("interns stipend", document_ids=["d1"], user_id=TEST_USER_ID)
    assert result["sources"] == [] and result["grounded"] is False


def test_calculator_route_is_deterministic(llm):
    result = g.run_agent("What is 20% of 500?")
    assert result["route"] == "calculator" and "100" in result["answer"]
    assert llm.calls == []


def test_calculator_errors_are_reported_not_raised(llm):
    result = g.run_agent("calculate 5 / 0")
    assert "couldn't calculate" in result["answer"]


# ----- speed: how many LLM calls does a question need? -----
def test_first_question_needs_exactly_one_llm_call(store, llm):
    _index(store, "Interns receive a monthly stipend of 5000 rupees")
    result = g.run_agent("What stipend do interns receive?", document_ids=["d1"], user_id=TEST_USER_ID)
    assert len(llm.calls) == 1 and result["metrics"]["llm_calls"] == 1


def test_standalone_question_in_a_conversation_skips_the_rewrite_call(store, llm):
    _index(store, "Leave policy for employees during probation is twelve days")
    history = [{"role": "user", "content": "Tell me about stipends"}, {"role": "assistant", "content": "They are monthly."}]
    result = g.run_agent("What is the leave policy for employees during probation?", history, document_ids=["d1"], user_id=TEST_USER_ID)
    assert len(llm.calls) == 1                       # no rewrite: the question already stands on its own
    assert result["standalone_question"] == "What is the leave policy for employees during probation?"


def test_real_follow_up_is_rewritten_and_costs_two_calls(store, llm, monkeypatch):
    _index(store, "The enterprise plan costs 40 dollars per month")
    history = [{"role": "user", "content": "Tell me about the enterprise plan"}, {"role": "assistant", "content": "Top tier."}]
    rewrites = []
    def rewrite(messages, temperature=None, max_tokens=None):
        rewrites.append(max_tokens)
        return "How much does the enterprise plan cost per month in dollars?"
    monkeypatch.setattr(g.llm_service, "generate", rewrite)
    result = g.run_agent("What about its cost?", history, document_ids=["d1"], user_id=TEST_USER_ID)
    assert rewrites == [60]                                  # one short rewrite call ...
    assert len(llm.calls) == 1                               # ... plus the streamed answer call
    assert result["metrics"]["llm_calls"] == 2 and result["grounded"] is True


def test_follow_up_detection():
    history = [{"role": "user", "content": "x"}]
    assert g.looks_like_follow_up("What about its cost?", history)
    assert g.looks_like_follow_up("And the deadline?", history)
    assert not g.looks_like_follow_up("What is the leave policy for employees during probation?", history)
    assert not g.looks_like_follow_up("What about its cost?", [])           # nothing to refer back to


def test_earlier_messages_are_only_added_to_the_prompt_for_follow_ups(store, llm):
    _index(store, "The enterprise plan costs 40 dollars per month")
    history = [{"role": "user", "content": "UNIQUE-EARLIER-MESSAGE"}, {"role": "assistant", "content": "ok"}]
    g.run_agent("How much does the enterprise plan cost per month in dollars?", history, document_ids=["d1"], user_id=TEST_USER_ID)
    assert "UNIQUE-EARLIER-MESSAGE" not in llm.calls[-1][-1].content
    llm.calls.clear()
    g.run_agent("What about its cost?", history, document_ids=["d1"], user_id=TEST_USER_ID)
    assert "UNIQUE-EARLIER-MESSAGE" in llm.calls[-1][-1].content          # the answer prompt (last call)


def test_metrics_are_recorded(store, llm):
    _index(store, "Interns receive a monthly stipend")
    m = g.run_agent("interns stipend", document_ids=["d1"], user_id=TEST_USER_ID)["metrics"]
    assert m["retrieval_ms"] >= 0 and m["generation_ms"] >= 0 and m["passages"] == 1
    assert m["tokens"] > 0 and m["tokens_per_second"] == 100.0


# ----- sources: one entry per document page, one Sources section -----
def test_chunks_from_the_same_page_become_one_source_with_several_passages(store, llm):
    for i, text in enumerate(["alpha policy covers travel costs", "alpha policy covers hotel costs"]):
        d = doc(text, page=3)
        d.metadata["chunk_id"] = f"d1:{i}"
        store.add_documents([d], ids=[d.metadata["chunk_id"]])
    result = g.run_agent("alpha policy costs", document_ids=["d1"], user_id=TEST_USER_ID)
    assert len(result["sources"]) == 1
    assert result["sources"][0]["page"] == 3 and len(result["sources"][0]["passages"]) == 2


def test_different_pages_stay_separate_sources(store, llm):
    for page in (1, 2):
        d = doc(f"alpha policy text on page {page}", page=page)
        d.metadata["chunk_id"] = f"d1:{page}"
        store.add_documents([d], ids=[d.metadata["chunk_id"]])
    llm.answer = "It is on both pages [Source 1][Source 2]."
    result = g.run_agent("alpha policy text page", document_ids=["d1"], user_id=TEST_USER_ID)
    assert sorted(s["page"] for s in result["sources"]) == [1, 2]


def test_a_sources_list_written_by_the_model_is_removed(store, llm):
    _index(store, "Interns receive a monthly stipend")
    llm.answer = "Interns are paid monthly [Source 1].\n\n**Sources:**\n- [Source 1] a.pdf, page 1"
    answer = g.run_agent("interns stipend", document_ids=["d1"], user_id=TEST_USER_ID)["answer"]
    assert "Sources" not in answer and "[Source" not in answer and answer.endswith("Interns are paid monthly.")
    llm.answer = "Interns are paid monthly [Source 1].\n\nSources: [Source 1]"
    assert "Sources:" not in g.run_agent("interns stipend", document_ids=["d1"], user_id=TEST_USER_ID)["answer"]


def test_real_content_mentioning_sources_is_not_removed():
    text = "The report lists funding sources in detail [Source 1].\n\n## Sources\nThe funding comes from three grants and a donation that were approved in a long board meeting held last year."
    assert g.strip_source_list(text) == text


def test_grouped_citations_are_normalised_and_both_sources_kept(store, llm):
    for page in (1, 2):
        d = doc(f"alpha policy text on page {page}", page=page)
        d.metadata["chunk_id"] = f"d1:{page}"
        store.add_documents([d], ids=[d.metadata["chunk_id"]])
    llm.answer = "Covered on both pages [Sources 1 and 2]."
    result = g.run_agent("alpha policy text page", document_ids=["d1"], user_id=TEST_USER_ID)
    assert "[Source" not in result["answer"] and len(result["sources"]) == 2


# ----- streaming through the graph -----
def test_stream_agent_sends_status_then_tokens_then_final_state(store, llm):
    _index(store, "Interns receive a monthly stipend of 5000 rupees")
    events = list(g.stream_agent("What stipend do interns receive?", document_ids=["d1"], user_id=TEST_USER_ID))
    custom = [data for kind, data in events if kind == "custom"]
    assert custom[0]["type"] == "status"
    tokens = [e["text"] for e in custom if e["type"] == "token"]
    assert len(tokens) > 1 and "".join(tokens).strip() == "The documents say so [Source 1]."
    assert events[-1][0] == "values" and events[-1][1]["grounded"] is True
