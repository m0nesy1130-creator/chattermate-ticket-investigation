"""本地节点运行记录。只写状态、耗时和用量，不写任务正文。"""

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt


TRACE_DIR = Path(__file__).resolve().parents[1] / "runtime" / "traces"
_logger = logging.getLogger(__name__)
_events = ContextVar("node_events", default=None)
_attempts = ContextVar("model_attempts", default=None)


@contextmanager
def collect_events():
    events = []
    previous = _events.set(events)
    try:
        yield events
    finally:
        _events.reset(previous)


def record_attempt(**event):
    attempts = _attempts.get()
    if attempts is not None:
        attempts.append(event)


def _trace_path(state, config):
    thread_id = config.get("configurable", {}).get("thread_id")
    if state.get("mode") == "live" and isinstance(thread_id, str):
        try:
            return TRACE_DIR / f"{UUID(thread_id).hex}.jsonl"
        except ValueError:
            pass
    return None


def observed_node(node, phase):
    def run(state, config: RunnableConfig):
        path = _trace_path(state, config)
        events = _events.get()
        if path is None and events is None:
            return node(state)
        started = perf_counter()
        attempts = []
        previous = _attempts.set(attempts)
        status = "ok"
        error_type = None
        try:
            return node(state)
        except GraphInterrupt:
            status = "awaiting_input"
            raise
        except Exception as error:
            status = "error"
            error_type = type(error).__name__
            raise
        finally:
            _attempts.reset(previous)
            event = {
                "at": datetime.now(timezone.utc).isoformat(),
                "node": node.__name__,
                "phase": phase,
                "step_id": (state.get("active_step") or None) if phase in {"retrieve", "resolve", "review"} else None,
                "status": status,
                "error_type": error_type,
                "seconds": round(perf_counter() - started, 6),
                "model_attempts": attempts,
            }
            if events is not None:
                events.append(event)
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                except OSError:
                    _logger.warning("无法写入本地任务运行记录。")
    if "return" in node.__annotations__:
        run.__annotations__["return"] = node.__annotations__["return"]
    return run
