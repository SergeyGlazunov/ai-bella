# Working with this 1C configuration

Guidance for AI agents. Skills markdown: `integrations/skills/webui/` (Open WebUI)
and `integrations/skills/cursor/`. Prefer the matching skill before improvising.

## Install set (all required)

| Tool | Role | Skill (webui) |
|---|---|---|
| Graph | metadata + call graph of **this** config | `ubd-graph` (и аналоги для КБ) |
| Code_index | BSL bodies, grep, file text | `code-index` |
| query-builder | build / validate / optimize queries | `query-builder` |
| MDD | owners / portal properties | `mdd` |
| SDMS | tasks / sprints (read-only) | `sdms` |
| Confluence | wiki (read-only) | `confluence` |

Do not invent object names — look them up in Graph. Do not invent BSL bodies —
open via Code_index after Graph gives file + lines. Do not use MDD for structure
or Graph for ownership.

## Graph (`metadata-*` / `code-*`)

- `metadata_search`, `metadata_object_attributes`, `metadata_referrers`, …
- `code_callers`, `code_callees`, `code_method_signature`, `code_call_path`, …

Answers **this configuration**. Not platform syntax, not MDD owners.

## query-builder

- `query_fields`, `query_join_hint`, `query_build`, `query_validate`, `query_optimize`

Uses the same Neo4j metadata layer as Graph. Table names like
`Справочник.Номенклатура`. Always run `query_validate`; treat `errors` vs
`warnings` differently (errors = fix; warnings = review).

## Code_index

Bodies and snippets after Graph located the procedure. Do not start with a
whole-repo grep when Graph can narrow the path.

## MDD / SDMS / Confluence

Separate MCPs. Read-only where the skill says so. «Мои задачи» in SDMS = the
JWT owner on the VM, not necessarily the chat user.

## answerable / degraded

If `answerable: false`, do not invent an answer. Empty list ≠ “object missing”
when the graph is empty or Neo4j is down — check `metadata_stats` / meaning.
