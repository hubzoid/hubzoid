"""Telegram enrollment: a shared contact binds the numeric id to a roster row.

- verified when the shared phone is in the roster (and it's the sender's OWN
  contact — user_id must equal the sender),
- not_registered when the phone isn't in the roster,
- not_own_contact when they shared someone else's number.
After binding, a later message from that numeric id resolves via the stored phone.
No LLM anywhere in here.
"""
from hubzoid.telegram.enrollment import Bindings, enroll_contact, resolve_telegram


def _roster(mapping):
    return lambda surface, handle: mapping.get(handle)


def test_enroll_verified_when_phone_in_roster(tmp_path):
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": ["coordinator"]}})
    b = Bindings(tmp_path)
    r = enroll_contact(handle="42", contact_user_id="42",
                       contact_phone="+91 98000-00001", resolver=resolver, bindings=b)
    assert r.status == "verified"
    assert r.email == "ravi@isha.org"
    assert b.phone_for("42") == "919800000001"


def test_enroll_not_registered_when_phone_absent(tmp_path):
    resolver = _roster({})
    r = enroll_contact(handle="42", contact_user_id="42", contact_phone="919999999999",
                       resolver=resolver, bindings=Bindings(tmp_path))
    assert r.status == "not_registered"


def test_enroll_rejects_someone_elses_contact(tmp_path):
    resolver = _roster({"919800000001": {"email": "x@isha.org"}})
    r = enroll_contact(handle="42", contact_user_id="99", contact_phone="919800000001",
                       resolver=resolver, bindings=Bindings(tmp_path))
    assert r.status == "not_own_contact"


def test_resolve_telegram_after_binding(tmp_path):
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})
    b = Bindings(tmp_path)
    b.bind("42", "919800000001")
    assert resolve_telegram("42", resolver, b)["email"] == "ravi@isha.org"


def test_resolve_telegram_unbound_is_none(tmp_path):
    resolver = _roster({"919800000001": {"email": "x@isha.org"}})
    assert resolve_telegram("42", resolver, Bindings(tmp_path)) is None


def test_binding_persists_across_instances(tmp_path):
    Bindings(tmp_path).bind("42", "919800000001")
    assert Bindings(tmp_path).phone_for("42") == "919800000001"
