# VENDOR

Vendor trees shipped inside this kit. Do not invent pinned SHAs — missing pins are blockers for release packaging.

## `compose/bsl-indexer/`

| Field | Value |
|-------|--------|
| Upstream | https://github.com/Regsorm/code-index-mcp |
| Role | BSL / code-index MCP + daemon; build context for `ai2-bsl-*` images |
| Local pin (commit/tag) | **UNKNOWN** — tree is a vendored copy without `.git`; resolve before release packaging |
| Local patches | Tracked only as diffs in this repo; prefer upstream PR when possible |
| Update method | Compare against upstream tag/release; replace tree; rebuild `ai2-bsl-indexer:local`; smoke MCP `:8011`/`:8211`/`:8311` |

## `compose/mcp_for_1c/`

| Field | Value |
|-------|--------|
| Upstream (historical stand reference) | `https://github.com/shultzMAN87/mcp_for-1C.git` (from prior UBD deploy notes / `GraphStackGitUrl`) |
| Role | Metadata indexer, graph MCP, query-builder; Neo4j-facing jobs |
| Local pin (commit/tag) | **UNKNOWN** — vendored `1c-mcp-suite` without verified upstream SHA in-kit |
| Local patches | AI-Contour overlays live under `deploy-ai2/mcp_for_1c-*`, not silent edits to vendor when avoidable |
| Update method | Diff against known upstream commit; rebuild `ai2-metadata-indexer` / related images; smoke graph MCP |

## `compose/ai-stack/`

| Field | Value |
|-------|--------|
| Role | Template for Open WebUI + OmniRoute (not necessarily bit-identical to live `deploy-ai2/ai-stack`) |
| Pin | Follow image tags in compose / `.env.example`; live stand may pin differently |

## Policy

1. Never commit filled `.env` or connection JSON.  
2. Prefer overlays in `deploy-ai2/` over rewriting vendor sources.  
3. Before a “release kit” tarball: fill **Local pin** rows or list them under `release-manifest.json` → `missing`.
