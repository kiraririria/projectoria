import json


def messages_for(system: str, payload: dict) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def estimate_input_tokens(messages: list[dict[str, str]]) -> int:
    # The provider reports actual counts with the model's own tokenizer.
    encoded = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return (len(encoded) + 3) // 4


def fit_messages(system: str, payload: dict, limit: int) -> list[dict[str, str]]:
    """Trim optional context in place, keeping the question and topic."""
    context = payload["dialog_context"]
    sources = payload.get("sources", [])
    messages = messages_for(system, payload)
    while estimate_input_tokens(messages) > limit:
        if sources and sources[0]["id"].split(".")[0] != sources[-1]["id"].split(".")[0]:
            last_source = sources[-1]["id"].split(".")[0]
            while sources[-1]["id"].startswith(f"{last_source}."):
                sources.pop()
        elif sum(turn["role"] == "user" for turn in context) >= 3:
            context.pop(1)
        elif context and context[-1]["role"] == "assistant":
            context.pop()
        elif len(context) > 1:
            context.pop(1)
        else:
            break
        messages = messages_for(system, payload)
    return messages
