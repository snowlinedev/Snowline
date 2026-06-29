"""Snowline governance plugin.

The flagship capability — Snowline's durable, cross-session memory of reasoning.
This first increment is the **decision graph** (record / supersede / read +
ancestor-inherited applicability) exposed as a registered MCP-server plugin over
its OWN database. Shadow speculation, artifacts, and the decision-event bus land
in later increments (toward issue #5).

Import-purity rule (governance-plugin spec §10, architecture §2 dependency
direction): `snowline_governance` imports NO monolith code (`snowline_server` /
`snowline_substrate`) and NO platform internals (`snowline_platform`). It depends
on the platform only over HTTP (the scope read API + the plugin registration
endpoint). The decision logic here is CARRIED (re-written), not imported, from
the frozen monolith.
"""
