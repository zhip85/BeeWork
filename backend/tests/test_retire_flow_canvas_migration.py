"""T-169: the migration that retires the self-built flow canvas — it must never lose canvas data silently.

PostgreSQL only (the migration chain needs it): runs when TEST_DATABASE_URL is set (scripts/test-postgres.sh). Every case builds
its own throw-away database next to the test database, migrates it to the revision before the retirement, seeds it and runs the
migration through the real ``alembic`` command.
"""

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]
BEFORE, AFTER = "0050_api_plugins", "0051_retire_flow_canvas"
TEST_URL = os.environ.get("TEST_DATABASE_URL", "")


@unittest.skipUnless(TEST_URL.startswith("postgresql"), "needs TEST_DATABASE_URL (PostgreSQL)")
class RetireFlowCanvasMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        base = make_url(TEST_URL)
        self.name = f"beework_t169_{uuid.uuid4().hex[:8]}"
        self.admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with self.admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{self.name}"'))
        self.url = base.set(database=self.name).render_as_string(hide_password=False)
        self.engine = create_engine(self.url)
        self.addCleanup(self.drop)
        self.alembic("upgrade", BEFORE)
        self.tenant = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO tenants (id, name, slug, status, created_at) VALUES (:i, 'T', 't', 'active', now())"), {"i": self.tenant})

    def drop(self) -> None:
        self.engine.dispose()
        with self.admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
        self.admin.dispose()

    def alembic(self, *args: str, confirm: bool = False) -> subprocess.CompletedProcess:
        env = {**os.environ, "DATABASE_URL": self.url, "PYTHONPATH": str(BACKEND / "src")}
        env.pop("BEEWORK_DROP_FLOW_CANVAS", None)
        if confirm:
            env["BEEWORK_DROP_FLOW_CANVAS"] = "1"
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True)

    def canvas(self, deleted: bool) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agents (id, kind, name, description, icon, status, draft_config, tenant_id, created_at, updated_at, row_version, deleted_at)"
                    " VALUES (:i, 'chatflow', 'c', '', 'chat', 'draft', '{}', :t, now(), now(), 1, CASE WHEN :d THEN now() ELSE NULL END)"
                ),
                {"i": uuid.uuid4(), "t": self.tenant, "d": deleted},
            )

    def scalar(self, sql: str):
        with self.engine.connect() as connection:
            return connection.execute(text(sql)).scalar()

    def current(self) -> str:
        return self.alembic("current").stdout.strip().split()[0]

    def test_it_refuses_while_live_canvases_exist_and_says_how_many(self) -> None:
        self.canvas(False)
        self.canvas(False)
        result = self.alembic("upgrade", AFTER)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发现 2 个未删除的流程画布", result.stderr + result.stdout)
        self.assertIn("BEEWORK_DROP_FLOW_CANVAS", result.stderr + result.stdout)
        self.assertEqual((self.current(), self.scalar("SELECT count(*) FROM agents")), (BEFORE, 2))  # nothing was dropped

    def test_it_runs_when_there_is_no_live_canvas_even_if_deleted_ones_remain(self) -> None:
        self.canvas(True)
        result = self.alembic("upgrade", AFTER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.current(), self.scalar("SELECT to_regclass('agents')"), self.scalar("SELECT to_regclass('flow_versions')")), (AFTER, None, None))

    def test_a_confirmed_run_drops_the_canvas_its_references_and_disables_native_connections(self) -> None:
        self.canvas(False)
        flow = uuid.uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO resource_references (id, tenant_id, referrer_type, referrer_id, layer, relation, target_type, target_id, created_at) VALUES (:i, :t, 'flow', :f, 'draft', 'node', 'assistant', :x, now())"),
                {"i": uuid.uuid4(), "t": self.tenant, "f": flow, "x": uuid.uuid4()},
            )
            connection.execute(
                text("INSERT INTO resource_references (id, tenant_id, referrer_type, referrer_id, layer, relation, target_type, target_id, created_at) VALUES (:i, :t, 'assistant', :f, 'draft', 'mount', 'kb', :x, now())"),
                {"i": uuid.uuid4(), "t": self.tenant, "f": uuid.uuid4(), "x": uuid.uuid4()},
            )
            connection.execute(
                text(
                    "INSERT INTO flow_connections (id, kind, name, description, source_type, base_url, app_id, input_mapping, output_mapping, streaming, status, tenant_id, created_at, updated_at, row_version)"
                    " VALUES (:i, 'chatflow', 'old', '', 'native', '', '', '{}', '{}', false, 'enabled', :t, now(), now(), 1)"
                ),
                {"i": uuid.uuid4(), "t": self.tenant},
            )
        result = self.alembic("upgrade", AFTER, confirm=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.scalar("SELECT to_regclass('agents')"), None)
        self.assertEqual(self.scalar("SELECT count(*) FROM resource_references WHERE referrer_type = 'flow' OR target_type = 'flow'"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM resource_references WHERE referrer_type = 'assistant'"), 1)  # other references stay
        self.assertEqual(self.scalar("SELECT status FROM flow_connections WHERE name = 'old'"), "disabled")
        with self.engine.connect() as connection:
            columns = {row[0] for row in connection.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'flow_connections'"))}
        self.assertNotIn("flow_id", columns)

    def test_the_migration_can_be_downgraded_and_run_again(self) -> None:
        self.assertEqual(self.alembic("upgrade", AFTER).returncode, 0)
        down = self.alembic("downgrade", BEFORE)
        self.assertEqual(down.returncode, 0, down.stderr)
        self.assertEqual((self.scalar("SELECT to_regclass('agents')") is not None, self.scalar("SELECT to_regclass('flow_versions')") is not None), (True, True))
        self.assertEqual(self.alembic("upgrade", "head").returncode, 0)  # head may be past AFTER (e.g. 0052+); `check` compares the models against a head database only
        self.assertEqual(self.alembic("check").returncode, 0)  # the models and the migrated schema agree


if __name__ == "__main__":
    unittest.main()
