"""Migration rehearsal against installed OWUI's real schema and model APIs.

Uses synthetic users in a fresh private database, not a customer database.
"""

from __future__ import annotations

import os
import subprocess
import sys

SCRIPT = r"""
import asyncio, json, sys
from pathlib import Path
from alembic import command
from alembic.config import Config
from open_webui.env import OPEN_WEBUI_DIR
cfg = Config(str(OPEN_WEBUI_DIR / 'alembic.ini'))
cfg.set_main_option('script_location', str(OPEN_WEBUI_DIR / 'migrations'))
command.upgrade(cfg, 'head')
from open_webui.models.users import Users
from open_webui.models.groups import Groups, GroupForm
from open_webui.models.models import Models, ModelForm, ModelMeta, ModelParams
from open_webui.models.access_grants import AccessGrants
from open_webui.internal.db import engine, async_engine
from hubzoid.access import migrate
from hubzoid.access.store import GrantStore
from sqlalchemy import create_engine
root = Path(sys.argv[1])

async def main():
    people = [('owner','user'), ('admin','admin'), ('allowed','user'), ('denied','user'), ('pending','pending')]
    for uid, role in people:
        assert await Users.insert_new_user(uid, uid, uid+'@example.com', role=role)
    group = await Groups.insert_new_group('owner', GroupForm(name='ledger', description='test'))
    assert await Groups.add_users_to_group(group.id, ['allowed','denied','pending'])
    original = [dict(principal_type='user', principal_id='allowed', permission='read')]
    model = await Models.insert_new_model(ModelForm(id='finance', name='Finance', meta=ModelMeta(), params=ModelParams(), access_grants=original), 'owner')
    assert model is not None
    other = await Models.insert_new_model(ModelForm(id='other', name='Legacy', meta=ModelMeta(), params=ModelParams(), access_grants=[]), 'owner')
    before = {}
    for uid, role in people:
        visible = role == 'admin' or uid == model.user_id or await AccessGrants.has_access(uid,'model','finance','read')
        before[uid] = visible
    plan = migrate.plan_from_owui(engine, 'finance', model_id='finance', permissions=['ledger'])
    gs = GrantStore(create_engine('sqlite:///'+str(root/'operational.db')))
    snapshot = gs.snapshot(['finance'])
    migrate.apply(gs, plan)
    for uid, role in people:
        assert gs.can(uid+'@example.com','finance','use_hub') == before[uid], (uid, before)
        expected_tool = before[uid] and uid in ('allowed','denied','pending')
        assert gs.can(uid+'@example.com','finance','ledger') == expected_tool
    assert not gs.is_authoritative('other')
    projected = [dict(principal_type='user', principal_id=uid, permission='read') for uid,role in people if role != 'pending' and gs.can(uid+'@example.com','finance','use_hub')]
    assert await Models.update_model_by_id('finance', ModelForm(id='finance', name='Finance', meta=ModelMeta(), params=ModelParams(), access_grants=projected))
    gs.restore(snapshot, actor='rehearsal')
    restored = await Models.update_model_by_id('finance', ModelForm(id='finance', name='Finance', meta=ModelMeta(), params=ModelParams(), access_grants=plan.visibility_backup['access_grants']))
    assert restored is not None
    for uid, role in people:
        visible = role == 'admin' or uid == restored.user_id or await AccessGrants.has_access(uid,'model','finance','read')
        assert visible == before[uid]
    assert not (await Models.get_model_by_id('other')).access_grants
    await async_engine.dispose()
    print('OWUI_REHEARSAL_OK')
asyncio.run(main())
"""


def test_real_owui_schema_migration_and_rollback(tmp_path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DATABASE_URL", "DATA_DIR", "HUBZOID_DEPLOYMENT")
    }
    env.update(
        DATA_DIR=str(tmp_path / "owui"),
        DATABASE_URL="sqlite:///" + str(tmp_path / "webui.db"),
        ENABLE_DB_MIGRATIONS="false",
        OFFLINE_MODE="true",
        HF_HUB_OFFLINE="1",
        WEBUI_SECRET_KEY="synthetic-rehearsal-only",
        ENABLE_VERSION_UPDATE_CHECK="false",
    )
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr[-8000:]
    assert "OWUI_REHEARSAL_OK" in result.stdout
