"""One engine per hub. A stale heartbeat never revokes a live lifetime lock."""
from __future__ import annotations
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from sqlalchemy import text
from .. import db, migrations


class OwnerBusy(RuntimeError):
    pass


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
            self.lock = self.engine.connect()
            self._key = int.from_bytes(hashlib.sha256(('hubzoid-owner:' + self.hub).encode()).digest()[:8], 'big', signed=True)
            if not self.lock.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': self._key}).scalar():
                self.lock.close()
                self.lock = None
                raise OwnerBusy('This hub already has a workflow engine owner')
            self.lock.commit()
        try:
            with self.engine.begin() as conn:
                conn.execute(text('INSERT INTO hz_workflow_owner (hub,boot,generation,expires,ready) VALUES (:h,:b,1,:e,:r) ON CONFLICT (hub) DO UPDATE SET boot=:b,generation=hz_workflow_owner.generation+1,expires=:e,ready=:r'), {'h': self.hub, 'b': self.boot, 'e': time.time()+90, 'r': '{}'})
                self.generation = conn.execute(text('SELECT generation FROM hz_workflow_owner WHERE hub=:h'), {'h': self.hub}).scalar_one()
        except BaseException:
            self.close()
            raise
        return self

    def assert_owned(self):
        if self.lost or self.lock is None:
            raise RuntimeError('Workflow engine ownership was lost; restart the bridge')
        # Never transparently reconnect a lost advisory-lock session.
        if self.engine.dialect.name == 'postgresql':
            with self._mutex:
                if self.lock.invalidated:
                    self.lost = True
                    raise RuntimeError('Workflow owner database connection was lost')
                try:
                    self.lock.execute(text('SELECT 1'))
                    self.lock.commit()
                except Exception:
                    self.lost = True
                    raise
        with self.engine.connect() as conn:
            row = conn.execute(text('SELECT boot,generation FROM hz_workflow_owner WHERE hub=:h'), {'h': self.hub}).first()
        if not row or row.boot != self.boot or row.generation != self.generation:
            self.lost = True
            raise RuntimeError('Workflow owner fencing token is stale')

    def heartbeat(self, ready=None):
        self.assert_owned()
        if ready is not None:
            self.ready = ready
        with self.engine.begin() as conn:
            changed = conn.execute(text('UPDATE hz_workflow_owner SET expires=:e,ready=:r WHERE hub=:h AND boot=:b AND generation=:g'), {'e': time.time()+90, 'r': json.dumps(self.ready), 'h': self.hub, 'b': self.boot, 'g': self.generation}).rowcount
        if not changed:
            self.lost = True
            raise RuntimeError('Workflow owner fencing token is stale')

    def close(self):
        if self.lock is None:
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(text('UPDATE hz_workflow_owner SET expires=0,ready=:r WHERE hub=:h AND boot=:b'), {'r': '{}', 'h': self.hub, 'b': self.boot})
        finally:
            if self.engine.dialect.name == 'postgresql':
                try:
                    self.lock.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': self._key})
                    self.lock.commit()
                finally:
                    self.lock.close()
            else:
                self.lock.close()
            self.lock = None


def readiness(hub_dir, hub):
    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, 'operational')
    with engine.connect() as conn:
        row = conn.execute(text('SELECT boot,generation,expires,ready FROM hz_workflow_owner WHERE hub=:h'), {'h': hub}).mappings().first()
    if not row or row['expires'] <= time.time():
        return None
    data = json.loads(row['ready'])
    return {**data, 'boot': row['boot'], 'generation': row['generation']} if data else None
