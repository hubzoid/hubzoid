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
