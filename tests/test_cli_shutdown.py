"""The supervisor must let children close SQLite before container PID 1 exits."""
from __future__ import annotations

import subprocess
import sys

from hubzoid.cli import _stop_processes


def test_shutdown_waits_for_sqlite_cleanup(tmp_path):
    database = tmp_path / 'state.db'
    code = '''
import signal, sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('CREATE TABLE state (value TEXT)')
conn.execute("INSERT INTO state VALUES ('preserved')")
conn.commit()
def stop(*args):
    time.sleep(0.05)
    conn.close()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
print('ready', flush=True)
while True: time.sleep(1)
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(database)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        assert database.with_name('state.db-wal').exists()
        _stop_processes([None, child])
        assert child.returncode == 0
        assert not database.with_name('state.db-wal').exists()
        import sqlite3
        with sqlite3.connect(database) as conn:
            assert conn.execute('SELECT value FROM state').fetchone() == ('preserved',)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()


def test_shutdown_kills_and_reaps_an_unresponsive_child():
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        _stop_processes([child], timeout=0.05)
        assert child.returncode is not None and child.returncode < 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
