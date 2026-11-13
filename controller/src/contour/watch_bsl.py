"""BSL reload entrypoints — thin wrappers over ``bsl_ctl`` (no TOML surgery)."""

from __future__ import annotations

from contour.bsl_ctl import reload_bsl


def reload_bsl_daemon(cfg) -> None:
    """Reload bsl-indexer for ``cfg.services.bsl_container`` via docker exec."""
    reload_bsl(cfg.services.bsl_container)
