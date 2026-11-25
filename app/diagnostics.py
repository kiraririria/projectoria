import json
import logging
from time import monotonic
from uuid import uuid4


logger = logging.getLogger("projectoria.chat")
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)
logger.propagate = False


class ChatDiagnostics:
    def __init__(self):
        self.request_id = uuid4().hex
        self.started = monotonic()
        self.provider_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def emit(self, event: str, started: float, outcome: str, **fields) -> None:
        logger.info(json.dumps({
            "event": event,
            "request_id": self.request_id,
            "duration_ms": round((monotonic() - started) * 1000, 1),
            "outcome": outcome,
            **fields,
        }, separators=(",", ":")))

    def stage(self, name: str, started: float, outcome: str, **fields) -> None:
        self.emit("chat_stage", started, outcome, stage=name, **fields)

    def provider_call(self, stage: str, started: float, outcome: str, status: int | None, usage: object) -> None:
        self.provider_calls += 1
        counts = {}
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens"):
                value = usage.get(key)
                if type(value) is int and 0 <= value <= 10_000_000:
                    counts[key] = value
        self.prompt_tokens += counts.get("prompt_tokens", 0)
        self.completion_tokens += counts.get("completion_tokens", 0)
        self.emit("chat_provider_call", started, outcome, stage=stage, http_status=status, **counts)

    def finish(self, outcome: str, **fields) -> None:
        self.emit("chat_total", self.started, outcome, provider_calls=self.provider_calls,
                  prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens, **fields)
