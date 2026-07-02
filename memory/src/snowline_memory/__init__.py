"""Snowline memory plugin.

Cross-folder, cross-machine agent SESSION MEMORY — the durable working context a
session needs to be productive (conventions, gotchas, user preferences, current
focus, references) — exposed as a registered MCP-server plugin over its OWN
database. Where governance is the record of ratified REASONING (decisions, a
supersession graph, inheritance), memory is the flat, scope-tagged, upsert-in-place
store of WORKING CONTEXT. A memory that hardens into policy graduates to a
`record_decision` on the governance surface; it does not live in memory forever
(memory-plugin spec §3).

Import-purity rule (architecture §2 dependency direction, mirroring governance):
`snowline_memory` imports NO monolith code (`snowline_server` /
`snowline_substrate`) and NO platform internals (`snowline_platform`). It depends
on the platform only over HTTP (the plugin registration endpoint). Scopes are
platform-owned; memory stores a scope slug as a SOFT, optional reference — never
resolved to a platform id, never a join.
"""
