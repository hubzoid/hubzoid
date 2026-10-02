"""One engine per hub. A stale heartbeat never revokes a live lifetime lock."""
from __future__ import annotations
import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from sqlalchemy import text
from .. import db, migrations

log = logging.getLogger(__name__)
LEASE_SECONDS = 90


class OwnerBusy(RuntimeError):
    pass


class OwnershipLost(BaseException):
    """This process no longer owns the hub's workflow engine.

    A BaseException on purpose. DBOS records an Exception raised by a workflow
    or step as its outcome, and recovery would replay that failure under the
    next owner. Nothing is recorded for this one: the run stays PENDING, as
    after a crash, and the next owner's startup recovery queues it again.
    Author code that catches Exception cannot swallow it.

    Verified against DBOS 3.1.0 (pinned). Before upgrading DBOS, rerun
    test_postgres_owner_loss_stops_claiming_and_keeps_work_recoverable."""


class Owner:
    def __init__(self, hub_dir, hub):
        self.hub_dir, self.hub = Path(hub_dir), hub
        self.boot = uuid.uuid4().hex
        self.engine = db.operational_engine(hub_dir)
        migrations.upgrade(self.engine, 'operational')
        self.lock = None
        self.generation = 0
        self.ready = {}
        self._mutex = threading.Lock()
        self.lost = False
        self._postgres = False
        self._lock_engine = None
        self._listeners = []

    def on_lost(self, callback):
        """Call `callback(reason)` once, from the thread that notices the loss."""
        self._listeners.append(callback)

    def _lose(self, reason):
        first = not self.lost
        self.lost = True
        if first:
            log.error('workflows: %s; this process stops running workflows', reason)
            for callback in list(self._listeners):
                try:
                    callback(reason)
                except Exception:  # noqa: BLE001 — a listener never masks the loss
                    log.exception('workflows: owner-loss listener failed')
        raise OwnershipLost(reason)

    def acquire(self):
        # Lock the workflow DB identity, not a checkout directory: two checkouts
        # using one engine DB must never both recover the same unfinished runs.
        from sqlalchemy.engine import make_url
        url = make_url(db.sqlalchemy_url(db.dbos_url(self.hub_dir)))
        if url.get_backend_name() == 'sqlite':
            import fcntl
            path = Path(url.database).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = open(str(path) + '.owner.lock', 'a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise OwnerBusy('This hub already has a workflow engine owner') from None
            self.lock = lock
        else:
            from sqlalchemy import create_engine
            self._postgres = True
            self._lock_engine = create_engine(url, pool_pre_ping=False)
            self.lock = self._lock_engine.connect()
            self._key = int.from_bytes(hashlib.sha256(('hubzoid-engine-owner:'+self.hub.lower()).encode()).digest()[:8], 'big', signed=True)
            if not self.lock.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': self._key}).scalar():
                self.lock.close()
                self.lock = None
                self._lock_engine.dispose()
                raise OwnerBusy('This hub already has a workflow engine owner')
            self.lock.commit()
        try:
            with self.engine.begin() as conn:
                if self._postgres:
                    # A session lock is released the moment its connection drops,
                    # while that owner may still claim queued work until its next
                    # heartbeat notices. Its lease outlives that check, so a new
                    # owner waits for it to lapse. A clean stop clears it at once.
                    prior = conn.execute(text('SELECT expires FROM hz_workflow_owner WHERE hub=:h'), {'h': self.hub}).first()
                    if prior and prior.expires > time.time():
                        raise OwnerBusy("The previous workflow owner's lease ends in "
                                        f"{prior.expires - time.time():.0f}s")
                conn.execute(text('INSERT INTO hz_workflow_owner (hub,boot,generation,expires,ready) VALUES (:h,:b,1,:e,:r) ON CONFLICT (hub) DO UPDATE SET boot=:b,generation=hz_workflow_owner.generation+1,expires=:e,ready=:r'), {'h': self.hub, 'b': self.boot, 'e': time.time()+LEASE_SECONDS, 'r': '{}'})
                self.generation = conn.execute(text('SELECT generation FROM hz_workflow_owner WHERE hub=:h'), {'h': self.hub}).scalar_one()
        except BaseException:
            self.close()
            raise
        return self

    def assert_owned(self):
        if self.lost or self.lock is None:
            raise OwnershipLost('Workflow engine ownership was lost')
        # Never transparently reconnect a lost advisory-lock session.
        if self._postgres:
            with self._mutex:
                if self.lock.invalidated:
                    self._lose('Workflow owner database connection was lost')
                try:
                    self.lock.execute(text('SELECT 1'))
                    self.lock.commit()
                except Exception:
                    self._lose('Workflow owner database connection was lost')
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT boot,generation FROM hz_workflow_owner WHERE hub=:h'), {'h': self.hub}).first()
        if not row or row.boot != self.boot or row.generation != self.generation:
            self._lose('Workflow owner fencing token is stale')

    def heartbeat(self, ready=None):
        self.assert_owned()
        if ready is not None:
            self.ready = ready
        with self.engine.begin() as conn:
            changed = conn.execute(text('UPDATE hz_workflow_owner SET expires=:e,ready=:r WHERE hub=:h AND boot=:b AND generation=:g'), {'e': time.time()+LEASE_SECONDS, 'r': json.dumps(self.ready), 'h': self.hub, 'b': self.boot, 'g': self.generation}).rowcount
        if not changed:
            self._lose('Workflow owner fencing token is stale')

    def retire(self):
        """Close admission immediately while retaining the lifetime lock."""
        self.lost = True
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE hz_workflow_owner SET expires=0,ready=:r WHERE hub=:h AND boot=:b'), {'r':'{}', 'h':self.hub, 'b':self.boot})

    def close(self):
        if self.lock is None:
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(text('UPDATE hz_workflow_owner SET expires=0,ready=:r WHERE hub=:h AND boot=:b'), {'r': '{}', 'h': self.hub, 'b': self.boot})
        except Exception:  # noqa: BLE001 — the lock below is released regardless
            log.warning('workflows: could not clear the owner lease', exc_info=True)
        finally:
            if self._postgres:
                try:
                    self.lock.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': self._key})
                    self.lock.commit()
                except Exception:  # noqa: BLE001 — a dropped session already released it
                    log.warning('workflows: advisory unlock failed; the lock ended with its session')
                finally:
                    self.lock.close()
            else:
                self.lock.close()
            self.lock = None
            if self._lock_engine is not None:
                self._lock_engine.dispose()


def readiness(hub_dir, hub):
    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, 'operational')
    with engine.connect() as conn:
        row = conn.execute(text('SELECT boot,generation,expires,ready FROM hz_workflow_owner WHERE hub=:h'), {'h': hub}).mappings().first()
    if not row or row['expires'] <= time.time():
        return None
    data = json.loads(row['ready'])
    return {**data, 'boot': row['boot'], 'generation': row['generation']} if data else None
