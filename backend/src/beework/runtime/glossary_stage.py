"""The ``glossary_rewrite`` OC stage (T-057, OC-037 / OC-051): glossary expansion and, when configured, question rewriting.

The user's message is never changed in the conversation; this stage produces the *query* that routing and retrieval use:
the (optionally rewritten) question plus the synonyms of the glossary terms it mentions. The definitions of the matched
terms go into the prompt. ``only_low_confidence`` rewriting is decided by the route stage (T-161, which knows the intent
confidence); here a rewrite runs only when that option is off.
"""

from beework.governance.glossary_runtime import expand
from beework.runtime.compiler import Node, RuntimeDeps, RuntimeState, register_stage
from beework.runtime.dsl import AssistantDSLv1


def expansion_update(deps: RuntimeDeps, original: str, query: str, info: dict, sub_questions: list[str] | None = None) -> RuntimeState:
    """The state update of a finished query: its glossary synonyms and matched terms, and what the trace records. Shared with the route
    stage, which rewrites late when the rewrite is set to “only at low confidence” (T-161)."""
    expansion = expand(query, deps.dictionary)
    if expansion.synonyms:
        info["synonyms"] = expansion.synonyms
    if expansion.matches:
        info["matched_terms"] = [entry.term for entry in expansion.matches]
    update: RuntimeState = {
        "query": query,
        "synonyms": expansion.synonyms,
        "glossary_hits": [{"term": e.term, "aliases": list(e.aliases), "definition": e.definition} for e in expansion.matches],
    }
    if query != original:
        info["rewritten"] = query
    if info:
        update["stage_info"] = info
    if sub_questions:
        update["sub_questions"] = list(sub_questions)  # a decomposed question: the route stage sends each part where it belongs
    return update


@register_stage("glossary_rewrite")
def glossary_rewrite_stage(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        if state.get("blocked"):
            return {}
        original = state["input"]
        query = original
        info: dict = {}
        subs: list[str] = []
        config = dsl.oc.rewrite
        if deps.rewrite is not None and config.enabled and not config.only_low_confidence:
            with deps.clock.external():  # a model call, not pipeline overhead
                result = deps.rewrite(query, state.get("history", []))
            query = result.query
            subs = result.sub_questions
            info.update(rewrite_steps=result.steps, rewrite_failure=result.failure)
        return expansion_update(deps, original, query, info, subs)

    return node
