"""Langfuse tracing for runs (SDK v3 / OpenTelemetry only).

Tracing is best effort: a missing configuration or any Langfuse failure never affects the run.
The trace id is derived from the run id, so a run can always be located in Langfuse.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from beework.config import settings
from beework.integrations.langfuse import langfuse_configured

logger = logging.getLogger(__name__)


@dataclass
class TraceHandle:
    callbacks: list[Any] = field(default_factory=list)
    trace_id: str | None = None
    trace_url: str | None = None
    client: Any = None

    def flush(self) -> None:
        """Export buffered spans. Called after the run is finished so it never adds to run latency."""
        if self.client is None:
            return
        try:
            self.client.flush()
        except Exception:
            logger.warning("Langfuse flush failed", exc_info=True)


@contextmanager
def trace_run(run_id: UUID, *, metadata: dict[str, str]) -> Iterator[TraceHandle]:
    handle = TraceHandle()
    span_context = None
    client = None
    if langfuse_configured():
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler

            client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
                # Langfuse Cloud only exposes SDK v3 spans through GET /api/public/v2/observations
                # when they are ingested with this header.
                additional_headers={"x-langfuse-ingestion-version": "4"},
            )
            trace_id = Langfuse.create_trace_id(seed=str(run_id))
            span_context = client.start_as_current_span(
                name="assistant.run", trace_context={"trace_id": trace_id}
            )
            span = span_context.__enter__()
            span.update_trace(
                name="assistant.run",
                session_id=metadata.get("conversation_id"),
                metadata=metadata,
                tags=[f"source:{metadata.get('source', 'debug')}"],
            )
            handle.callbacks = [CallbackHandler(public_key=settings.langfuse_public_key)]
            handle.trace_id = trace_id
            handle.trace_url = client.get_trace_url(trace_id=trace_id)
            handle.client = client
        except Exception:
            logger.warning("Langfuse tracing unavailable for run %s", run_id, exc_info=True)
            span_context = None
            handle.callbacks = []
    try:
        yield handle
    finally:
        if span_context is not None:
            try:
                span_context.__exit__(None, None, None)
            except Exception:
                logger.warning("Langfuse span close failed for run %s", run_id, exc_info=True)
