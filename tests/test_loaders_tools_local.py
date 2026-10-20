"""tools_local discovery: one broken file fails its own tools, not the hub."""
from __future__ import annotations

from hubzoid.loaders import tools_local


def test_a_file_that_fails_to_import_is_skipped_and_named(tmp_path, caplog):
    folder = tmp_path / "tools_local"
    folder.mkdir()
    (folder / "good.py").write_text(
        "from agents import function_tool\n\n@function_tool\ndef stock(sku: str) -> str:\n"
        "    \"\"\"Stock for a SKU.\"\"\"\n    return sku\n")
    (folder / "broken.py").write_text("import a_package_that_is_not_installed\n")
    tools = tools_local.load_all(tmp_path)
    assert list(tools) == ["stock"]
    assert "tools_local/broken.py was not loaded" in caplog.text
    assert "ModuleNotFoundError" in tools_local.LOAD_ERRORS[str(folder / "broken.py")]
