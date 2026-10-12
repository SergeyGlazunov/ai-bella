"""
Клиентские конфиги (после удаления OpenCode).

Проверяем: если есть `.cursor/mcp.json`, в нём перечислены порты живых
MCP из docker-compose.yml. opencode-config.json больше не канон.

Запуск:  python scripts/tests_client_configs.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

for _stream in __import__("sys").stdout, __import__("sys").stderr:
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent

# Порты MCP в base compose (vendor suite). На ai2 часть в unused — файл
# всё равно описывает стек.
SERVER_PORTS = (8001, 8002, 8003, 8009, 8765)


def ports_in(path: Path) -> set[int]:
    text = path.read_text(encoding="utf-8")
    return {int(p) for p in re.findall(r"://[^\"'\s]+?:(\d{4,5})/", text)}


def compose_published_ports() -> set[int]:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    found = set()
    for m in re.finditer(r'"(?:127\.0\.0\.1:|0\.0\.0\.0:)?(\d{4,5}):\d{4,5}"', text):
        found.add(int(m.group(1)))
    return found


class TestCursorConfigOptional(unittest.TestCase):
    def setUp(self):
        self.cursor = ROOT / ".cursor" / "mcp.json"
        if not self.cursor.exists():
            self.skipTest(".cursor/mcp.json нет — проверять нечего")

    def test_known_servers_present_when_cursor_config_exists(self):
        missing = sorted(set(SERVER_PORTS) - ports_in(self.cursor))
        self.assertFalse(
            missing,
            f"mcp.json: не хватает серверов на портах {missing}",
        )


class TestComposePublishesKnownPorts(unittest.TestCase):
    def test_server_ports_match_compose(self):
        published = compose_published_ports()
        missing = sorted(set(SERVER_PORTS) - published)
        self.assertFalse(
            missing,
            f"SERVER_PORTS числит порты {missing}, которых compose не публикует",
        )

    def test_opencode_service_removed(self):
        text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotRegex(
            text,
            r"(?m)^\s+opencode:\s*$",
            "сервис opencode должен быть удалён из docker-compose.yml",
        )
        self.assertFalse(
            (ROOT / "opencode-config.json").exists(),
            "opencode-config.json должен быть удалён",
        )


class TestNoDeadServerReferences(unittest.TestCase):
    DEAD = ("mcp-code-rag", "mcp-rest-proxy", "mcp-sonarqube",
            "mcp-naparnik", "mcp-code-templates", "mcp-testing")
    HISTORY = ("PLAN", "ИТОГИ", "README-", "docs/archive")

    def test_live_code_does_not_reference_deleted_servers(self):
        offenders = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()
            if any(h in rel for h in self.HISTORY):
                continue
            if path.suffix not in (".py", ".yml", ".yaml", ".json", ".example"):
                continue
            if "__pycache__" in rel or rel.startswith("v8std-data/"):
                continue
            if path.name == Path(__file__).name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for dead in self.DEAD:
                for i, line in enumerate(text.splitlines(), 1):
                    if dead not in line:
                        continue
                    if any(w in line for w in ("больше нет", "удалён",
                                               "HYG-4", "по образцу")):
                        continue
                    offenders.append(f"{rel}:{i}: {dead}")
        self.assertFalse(
            offenders,
            "упоминания удалённых серверов в рабочих файлах:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
