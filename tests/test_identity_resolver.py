"""The identity resolver: turn a surface handle (phone) into {email, groups}.

Presence-activated like `restricted/`: a hub with no `identity/` folder has no
resolver (everyone fail-closed on webhook surfaces). `identity/access.csv` is
no longer read. Email is the key; unknown handle -> None.
"""
import textwrap

from hubzoid.access.resolver import load_resolver


def _write(hub, rel, content):
    p = hub / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))
    return p


def test_console_phone_numbers_resolve_first(tmp_path, monkeypatch):
    """A number recorded on a person in the Console is that person; anyone else
    is asked of the hub's own roster. A blocked person is nobody."""
    import pytest

    from hubzoid.access import store_for
    from hubzoid.access.store import PhoneTaken

    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001") is None          # no roster, no number yet
    gs = store_for(tmp_path)
    assert gs.set_phone("Ana@Example.org", "+91 98000-00001", actor="t") == "919800000001"
    assert resolve("whatsapp", "919800000001") == {"email": "ana@example.org", "groups": []}
    assert resolve("telegram", "+919800000001")["email"] == "ana@example.org"
    with pytest.raises(PhoneTaken):
        gs.set_phone("ben@example.org", "919800000001", actor="t")
    with pytest.raises(ValueError):
        gs.set_phone("ben@example.org", "12", actor="t")
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,someone@example.org,coordinator
        919800000002,ravi@example.org,coordinator
    """)
    resolve = load_resolver(tmp_path)
    got = resolve("whatsapp", "919800000001")                   # the Console is the one home
    assert got == {"email": "ana@example.org", "groups": []}
    assert resolve("whatsapp", "919800000002") is None          # access.csv is no longer read
    gs.suspend("ana@example.org", actor="t")
    assert resolve("whatsapp", "919800000001") is None
    gs.set_phone("ana@example.org", "", actor="t")
    assert gs.subject_for_phone("919800000001") is None


def test_function_backing_wins_over_table(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email
        919800000001,table@example.org
    """)
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return {"email": "function@example.org", "groups": ["coordinator"]}
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["email"] == "function@example.org"


def test_function_backing_none_is_fail_closed(tmp_path):
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return None
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("telegram", "123") is None


def test_function_receives_surface_and_handle(tmp_path):
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return {"email": f"{surface}-{handle}@example.org"}
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("telegram", "123")["email"] == "telegram-123@example.org"


# ---------------------------------------------------------------------------
# Email-keyed lookup, reload-on-edit, and the .py opt-in (unify-access, 0.8.1)
# ---------------------------------------------------------------------------
def test_py_backing_ignoring_args_is_never_handed_an_email(tmp_path):
    # Legacy access.py that defines only resolve() and ignores its arguments:
    # it must NOT leak its fixed group to every OWUI email.
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return {"email": "fixed@example.org", "groups": ["coordinator"]}
    """)
    r = load_resolver(tmp_path)
    assert r("whatsapp", "919800000001")["groups"] == ["coordinator"]  # phone path unchanged
    assert r.groups_for_email("anyone@example.org") == []  # email path contributes nothing


def test_py_backing_opts_in_with_groups_for_email(tmp_path):
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return None
        def groups_for_email(email):
            return ["coordinator"] if email == "ravi@example.org" else []
    """)
    r = load_resolver(tmp_path)
    assert r.groups_for_email("ravi@example.org") == ["coordinator"]
    assert r.groups_for_email("other@example.org") == []
