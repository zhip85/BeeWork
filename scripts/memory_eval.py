"""Evaluate long-term memory extraction and reconciliation with a real model (T-056).

Runs the real code path (``extract_conversation_memory``) against an in-memory database, with the model of a LiteLLM gateway, over the
cases in ``memory_eval_cases.py``. Prints a table and the numbers worth comparing between prompt versions.

    LITELLM_URL=http://localhost:4000/v1 LITELLM_KEY=… LITELLM_MODEL=bw-… python scripts/memory_eval.py [--repeat 2]
"""

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_openai import ChatOpenAI  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import beework.main  # noqa: E402,F401  (imports the application in the order it expects)
from beework import memory as mem  # noqa: E402
from beework.models import Base, Tenant  # noqa: E402
from beework.models.assistants import Assistant  # noqa: E402
from beework.models.conversations import Conversation, EndUser, Message  # noqa: E402
from beework.models.memory import EndUserMemory  # noqa: E402
from beework.models.runtime import Run  # noqa: E402
from memory_eval_cases import SEQUENCES, SINGLE  # noqa: E402


def make_llm() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.environ["LITELLM_URL"], api_key=os.environ["LITELLM_KEY"], model=os.environ["LITELLM_MODEL"], temperature=0, timeout=60,
    )


class World:
    def __init__(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(bind=engine, expire_on_commit=False)
        with self.factory.begin() as db:
            raw = db.connection().connection
            raw.cursor().execute("PRAGMA foreign_keys=OFF")
        self.tenant_id, self.assistant_id, self.end_user_id = uuid4(), uuid4(), uuid4()
        with self.factory.begin() as db:
            db.add(Tenant(id=self.tenant_id, name="Eval", slug="eval", plan="enterprise", seat_limit=10))
            db.flush()
            db.add(EndUser(id=self.end_user_id, tenant_id=self.tenant_id, channel_type="portal", external_id="u1", name="U"))
            db.add(Assistant(id=self.assistant_id, tenant_id=self.tenant_id, organization_id=uuid4(), name="A", description="d", draft_config={}))

    def conversation(self, turns) -> Conversation:
        with self.factory.begin() as db:
            conversation = Conversation(
                id=uuid4(), tenant_id=self.tenant_id, assistant_id=self.assistant_id, end_user_id=self.end_user_id, turns=sum(1 for r, _ in turns if r == "用户"),
                source="portal",
            )
            db.add(conversation)
            db.flush()
            for seq, (role, text) in enumerate(turns, start=1):
                db.add(Message(tenant_id=self.tenant_id, conversation_id=conversation.id, seq=seq, role="user" if role == "用户" else "assistant", content=text))
            db.add(Run(tenant_id=self.tenant_id, assistant_id=self.assistant_id, conversation_id=conversation.id, status="succeeded", config={}, input={"message": "x"}))
            return conversation

    def extract(self, conversation: Conversation, llm) -> dict:
        with self.factory() as db:
            return mem.extract_conversation_memory(db, conversation.id, llm_factory=lambda *_: llm, now=datetime.now(UTC))

    def memories(self) -> list[tuple[str, str | None, str]]:
        with self.factory() as db:
            return [(m.file, m.type, m.content) for m in db.scalars(select(EndUserMemory).order_by(EndUserMemory.created_at))]


def check(texts: list[str], case: dict, rows) -> list[str]:
    problems = []
    for group in case.get("expect", []):
        if not any(alt in t for alt in group for t in texts):
            problems.append(f"missing {group}")
    for bad in case.get("forbid", []):
        if any(bad in t for t in texts):
            problems.append(f"forbidden {bad!r}")
    for file in case.get("expect_empty", []):
        if any(f == file for f, _, _ in rows):
            problems.append(f"{file} should be empty")
    for kind in case.get("forbid_types", []):
        if any(t == kind for _, t, _ in rows):
            problems.append(f"type {kind} stored")
    for needle, count in (case.get("count_containing") or {}).items():
        have = sum(1 for f, _, t in rows if f in ("memory", "profile") and needle in t)
        if have != count:
            problems.append(f"{needle!r} appears in {have} entries, wanted {count}")
    for needle, most in (case.get("max_containing") or {}).items():
        have = sum(1 for f, _, t in rows if f in ("memory", "profile") and needle in t)
        if have > most:
            problems.append(f"{needle!r} appears in {have} entries, at most {most} expected")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--baseline", action="store_true", help="write candidates without comparing them with existing memories (T-056a behaviour)")
    args = parser.parse_args()
    if args.baseline:
        mem._write_with_reconcile = lambda db, conversation, file, entries, *, llm, **kw: mem._replace_entries(db, conversation, file, entries, **kw)
    llm = make_llm()
    failures = total = 0
    for run in range(args.repeat):
        for case in SINGLE:
            world = World()
            world.extract(world.conversation(case["turns"]), llm)
            rows = world.memories()
            problems = check([t for f, _, t in rows if f != "session"] , case, rows)
            total += 1
            failures += bool(problems)
            print(f"[{run + 1}] single   {case['name']:<42} {'OK' if not problems else 'FAIL ' + '; '.join(problems)}   -> {[(f, t) for f, _, t in rows if f != 'session']}")
        for case in SEQUENCES:
            world = World()
            for turns in case["conversations"]:
                world.extract(world.conversation(turns), llm)
            rows = world.memories()
            problems = check([t for f, _, t in rows if f != "session"], case, rows)
            total += 1
            failures += bool(problems)
            print(f"[{run + 1}] sequence {case['name']:<42} {'OK' if not problems else 'FAIL ' + '; '.join(problems)}   -> {[(f, t) for f, _, t in rows if f != 'session']}")
    print(f"\n{total - failures}/{total} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
