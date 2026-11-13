"""The ``route`` OC stage (T-161, 演进 PRD 23.1): L2 intent judgement — a capability answers directly (path A) or the L3 agent plans (path B).

The stage decides with ``runtime.intent.decide`` and records the decision for the trace; it never changes the conversation. Path A
runs the capability here (through the controlled tool executor, so permissions, argument checks, credential scrubbing and the call
records are the same as when a model calls it as a tool): a Dify application's answer, a capability that declares ``direct_output`` and an
FAQ answer *are* the reply; any other result is handed to the ``execute`` stage, which composes the reply from it in one model call
without tools. Everything that does not work out — no candidates, a classifier that fails, low confidence, a write, arguments that are
missing or invalid, a capability that fails — is **path B**: the normal tool-using agent, with the candidates and the reason as hints.
Nothing in the stage raises into the run because of the classifier.

With intent judgement off (or nothing in scope) the agent answers as before. The stage also decides the rewrite when that is set to
“only at low confidence” (T-057 left it to this stage): the message is classified as written; only when the confidence is below the
rewrite threshold is it rewritten and classified again.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from beework.runtime.compiler import Node, RuntimeDeps, RuntimeState, register_stage
from beework.runtime.dsl import AssistantDSLv1
from beework.runtime.glossary_stage import expansion_update
from beework.runtime.intent import Candidate, ClassifyResult, IntentDecision, Slots, decide

MAX_RECORDED = 8


@dataclass
class FaqAnswer:
    text: str
    chunk: dict[str, Any]  # shaped like the retrieval context of ``run_agent`` so citations and FAQ hit counts work unchanged


@dataclass
class IntentRuntime:
    """What the route stage needs from the outside world, so the stage itself stays a function of its inputs."""

    callable_by_id: dict[str, Candidate]  # what a rule may target / path A may call
    candidates: Callable[[str], list[Candidate]]  # the registry recall for a query, inside the agent's scope
    classify: Callable[[str, list[dict[str, str]], list[Candidate]], ClassifyResult]
    extract: Callable[[Candidate, str, list[dict[str, str]]], Slots]
    invoke: Callable[..., Any]  # (candidate, args, stream=None) -> tools.executor.ToolResult; ``stream`` receives tokens as they arrive
    faq: Callable[[Candidate, str], FaqAnswer | None]
    pending: Callable[[], Candidate | None]


def _brief(candidate: Candidate) -> dict[str, str]:
    return {"id": candidate.id, "name": candidate.name, "kind": candidate.kind}


def route_record(decision: IntentDecision, candidates: list[Candidate]) -> dict[str, Any]:
    """The decision as the trace, the run's output and the step event carry it: ids, names, scores and reasons by code — never the
    user's text, an argument value or the classifier's free-text reason (which may quote the message)."""
    info: dict[str, Any] = {"version": 2, "path": decision.path, "source": decision.source}
    if decision.target is not None:
        info["target"] = _brief(decision.target)
    if decision.rule_name:
        info.update(rule=decision.rule_name, rule_id=decision.rule_id)
    confidence = {s.id: s.confidence for s in decision.scored}
    info["candidates"] = [
        {**_brief(c), "score": round(c.recall_score, 3), **({"confidence": round(confidence[c.id], 3)} if c.id in confidence else {})}
        for c in candidates[:MAX_RECORDED]
    ]
    if decision.confidence is not None:
        info["confidence"] = round(decision.confidence, 3)
    if decision.second is not None:
        info["second"] = round(decision.second, 3)
    if decision.classifier_ms is not None:
        info["classifier_ms"] = decision.classifier_ms
    if decision.failure:
        info["failure"] = decision.failure
    if decision.path == "B" and decision.handoff != "none":
        info["handoff"] = decision.handoff
    return info


def _hints(decision: IntentDecision, candidates: list[Candidate], *, failed: str | None = None, missing: list[str] | None = None) -> dict[str, Any]:
    """What L3 gets: the candidates the classifier thought of (best first), what failed, what is missing."""
    by_id = {c.id: c for c in candidates}
    items = [
        {"name": by_id[s.id].name, "kind": by_id[s.id].kind, "intent": by_id[s.id].intent_description, "confidence": s.confidence}
        for s in decision.scored if s.id in by_id
    ]
    if not items:  # nothing was scored (no classifier answer): the best recalled ones are still worth a mention
        items = [{"name": c.name, "kind": c.kind, "intent": c.intent_description, "confidence": None} for c in candidates[:3] if c.recall_score > 0]
    return {"candidates": items, "failed": failed, "missing": missing or []}


@register_stage("route", replace=True)
def route_stage(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    config, rewrite_config = dsl.oc.intent, dsl.oc.rewrite
    late_rewrite = deps.rewrite is not None and rewrite_config.enabled and rewrite_config.only_low_confidence

    def rewrite_now(original: str, query: str, history: list[dict[str, str]]) -> RuntimeState:
        with deps.clock.external():  # a model call, not pipeline overhead
            result = deps.rewrite(query, history)
        if result.failure or result.query == query:
            return {"stage_info": {"rewrite_failure": result.failure}} if result.failure else {}
        return expansion_update(deps, original, result.query, {"rewrite_steps": result.steps, "rewrite_failure": None}, result.sub_questions)

    def call(runtime: IntentRuntime, target: Candidate, args: dict[str, Any], *, stream: bool = False):
        deps.check_stop()
        sink = None
        if stream and not deps.hold_output:  # the reply is this capability's own answer: its tokens go to the user as they arrive
            from langgraph.config import get_stream_writer

            writer = get_stream_writer()
            sink = lambda text: writer({"type": "token", "text": text})  # noqa: E731
        with deps.clock.external(), deps.span(f"route.{target.kind}"):
            return runtime.invoke(target, args, sink)

    def run_path_a(runtime: IntentRuntime, decision: IntentDecision, candidates: list[Candidate], query: str, history: list[dict[str, str]]) -> RuntimeState:
        """Carry out path A. Anything that does not work out turns the decision into path B (in place) and returns the L3 hints."""
        target = decision.target
        assert target is not None
        if target.kind == "kb":  # FAQ direct answer: only a confident FAQ hit answers; otherwise L3 (the usual retrieval) does
            with deps.clock.external():
                faq = runtime.faq(target, query)
            if faq is None:
                decision.to_b("not_direct")
                return {"l3_hints": _hints(decision, candidates)}
            decision.source = "faq"
            return {"text": faq.text, "answered": True, "context": [faq.chunk], "retrieval_mode": "faq", "usage": {"input": None, "output": None}}
        with deps.clock.external():
            slots = runtime.extract(target, query, history)
        if slots.status != "ok":
            decision.to_b({"missing": "slots_missing", "invalid": "slots_invalid"}.get(slots.status, "slots_failed"))  # type: ignore[arg-type]
            update: RuntimeState = {"l3_hints": _hints(decision, candidates, missing=slots.missing)}
            update["route_info"] = {"slots": {"status": slots.status, "missing": slots.missing}}
            return update
        direct = target.kind.startswith("dify") or target.direct_output
        result = call(runtime, target, slots.args, stream=direct)
        if result.status != "ok":
            decision.to_b("capability_failed")
            return {"l3_hints": _hints(decision, candidates, failed=target.name), "route_info": {"slots": {"status": "ok"}, "capability_error": result.code or "tool.failed"}}
        info: dict[str, Any] = {"slots": {"status": "ok"}, "executed": True}
        if target.kind == "dify_chatflow":
            info["pending"] = {"entry_id": target.id}  # the Chatflow may be waiting for the next message; the connection decides (sticky, idle time, exit words)
        if direct:
            return {"text": result.content.strip(), "answered": True, "usage": {"input": None, "output": None}, "route_info": info}
        return {"capability_result": {"name": target.name, "content": result.content}, "compose": True, "route_info": info}

    def route_sub_questions(runtime: IntentRuntime, subs: list[str], history: list[dict[str, str]]) -> RuntimeState | None:
        """Every part of a decomposed question goes through the same judgement. The parts a capability answers directly are answered
        now and handed to the agent's model to merge; the rest stay with the agent. ``None`` when no part could be answered directly."""
        results: list[dict[str, str]] = []
        parts: list[dict[str, Any]] = []
        for index, sub in enumerate(subs, start=1):
            candidates = runtime.candidates(sub)
            with deps.clock.external():
                decision = decide(text=sub, candidates=candidates, config=config, classify=lambda q, c=candidates: runtime.classify(q, history, c), callable_by_id=runtime.callable_by_id)
            answered = False
            if decision.path == "A" and decision.target is not None and decision.target.kind != "kb":
                with deps.clock.external():
                    slots = runtime.extract(decision.target, sub, history)
                if slots.status == "ok":
                    result = call(runtime, decision.target, slots.args)
                    if result.status == "ok" and result.content.strip():
                        results.append({"question": sub, "target": decision.target.name, "answer": result.content.strip()})
                        answered = True
            parts.append({"index": index, "target": decision.target.name if answered and decision.target else None, "source": decision.source if answered else "assistant"})
        if not results:
            return None
        return {"sub_results": results, "route_info": {"version": 2, "path": "B", "source": "none", "handoff": "decomposed", "parts": parts}}

    def node(state: RuntimeState) -> RuntimeState:
        if state.get("blocked"):
            return {}
        runtime: IntentRuntime | None = deps.routing
        history = state.get("history", [])
        original = state["input"]
        query = state.get("query") or original
        if not (config.enabled and runtime is not None):
            update: RuntimeState = {"target": "assistant"}
            if late_rewrite:  # nothing measures the confidence, so a rewrite the administrator switched on just runs
                update.update(rewrite_now(original, query, history))
            return update

        subs = state.get("sub_questions") or []
        if len(subs) >= 2:
            decomposed = route_sub_questions(runtime, subs, history)
            if decomposed is not None:
                return {"target": "assistant", **decomposed}

        candidates = runtime.candidates(query)
        rewritten: dict[str, RuntimeState] = {}

        def classify(text: str) -> ClassifyResult:
            with deps.clock.external():
                first = runtime.classify(text, history, candidates)
            low = not first.scored or first.scored[0].confidence < rewrite_config.confidence_threshold
            if late_rewrite and first.failure is None and low:
                # Not sure enough: rewrite the question, then classify the rewritten one (the user's own words stay the conversation).
                fresh = rewrite_now(original, text, history)
                if fresh.get("query") and fresh["query"] != text:
                    rewritten["update"] = fresh
                    with deps.clock.external():
                        return runtime.classify(fresh["query"], history, candidates)
            return first

        decision = decide(
            text=original, candidates=candidates, config=config, classify=classify, pending=runtime.pending(),
            callable_by_id=runtime.callable_by_id,
        )
        update = {"target": "assistant"}
        if "update" in rewritten:
            update.update(rewritten["update"])
        elif late_rewrite and decision.path == "B" and decision.handoff in ("no_candidates", "classifier_failed"):
            update.update(rewrite_now(original, query, history))  # no confidence was measured: rewrite anyway
        outcome: RuntimeState = {}
        if decision.path == "A":
            outcome = run_path_a(runtime, decision, candidates, update.get("query") or query, history)
        elif decision.target is not None or candidates:
            outcome = {"l3_hints": _hints(decision, candidates)}
        info = route_record(decision, candidates)  # after path A had its say: it may have turned the decision into path B
        info.update((outcome.pop("route_info", None)) or {})
        update.update(outcome)
        update["route_info"] = info
        return update

    return node
