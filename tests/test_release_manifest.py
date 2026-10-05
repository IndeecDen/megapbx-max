from __future__ import annotations

import hashlib
from pathlib import Path


def test_sha256_manifest_matches_release_files() -> None:
    root = Path(__file__).resolve().parents[1]
    entries: dict[str, str] = {}
    for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, marked_path = line.split(" ", 1)
        assert marked_path.startswith("*"), line
        relative_path = marked_path[1:]
        assert relative_path not in entries, relative_path
        entries[relative_path] = digest

    required = {"pyproject.toml", "requirements.txt", "requirements.lock", "LICENSE", "README.md"}
    assert required <= entries.keys()
    for relative_path, expected in entries.items():
        path = root / relative_path
        assert path.is_file(), relative_path
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, relative_path

    source_files = {
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*.py")
    }
    assert source_files <= entries.keys()
