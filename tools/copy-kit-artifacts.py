#!/usr/bin/env python3
"""Vendor non-secret kit artifacts, including image build trees.

Does not copy .env, .git, secrets, Cargo target/, or proprietary 1C help dumps.
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_AI_STACK = Path(r"C:\ai-stack")
SRC_GRAPH = Path(r"C:\MCP\mcp_for_1c")
SRC_BSL_REPO = Path(r"C:\MCP\code_index\repo")
SRC_SA = Path(r"C:\instructions-main-system_analytics\system_analytics")

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "target",
    "target2",
    ".cursor",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "v8std-data",
}


def _ignore(directory: str, names: list[str]) -> set[str]:
    skipped: set[str] = set()
    for name in names:
        if name in SKIP_DIRS:
            skipped.add(name)
            continue
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            skipped.add(name)
            continue
        if name.endswith(".lic") or name.endswith(".db"):
            skipped.add(name)
    parent = Path(directory).name.lower()
    if parent == "platform-help-data":
        skipped.update(
            n for n in names if n not in {".gitkeep", "README.md", "README"}
        )
    if parent == "workspace":
        skipped.update(
            n for n in names if n not in {".gitkeep", "AGENTS.md"}
        )
    return skipped


def copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=_ignore, dirs_exist_ok=False)
    print(f"copied tree {src} -> {dest}")


def copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"copied {src} -> {dest}")


def copy_tree_md(src: Path, dest: Path) -> int:
    count = 0
    if not src.is_dir():
        print(f"missing {src}")
        return 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".txt"}:
            continue
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def write_bsl_kit_overlay(dest: Path) -> None:
    compose = dest / "docker-compose.yml"
    compose.write_text(
        """name: bsl-indexer
services:
  bsl-indexer:
    build:
      context: .
      dockerfile: deploy/docker/Dockerfile
    image: bsl-indexer:local
    container_name: bsl-indexer
    environment:
      CODE_INDEX_HOME: /data/code-index-home
      MCP_HTTP_HOST: 0.0.0.0
      MCP_HTTP_PORT: "9003"
    volumes:
      - /srv/ai/ubd/src:/repos/SRC
      - bsl-indexer-home:/data/code-index-home
      - ./deploy/docker/daemon.toml:/data/code-index-home/daemon.toml:ro
    ports:
      - "8011:9003"
    restart: unless-stopped

volumes:
  bsl-indexer-home:
""",
        encoding="utf-8",
    )
    daemon = dest / "deploy" / "docker" / "daemon.toml"
    daemon.parent.mkdir(parents=True, exist_ok=True)
    daemon.write_text(
        """[daemon]
http_host = "127.0.0.1"
http_port = 0
log_level = "info"

[[paths]]
path = "/repos/SRC"
alias = "ubd"
language = "bsl"
""",
        encoding="utf-8",
    )
    print(f"wrote kit overlay {compose} and {daemon}")


def main() -> None:
    if SRC_AI_STACK.joinpath("docker-compose.yml").is_file():
        copy_file(
            SRC_AI_STACK / "docker-compose.yml",
            ROOT / "compose" / "ai-stack" / "docker-compose.yml",
        )
    if SRC_AI_STACK.joinpath(".env.example").is_file():
        copy_file(
            SRC_AI_STACK / ".env.example",
            ROOT / "compose" / "ai-stack" / ".env.example",
        )
    if not SRC_GRAPH.is_dir():
        raise SystemExit(f"missing graph source {SRC_GRAPH}")
    copy_tree(SRC_GRAPH, ROOT / "compose" / "mcp_for_1c")
    if not SRC_BSL_REPO.is_dir():
        raise SystemExit(f"missing bsl source {SRC_BSL_REPO}")
    copy_tree(SRC_BSL_REPO, ROOT / "compose" / "bsl-indexer")
    write_bsl_kit_overlay(ROOT / "compose" / "bsl-indexer")
    n_webui = copy_tree_md(
        SRC_SA / "docs" / "webui" / "skills",
        ROOT / "integrations" / "skills" / "webui",
    )
    n_cursor = copy_tree_md(
        SRC_SA / "docs" / "cursor" / "skills",
        ROOT / "integrations" / "skills" / "cursor",
    )
    print(f"skills webui={n_webui} cursor={n_cursor}")


if __name__ == "__main__":
    main()
