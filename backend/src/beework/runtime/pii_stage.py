"""The ``pii_input`` and ``pii_output`` OC stages (T-055, OC-013, PLT-144).

Input rules run before memory is assembled: a block answers at once with the block message (the model and retrieval are
never called), masked text replaces the user's message for every later stage. Output rules run on the model's reply
before it reaches the user; while output rules can change the reply it is held back during generation and sent as one
piece afterwards, so unmasked text is never streamed. Hits carry the rule, layer, action and stage — never the text.
"""

from langgraph.config import get_stream_writer

from beework.governance.pii_engine import applies, scan
from beework.runtime.compiler import Node, RuntimeDeps, RuntimeState, register_stage
from beework.runtime.dsl import AssistantDSLv1


def holds_output(rules: list) -> bool:
    """Whether output rules may rewrite the reply, so streaming must wait for them."""
    return any(applies(rule, "output") and rule.action in ("mask", "block") for rule in rules)


@register_stage("pii_input")
def pii_input_stage(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        result = scan(state["input"], deps.pii_rules, "input")
        update: RuntimeState = {
            "pii_hits": [*state.get("pii_hits", []), *(hit.as_event() for hit in result.hits)],
            "pii_failures": [*state.get("pii_failures", []), *result.failures],
            "pii_last": {"hits": [hit.as_event() for hit in result.hits], "failures": result.failures, "blocked": result.blocked},
        }
        if result.blocked:
            get_stream_writer()({"type": "token", "text": result.message})
            update.update({"blocked": True, "block_message": result.message, "text": result.message})
        else:
            update["input"] = result.text  # later stages, memory included, only ever see the masked message
        return update

    return node


@register_stage("pii_output")
def pii_output_stage(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        if state.get("blocked"):
            return {}  # the reply is the block message itself
        result = scan(state.get("text") or "", deps.pii_rules, "output")
        text = result.message if result.blocked else result.text
        if deps.hold_output:
            get_stream_writer()({"type": "token", "text": text})
        return {
            "text": text,
            "pii_hits": [*state.get("pii_hits", []), *(hit.as_event() for hit in result.hits)],
            "pii_failures": [*state.get("pii_failures", []), *result.failures],
            "pii_last": {"hits": [hit.as_event() for hit in result.hits], "failures": result.failures, "blocked": result.blocked},
        }

    return node
