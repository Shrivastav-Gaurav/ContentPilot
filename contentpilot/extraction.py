import json

import requests
from pydantic import BaseModel, Field, ValidationError

from contentpilot import config
from contentpilot.prompts import tpm_lens
from contentpilot.tracing import trace_llm_call

# Haiku's context window is ~200k tokens (~800k chars). Chunk only beyond that.
_MAX_CHARS_SINGLE_PASS = 600_000
_CHUNK_CHARS = 500_000


class Entities(BaseModel):
    people: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)


class Extraction(BaseModel):
    title: str
    summary: str
    content_type: str
    topics: list[str] = Field(default_factory=list)
    key_learnings: list[str] = Field(min_length=1, max_length=7)
    build_ideas: list[str] = Field(default_factory=list, max_length=5)
    job_signals: list[str] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)


class ExtractionResult(BaseModel):
    extraction: Extraction
    prompt_version: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


class ExtractionValidationError(ValueError):
    def __init__(self, message: str, cost_usd: float = 0.0, model: str | None = None):
        super().__init__(message)
        self.cost_usd = cost_usd  # the failed calls still cost money
        self.model = model


def _strip_json_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        return raw.strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start:end + 1]
    return raw


def _call_openrouter(content: str, extra_instruction: str = "") -> dict:
    model = config.openrouter_model()

    user_content = f"<content>\n{content}\n</content>"
    if extra_instruction:
        user_content += f"\n\n{extra_instruction}"

    response = requests.post(
        config.OPENROUTER_URL,
        headers=config.openrouter_headers(),
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": tpm_lens.SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.3,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def extract_tpm_lens(content: str, source_id: int | None = None) -> ExtractionResult:
    if len(content) > _MAX_CHARS_SINGLE_PASS:
        content = content[:_CHUNK_CHARS]

    # content_type isn't known until the LLM responds, so it can't be a trace
    # tag (tags are set at span creation); source_id is the one identifying
    # tag available before the call, and is traced here.
    with trace_llm_call("card_extraction", source_id=source_id):
        payload = _call_openrouter(content)
        raw = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage", {})
        cost = float(usage.get("cost") or 0.0)

        try:
            parsed = Extraction.model_validate_json(_strip_json_fences(raw))
        except (ValidationError, json.JSONDecodeError) as first_error:
            retry_instruction = (
                "Your previous response failed validation with this error:\n"
                f"{first_error}\n"
                "Respond again with ONLY the corrected valid JSON in the exact shape requested."
            )
            payload = _call_openrouter(content, extra_instruction=retry_instruction)
            raw = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
            cost += float(usage.get("cost") or 0.0)
            try:
                parsed = Extraction.model_validate_json(_strip_json_fences(raw))
            except (ValidationError, json.JSONDecodeError) as second_error:
                raise ExtractionValidationError(str(second_error), cost_usd=cost,
                                                model=config.openrouter_model()) from second_error

    return ExtractionResult(
        extraction=parsed,
        prompt_version=tpm_lens.VERSION,
        model=config.openrouter_model(),
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        cost_usd=cost if (cost or usage.get("cost") is not None) else None,
    )
