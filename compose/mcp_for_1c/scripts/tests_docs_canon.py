"""
Канон корня compose/mcp_for_1c (после чистки 2026-10-06).

В корне разрешены только README.md и ARCHITECTURE.md (+ LICENSE не .md).
Планы заходов / СТАТУС / ОТКРЫТОЕ / PROMPTS удалены — операционка в docs/.

Запуск:  python scripts/tests_docs_canon.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

CANON = {"README.md", "ARCHITECTURE.md"}
PLAN_RE = re.compile(r"^PLAN(-\d+)?\.md$")
FORBIDDEN_ROOT = {
    "СТАТУС.md",
    "ОТКРЫТОЕ.md",
    "PROMPTS.md",
    "PLAN-10.md",
    "opencode-config.json",
}


class TestCanonListsAgree(unittest.TestCase):
    def script_canon(self) -> set:
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import archive_docs
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"archive_docs.py не импортируется: {e}")
        return {n for n in archive_docs.CANON if n.endswith(".md")}

    def test_the_two_lists_match(self):
        missing = sorted(CANON - self.script_canon())
        self.assertFalse(
            missing,
            f"канон требует эти файлы в корне, а archive_docs.py считает их "
            f"лишними: {missing}",
        )

    def test_script_does_not_keep_extra(self):
        extra = sorted(self.script_canon() - CANON)
        self.assertFalse(
            extra,
            f"archive_docs.py держит в каноне лишнее относительно теста: {extra}",
        )


class TestRootIsCanon(unittest.TestCase):
    def test_only_canon_md_in_root(self):
        md = {p.name for p in ROOT.glob("*.md") if p.is_file()}
        plans = {n for n in md if PLAN_RE.match(n)}
        unexpected = sorted((md - CANON) - plans)
        self.assertFalse(
            unexpected,
            f"в корне лишние .md (не канон и не PLAN*): {unexpected}",
        )

    def test_canon_files_exist(self):
        for name in sorted(CANON):
            self.assertTrue((ROOT / name).is_file(), f"нет {name}")

    def test_forbidden_archive_gone(self):
        present = [n for n in FORBIDDEN_ROOT if (ROOT / n).exists()]
        self.assertFalse(present, f"архивный мусор ещё в корне: {present}")

    def test_opencode_dockerfile_gone(self):
        self.assertFalse(
            (ROOT / "Dockerfile").exists(),
            "корневой Dockerfile OpenCode должен быть удалён",
        )
        self.assertFalse(
            (ROOT / "entrypoint.sh").exists(),
            "корневой entrypoint.sh OpenCode должен быть удалён",
        )

    def test_deploy_docs_exist(self):
        for name in ("РАЗВЁРТЫВАНИЕ.md", "ОШИБКИ-РАЗВЁРТЫВАНИЯ.md"):
            self.assertTrue(
                (ROOT / "docs" / name).is_file(),
                f"нет docs/{name}",
            )


class TestNoStaleCanonLinks(unittest.TestCase):
    """Действующие доки не ссылаются на удалённые файлы как на живые."""

    LIVE_DOCS = ("README.md", "ARCHITECTURE.md", "docs/РАЗВЁРТЫВАНИЕ.md",
                 "docs/ОШИБКИ-РАЗВЁРТЫВАНИЯ.md", "workspace/AGENTS.md")
    DEAD_NAMES = ("СТАТУС.md", "ОТКРЫТОЕ.md", "PROMPTS.md", "PLAN-10.md",
                  "opencode-config.json", "КУДА-КЛАСТЬ")

    def test_live_docs_do_not_point_at_deleted(self):
        offenders = []
        for rel in self.LIVE_DOCS:
            path = ROOT / rel
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for dead in self.DEAD_NAMES:
                if dead in text:
                    # Допустимо только как «удалено / не часть набора»
                    for i, line in enumerate(text.splitlines(), 1):
                        if dead not in line:
                            continue
                        if any(w in line.lower() for w in (
                                "удал", "убран", "не входит", "архив",
                                "не часть")):
                            continue
                        offenders.append(f"{rel}:{i}: {dead}")
        self.assertFalse(
            offenders,
            "живые доки ссылаются на удалённое:\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
