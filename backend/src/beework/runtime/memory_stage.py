"""The ``memory`` OC stage: inject long-term end-user memory into the prompt (T-056a)."""

from beework.runtime.compiler import Node, RuntimeDeps, RuntimeState, register_stage
from beework.runtime.dsl import AssistantDSLv1


@register_stage("memory")
def memory_stage(dsl: AssistantDSLv1, deps: RuntimeDeps) -> Node:
    def node(state: RuntimeState) -> RuntimeState:
        if not dsl.oc.memory.long_term.enabled:
            return {}
        return {"memory": deps.memory(state["input"])}

    return node
