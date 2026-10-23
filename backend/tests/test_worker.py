import json
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import redis
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from beework.models import Base, Tenant
from beework.models.jobs import Job
from beework.worker.events import publish_run_event, run_event_tenant_id, stream_run_events
from beework.worker.queue import (
    HANDLERS,
    claim_jobs,
    enqueue,
    execute_job,
    job_handler,
    reclaim_expired_jobs,
    retry_delay,
)
from beework.worker.runner import run_once
from beework.worker.scheduler import enqueue_scheduled_jobs


class _PubSub:
    """Stands in for a Valkey subscription; hooks let tests act at exact moments."""

    def __init__(self, on_subscribe=None, on_wait=None, fail_subscribe=False):
        self.on_subscribe = on_subscribe
        self.on_wait = on_wait
        self.fail_subscribe = fail_subscribe
        self.subscribed = False
        self.closed = False

    def subscribe(self, _channel):
        if self.fail_subscribe:
            raise redis.ConnectionError("valkey down")
        self.subscribed = True
        if self.on_subscribe:
            self.on_subscribe()

    def get_message(self, timeout=0):
        if self.on_wait:
            self.on_wait()
        return None

    def close(self):
        self.closed = True


class _Redis:
    def __init__(self, pubsub=None, fail_publish=False):
        self._pubsub = pubsub or _PubSub()
        self.fail_publish = fail_publish
        self.published = []

    def publish(self, channel, payload):
        if self.fail_publish:
            raise redis.ConnectionError("valkey down")
        self.published.append((channel, payload))

    def pubsub(self, **_kwargs):
        return self._pubsub


class WorkerQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        event.listen(cls.engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
        cls.sessions = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        with self.sessions.begin() as db:
            tenant = Tenant(name="Acme", slug="acme")
            db.add(tenant)
            db.flush()
            self.tenant_id = tenant.id

    def test_enqueue_deduplicates_idempotency_key(self):
        with self.sessions.begin() as db:
            first = enqueue(db, "noop.echo", {"value": 1}, idempotency_key="same")
            second = enqueue(db, "noop.echo", {"value": 2}, idempotency_key="same")
            self.assertIs(first, second)
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(Job)))), 1)

    def test_enqueue_recovers_when_another_transaction_wins_the_key(self):
        with self.sessions() as db:
            # Simulate losing the race: the SELECT sees nothing, the INSERT collides.
            winner = Job(kind="noop.echo", payload={}, idempotency_key="race")
            db.add(winner)
            db.commit()
            original = db.scalar
            calls = {"n": 0}

            def blind_first_lookup(*args, **kwargs):
                calls["n"] += 1
                return None if calls["n"] == 1 else original(*args, **kwargs)

            db.scalar = blind_first_lookup
            job = enqueue(db, "noop.echo", {}, idempotency_key="race")
            db.commit()
            self.assertEqual(job.id, winner.id)
            self.assertEqual(len(list(db.scalars(select(Job)))), 1)

    def test_failure_backs_off_then_becomes_dead(self):
        @job_handler("test.always_fails")
        def always_fails(_db, _payload):
            raise RuntimeError("expected failure")

        self.addCleanup(HANDLERS.pop, "test.always_fails")
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            job = enqueue(db, "test.always_fails", {}, max_attempts=3, run_after=now)
            db.flush()
            job_id = job.id
            claim_jobs(db, "worker", now=now)
        with self.sessions() as db:
            first = execute_job(db, job_id, now=now)
            self.assertEqual((first.status, first.attempts), ("queued", 1))
            self.assertEqual(first.run_after.replace(tzinfo=UTC), now + retry_delay(1))
            self.assertEqual(claim_jobs(db, "worker", now=now), [])  # not due yet
            later = now + retry_delay(1)
            claim_jobs(db, "worker", now=later)
            db.commit()
            second = execute_job(db, job_id, now=later)
            self.assertEqual((second.status, second.attempts), ("queued", 2))
            self.assertEqual(second.run_after.replace(tzinfo=UTC), later + retry_delay(2))
            latest = later + retry_delay(2)
            claim_jobs(db, "worker", now=latest)
            db.commit()
            third = execute_job(db, job_id, now=latest)
            self.assertEqual((third.status, third.attempts), ("dead", 3))
            self.assertEqual(third.last_error, "expected failure")

    def test_unknown_job_kind_fails_visibly(self):
        with self.sessions.begin() as db:
            job = enqueue(db, "no.such.kind", {}, max_attempts=1)
            db.flush()
            job_id = job.id
            claim_jobs(db, "worker")
        with self.sessions() as db:
            done = execute_job(db, job_id)
            self.assertEqual(done.status, "dead")
            self.assertIn("No handler registered", done.last_error)

    def test_reclaims_timed_out_lock(self):
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            job = enqueue(db, "noop.echo", {})
            db.flush()
            job.status, job.locked_at, job.locked_by = "running", now - timedelta(minutes=16), "lost"
            self.assertEqual(reclaim_expired_jobs(db, now=now), 1)
            self.assertEqual(job.status, "queued")

    def test_worker_loop_step_runs_jobs_and_schedules_once_per_day(self):
        with self.sessions.begin() as db:
            enqueue(db, "noop.echo", {"message": "hello"}, tenant_id=self.tenant_id)
        self.assertEqual(run_once("worker-1", self.sessions), 2)  # noop.echo + audit.retention
        self.assertEqual(run_once("worker-2", self.sessions), 0)  # nothing new, no duplicate schedule
        with self.sessions() as db:
            jobs = list(db.scalars(select(Job)))
            self.assertEqual(sorted(job.kind for job in jobs), ["audit.retention", "noop.echo"])
            self.assertTrue(all(job.status == "succeeded" for job in jobs), [(j.kind, j.status, j.last_error) for j in jobs])

    def test_scheduler_is_idempotent_within_a_day_and_runs_again_next_day(self):
        day = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)
        for _ in range(3):
            with self.sessions.begin() as db:
                enqueue_scheduled_jobs(db, now=day)
        with self.sessions.begin() as db:
            enqueue_scheduled_jobs(db, now=day + timedelta(days=1))
        with self.sessions() as db:
            keys = sorted(job.idempotency_key for job in db.scalars(select(Job)))
        self.assertEqual(keys, ["audit.retention:2026-10-22", "audit.retention:2026-10-23"])

    # --- run events -------------------------------------------------------

    def _publish(self, run_id, event_type, data=None, redis_client=None):
        with self.sessions() as db:
            return publish_run_event(
                db, run_id, event_type, data or {}, tenant_id=self.tenant_id, redis_client=redis_client or _Redis()
            )

    def test_publish_assigns_increasing_seq_and_notifies_valkey(self):
        run_id = uuid4()
        client = _Redis()
        self.assertEqual([self._publish(run_id, "step", redis_client=client).seq for _ in range(3)], [1, 2, 3])
        self.assertEqual(json.loads(client.published[-1][1])["seq"], 3)
        self.assertEqual(client.published[0][0], f"run:{run_id}")
        with self.sessions() as db:
            self.assertEqual(run_event_tenant_id(db, run_id), self.tenant_id)
            self.assertIsNone(run_event_tenant_id(db, uuid4()))

    def test_event_survives_valkey_publish_failure(self):
        run_id = uuid4()
        self._publish(run_id, "step", redis_client=_Redis(fail_publish=True))
        self._publish(run_id, "done")
        with self.sessions() as db:
            events = list(stream_run_events(db, run_id, redis_client=_Redis(), poll_seconds=0))
        self.assertEqual([e["type"] for e in events], ["step", "done"])

    def test_stream_replays_history_and_stops_at_terminal_event(self):
        run_id = uuid4()
        for event_type in ("queued", "token", "done"):
            self._publish(run_id, event_type)
        with self.sessions() as db:
            events = list(stream_run_events(db, run_id, redis_client=_Redis(), poll_seconds=0))
            self.assertEqual([e["type"] for e in events], ["queued", "token", "done"])
            after = list(stream_run_events(db, run_id, after_seq=2, redis_client=_Redis(), poll_seconds=0))
            self.assertEqual([e["seq"] for e in after], [3])

    def test_event_published_while_subscribing_is_not_lost(self):
        run_id = uuid4()
        # The gap the old replay-then-subscribe order had: an event lands after the
        # (empty) history read but before the subscription is live.
        pubsub = _PubSub(on_subscribe=lambda: self._publish(run_id, "done"))
        with self.sessions() as db:
            events = list(stream_run_events(db, run_id, redis_client=_Redis(pubsub), poll_seconds=0))
        self.assertEqual([e["type"] for e in events], ["done"])
        self.assertTrue(pubsub.subscribed and pubsub.closed)

    def test_live_events_arrive_in_order_after_history(self):
        run_id = uuid4()
        self._publish(run_id, "queued")
        pending = iter([("token", {"t": "a"}), ("token", {"t": "b"}), ("done", {})])

        def publish_next():
            event_type, data = next(pending)
            self._publish(run_id, event_type, data)

        with self.sessions() as db:
            events = list(
                stream_run_events(db, run_id, redis_client=_Redis(_PubSub(on_wait=publish_next)), poll_seconds=0)
            )
        self.assertEqual([e["type"] for e in events], ["queued", "token", "token", "done"])
        self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4])

    def test_stream_falls_back_to_polling_when_valkey_is_down(self):
        run_id = uuid4()
        self._publish(run_id, "done")
        with self.sessions() as db:
            events = list(
                stream_run_events(db, run_id, redis_client=_Redis(_PubSub(fail_subscribe=True)), poll_seconds=0)
            )
        self.assertEqual([e["type"] for e in events], ["done"])

    def test_heartbeat_ticks_while_idle_so_disconnects_are_noticed(self):
        run_id = uuid4()
        ticks = {"n": 0}

        def finish_after_two_idle_ticks():
            ticks["n"] += 1
            if ticks["n"] == 2:
                self._publish(run_id, "cancelled")

        with self.sessions() as db:
            items = list(
                stream_run_events(
                    db,
                    run_id,
                    redis_client=_Redis(_PubSub(on_wait=finish_after_two_idle_ticks)),
                    poll_seconds=0,
                    heartbeat=True,
                )
            )
        self.assertEqual(items[:2], [None, None])
        self.assertEqual(items[-1]["type"], "cancelled")


if __name__ == "__main__":
    unittest.main()
