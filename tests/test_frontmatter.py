from hubzoid import frontmatter


def test_split_no_frontmatter():
    fm, body = frontmatter.split("Just body text.\n")
    assert fm == {}
    assert body == "Just body text."


def test_split_with_frontmatter():
    text = "---\nname: foo\ndescription: bar\n---\n\nHello body."
    fm, body = frontmatter.split(text)
    assert fm == {"name": "foo", "description": "bar"}
    assert body == "Hello body."


def test_split_with_list():
    text = "---\nkeywords:\n  - a\n  - b\n---\nbody"
    fm, _ = frontmatter.split(text)
    assert fm["keywords"] == ["a", "b"]


def test_split_unclosed_frontmatter_treated_as_no_frontmatter():
    text = "---\nname: foo\nno close delim"
    fm, body = frontmatter.split(text)
    assert fm == {}
    assert "no close delim" in body


def test_split_invalid_yaml_raises():
    text = "---\nname: foo\n: : bad\n---\nx"
    try:
        frontmatter.split(text)
    except ValueError:
        return
    raise AssertionError("expected ValueError for invalid YAML")
