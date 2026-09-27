"""A fake package index, so the whole CLI can run on real-shaped packages with no network.

`fake_index({"1.0": files, "2.0": files}, dist="name")` replaces `cli.install` with one
that writes `files` (path -> source) into the target directory, plus a dist-info with a
RECORD listing them - exactly what uv leaves behind, which is what the import-name logic
reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blast_radius import cli


def write_version(into: Path, dist: str, version: str, files: dict[str, str]) -> None:
    into.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        path = into / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    info = into / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir(exist_ok=True)
    (info / "RECORD").write_text(
        "".join(f"{rel},sha256=x,1\n" for rel in files) + f"{info.name}/RECORD,,\n",
        encoding="utf-8",
    )


@pytest.fixture
def fake_index(monkeypatch):
    def install_from(versions: dict[str, dict[str, str]], dist: str = "fakepkg"):
        def fake_install(package, version, into, timeout=600.0):
            if version not in versions:
                return False, f"no such version {version}"
            write_version(Path(into), dist, version, versions[version])
            return True, ""

        monkeypatch.setattr(cli, "install", fake_install)

    return install_from
