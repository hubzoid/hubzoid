from pathlib import Path

from hubzoid._fs import resolve_bucket


def _make(tmp_path: Path, names: list[str]) -> Path:
    for n in names:
        (tmp_path / n).mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_lowercase_plural(tmp_path):
    hub = _make(tmp_path, ["skills"])
    assert resolve_bucket(hub, "skills").name == "skills"


def test_capitalized(tmp_path):
    hub = _make(tmp_path, ["Skills"])
    assert resolve_bucket(hub, "skills").name == "Skills"


def test_singular(tmp_path):
    hub = _make(tmp_path, ["skill"])
    assert resolve_bucket(hub, "skills").name == "skill"


def test_missing_returns_none(tmp_path):
    assert resolve_bucket(tmp_path, "skills") is None


def test_multiple_picks_alphabetical_and_warns(tmp_path, caplog):
    hub = _make(tmp_path, ["Skills", "skill"])
    with caplog.at_level("WARNING"):
        chosen = resolve_bucket(hub, "skills")
    assert chosen.name in {"Skills", "skill"}
    assert "multiple folders match" in caplog.text


def test_raw_data_lowercase(tmp_path):
    hub = _make(tmp_path, ["raw_data"])
    assert resolve_bucket(hub, "raw_data").name == "raw_data"


def test_raw_data_hyphen(tmp_path):
    hub = _make(tmp_path, ["raw-data"])
    assert resolve_bucket(hub, "raw_data").name == "raw-data"


def test_raw_data_runtogether(tmp_path):
    hub = _make(tmp_path, ["rawdata"])
    assert resolve_bucket(hub, "raw_data").name == "rawdata"


def test_unknown_bucket_raises(tmp_path):
    try:
        resolve_bucket(tmp_path, "not-a-bucket")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
