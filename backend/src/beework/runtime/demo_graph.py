from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class DemoState(TypedDict):
    message: str


def prepare_reply(state: DemoState) -> DemoState:
    return {"message": f"LangGraph is ready: {state['message']}"}


builder = StateGraph(DemoState)
builder.add_node("prepare_reply", prepare_reply)
builder.add_edge(START, "prepare_reply")
builder.add_edge("prepare_reply", END)
demo_graph = builder.compile()
