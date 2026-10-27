"""The manifest version must not drift from what is actually shipped.

This has now gone wrong twice in this project: the Lovelace card sat at
2.0.0 on two machines with different contents, so cache-busting silently did
nothing, and the integration manifest stayed at 0.1.0 through three commits.
Home Assistant and HACS both read the manifest version to decide what is
installed, so a stale one means an upgrade that looks like it worked.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "multisplit" / "manifest.json"
CHANGELOG = ROOT / "CHANGELOG.md"


def manifest_version() -> str:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]


def changelog_versions() -> list[str]:
    return re.findall(r"^## (\d+\.\d+\.\d+)", CHANGELOG.read_text(encoding="utf-8"), re.M)


def test_the_manifest_declares_a_version():
    """Required for a custom integration — Home Assistant refuses to load
    one without it."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest_version())


def test_the_manifest_matches_the_newest_changelog_entry():
    versions = changelog_versions()
    assert versions, "the changelog has no version headings"
    assert manifest_version() == versions[0], (
        f"manifest says {manifest_version()} but the changelog's newest entry "
        f"is {versions[0]} — bump one of them"
    )


def test_changelog_versions_are_in_descending_order():
    versions = [tuple(int(part) for part in v.split(".")) for v in changelog_versions()]
    assert versions == sorted(versions, reverse=True)


def test_every_changelog_version_is_unique():
    versions = changelog_versions()
    assert len(versions) == len(set(versions))


def test_the_manifest_has_what_hacs_and_home_assistant_require():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
        assert manifest.get(key), f"manifest is missing {key!r}"
    assert manifest["domain"] == "multisplit"
