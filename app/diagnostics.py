import json
import logging
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class ChatDiagnostics:
    def __init__(self):
        self.request_id = uuid4().hex
        self.phase = "knowledge"

    def record(self, stage, started, *, outcome="ok", reason=None, model=None, requested_model=None, http_status=None):
        event = {
            "request_id": self.request_id,
            "stage": stage,
            "outcome": outcome,
            "duration_ms": round((perf_counter() - started) * 1000),
        }
        if reason is not None:
            event["reason"] = reason
        if model is not None:
            event["model"] = model
        if requested_model is not None:
            event["requested_model"] = requested_model
        if http_status is not None:
            event["http_status"] = http_status
        level = logging.INFO if reason is None else logging.WARNING
        logger.log(level, "%s", json.dumps(event, ensure_ascii=False, separators=(",", ":")))
