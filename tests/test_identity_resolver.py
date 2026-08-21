"""The identity resolver: turn a surface handle (phone) into {email, groups}.

Presence-activated like `restricted/`: a hub with no `identity/` folder has no
resolver (everyone fail-closed on webhook surfaces). A `.py` function backing
wins over a `.csv` table. Email is the key; unknown handle -> None.
"""
import textwrap

from hubzoid.access.resolver import load_resolver


def _write(hub, rel, content):
    p = hub / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))
    return p


def test_no_identity_folder_means_no_resolver(tmp_path):
    assert load_resolver(tmp_path) is None


def test_table_resolves_phone_to_email_and_groups(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    resolve = load_resolver(tmp_path)
    assert resolve is not None
    got = resolve("whatsapp", "919800000001")
    assert got["email"] == "ravi@isha.org"
    assert got["groups"] == ["coordinator"]


def test_table_normalizes_phone_on_both_sides(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email
        +91 98000-00001,ravi@isha.org
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["email"] == "ravi@isha.org"


def test_table_headers_are_case_insensitive_and_trimmed(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        Phone , Email , Groups
        919800000001,ravi@isha.org,coordinator
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["email"] == "ravi@isha.org"


def test_email_is_lowercased(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email
        919800000001,Ravi@Isha.org
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["email"] == "ravi@isha.org"


def test_groups_split_on_semicolon(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator;curator
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["groups"] == ["coordinator", "curator"]


def test_unknown_phone_is_fail_closed(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email
        919800000001,ravi@isha.org
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "910000000000") is None


def test_blank_rows_are_skipped(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email

        919800000001,ravi@isha.org
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["email"] == "ravi@isha.org"


def test_extra_columns_preserved_as_context(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,center
        919800000001,ravi@isha.org,adyar
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["center"] == "adyar"


def test_function_backing_wins_over_table(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email
        919800000001,table@isha.org
    """)
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return {"email": "function@isha.org", "groups": ["coordinator"]}
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("whatsapp", "919800000001")["email"] == "function@isha.org"


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
            return {"email": f"{surface}-{handle}@isha.org"}
    """)
    resolve = load_resolver(tmp_path)
    assert resolve("telegram", "123")["email"] == "telegram-123@isha.org"


# ---------------------------------------------------------------------------
# Email-keyed lookup, reload-on-edit, and the .py opt-in (unify-access, 0.8.1)
# ---------------------------------------------------------------------------
def test_table_resolves_same_identity_by_phone_and_email(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator;staff
    """)
    r = load_resolver(tmp_path)
    assert r("whatsapp", "919800000001")["email"] == "ravi@isha.org"
    # Same person, reached by the OWUI email, resolves the same groups.
    assert sorted(r.groups_for_email("ravi@isha.org")) == ["coordinator", "staff"]


def test_groups_for_email_is_case_insensitive(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,Ravi@Isha.org,coordinator
    """)
    r = load_resolver(tmp_path)
    assert r.groups_for_email("  RAVI@isha.ORG ") == ["coordinator"]


def test_unknown_email_returns_empty_not_none(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    r = load_resolver(tmp_path)
    assert r.groups_for_email("stranger@isha.org") == []


def test_duplicate_email_across_rows_unions_groups(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
        919800000002,ravi@isha.org,staff
    """)
    r = load_resolver(tmp_path)
    assert sorted(r.groups_for_email("ravi@isha.org")) == ["coordinator", "staff"]


def test_csv_reloads_on_edit_without_restart(tmp_path):
    p = _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    r = load_resolver(tmp_path)
    assert r.groups_for_email("ravi@isha.org") == ["coordinator"]
    # Operator edits the roster in place; next lookup must reflect it.
    p.write_text(textwrap.dedent("""\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator;auditor
    """))
    assert sorted(r.groups_for_email("ravi@isha.org")) == ["auditor", "coordinator"]


def test_corrupt_csv_denies_not_last_good(tmp_path):
    p = _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    r = load_resolver(tmp_path)
    assert r.groups_for_email("ravi@isha.org") == ["coordinator"]
    # A half-saved / unreadable file must fail closed (deny), never keep the
    # old grant alive.
    p.unlink()
    p.mkdir()  # replace the file with a directory -> open() raises OSError
    assert r.groups_for_email("ravi@isha.org") == []


def test_py_backing_ignoring_args_is_never_handed_an_email(tmp_path):
    # Legacy access.py that defines only resolve() and ignores its arguments:
    # it must NOT leak its fixed group to every OWUI email.
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return {"email": "fixed@isha.org", "groups": ["coordinator"]}
    """)
    r = load_resolver(tmp_path)
    assert r("whatsapp", "919800000001")["groups"] == ["coordinator"]  # phone path unchanged
    assert r.groups_for_email("anyone@isha.org") == []  # email path contributes nothing


def test_py_backing_opts_in_with_groups_for_email(tmp_path):
    _write(tmp_path, "identity/access.py", """\
        def resolve(surface, handle):
            return None
        def groups_for_email(email):
            return ["coordinator"] if email == "ravi@isha.org" else []
    """)
    r = load_resolver(tmp_path)
    assert r.groups_for_email("ravi@isha.org") == ["coordinator"]
    assert r.groups_for_email("other@isha.org") == []
