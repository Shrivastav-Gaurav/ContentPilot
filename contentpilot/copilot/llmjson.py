"""One JSON-returning LLM call with a single validation retry. Shared by prep and fit."""
import json

from pydantic import BaseModel, ValidationError

from contentpilot.copilot.triage import LLM, _strip_fences


class LLMJsonError(RuntimeError):
    pass


def call_json(llm: LLM, system: str, user: str, model: type[BaseModel], check=None) -> tuple[BaseModel, float]:
    """Return (parsed, cost_usd). check(parsed) may raise ValueError to force the retry."""
    raw, usage = llm(system, user)
    cost = float(usage.get("cost") or 0.0)
    try:
        parsed = model.model_validate(json.loads(_strip_fences(raw), strict=False))
        if check:
            check(parsed)
        return parsed, cost
    except (ValidationError, ValueError, json.JSONDecodeError) as first:
        retry = f"{user}\n\nYour previous answer failed validation: {first}\nReturn ONLY the corrected JSON."
        raw, usage = llm(system, retry)
        cost += float(usage.get("cost") or 0.0)
        try:
            parsed = model.model_validate(json.loads(_strip_fences(raw), strict=False))
            if check:
                check(parsed)
            return parsed, cost
        except (ValidationError, ValueError, json.JSONDecodeError) as second:
            raise LLMJsonError(str(second)) from second
