"""Conversational interface scoped to a single saved card. Unlike the
Interview Prep synthesizer (which pulls from the whole library), this agent's
context is just one card — the full extraction plus the first 3000 chars of
the original source text.

Plain chat replies (no JSON schema / Pydantic validation here): the output is
free-form conversational text, not structured data to be stored and queried.
"""
import requests

from contentpilot import config
from contentpilot.storage import get_card_chat_context, load_chat_history, save_chat_turn
from contentpilot.tracing import trace_llm_call

SYSTEM_PROMPT_TEMPLATE = """You are a learning coach helping a senior TPM prepare for FAANG interviews.
You have been given the full content of one saved post or video.
Your job is to help the user deeply understand it, extract what matters for interviews,
and turn it into something they can actually use.

Be direct. Don't summarize what they can already read.
Push their thinking. Ask back when their answer is shallow.
Cite specific parts of the content when you make a point.

Card context:
Title: {title}
Author: {author}
Summary: {summary}
Key learnings: {key_learnings}
Topics: {topics}
Build ideas: {build_ideas}
Job signals: {job_signals}
Full content: {content_snippet}"""

STARTERS = {
    "study": "Help me really understand the core idea here. Don't just summarize — what's the mental model I should internalize?",
    "build": "What could I actually build from this? Give me 2-3 concrete project ideas with a one-line 'how you'd build it' for each.",
    "apply": "What would a FAANG interviewer ask me about this? And how could I use something from my own experience at Shell to answer it?",
    "share": "Draft a LinkedIn post using the key insight here. Write it in a direct, first-person voice — no fluff, no 'excited to share'.",
}


class CardChatError(ValueError):
    pass


def _build_system_prompt(context: dict) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        title=context["title"],
        author=context["author"],
        summary=context["summary"],
        key_learnings=", ".join(context["key_learnings"]) or "(none)",
        topics=", ".join(context["topics"]) or "(none)",
        build_ideas=", ".join(context["build_ideas"]) or "(none)",
        job_signals=", ".join(context["job_signals"]) or "(none)",
        content_snippet=context["content_snippet"] or "(not available)",
    )


def chat(card_id: int, user_message: str, history: list[dict] | None = None) -> str:
    """Sends user_message (with prior turns) to the chat agent, scoped to
    card_id's content. Saves both the user message and the assistant's reply
    to card_chats. If history is None, it's loaded from card_chats.
    """
    context = get_card_chat_context(card_id)
    if context is None:
        raise CardChatError(f"No card found for card_id={card_id}")

    if history is None:
        history = load_chat_history(card_id)

    api_key = config.openrouter_api_key()
    model = config.synthesis_model()

    messages = [{"role": "system", "content": _build_system_prompt(context)}]
    messages += history
    messages.append({"role": "user", "content": user_message})

    with trace_llm_call("card_chat", card_id=card_id):
        response = requests.post(
            config.OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages, "temperature": 0.6},
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()

    reply = payload["choices"][0]["message"]["content"].strip()
    if not reply:
        raise CardChatError("Model returned an empty response.")

    save_chat_turn(card_id, "user", user_message)
    save_chat_turn(card_id, "assistant", reply)

    return reply
