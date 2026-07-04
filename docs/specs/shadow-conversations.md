# Shadow conversations — conversing in a branch from the UI

> **Status: draft** (designed 2026-07-03, Sean's ask: "can we have a
> conversation in the UI for a shadow conversation"). Two phases: the UI
> write seam + a durable per-branch conversation log (phase 1), then
> server-side agent turns on the codex inference carrier (phase 2). This spec
> ACTIVATES two seams `ui-shell.md` deliberately reserved — §4.3 (the
> declarative write path) and §5 ("POST reserved for actions") — and the
> `shadow_conversation_events` table that shipped dormant with the shadow
> graph ("the conversation/turn machinery is a later PR").

## 1. Purpose

The shadow-branch thread page is the re-entry surface for a speculation line,
but today it is read-only: continuing a discussion means opening a Claude Code
session. The goal is to continue it from the dashboard — phone over tailnet
included:

- **Phase 1 (human writes):** a composer on the thread page appends your
  message to the branch's durable conversation log. Useful alone — the next
  agent session picks the discussion up (shadow briefing delta already
  watermarks activity).
- **Phase 2 (agent replies):** a governance-side turn-runner notices an
  unanswered message, gathers branch context, runs inference via the codex
  carrier (decision `4bc92633`), and appends the reply — the conversation
  outlives any client connection, exactly what the event log's design
  anticipated.

## 2. Data model (already shipped)

`shadow_conversation_events` (governance, `models.py`): append-only, one row
per event, per-branch monotonic `seq` (doubles as a resume cursor), untyped
JSONB `payload`, `kind` string, cascades with the branch. Isolated by
construction like all shadow tables — nothing real references it.

Event kinds used by this spec (the column is unschematized; these are the
payload conventions):

| `kind` | payload | author |
|---|---|---|
| `message` | `{ "author": "human" \| "agent", "markdown": str }` | both phases |
| `agent.error` | `{ "error": str }` | phase 2 — a failed turn is VISIBLE in the thread (fail-visible, ui-shell §4.4), never silent |

`seq` allocation: `SELECT ... FOR UPDATE` on the branch row, then
`max(seq)+1` — the branch row is the natural serialization point and turns
are rare; no advisory-lock machinery.

## 3. Phase 1a — platform: the `/ui-api` POST proxy

`ui-shell.md` §5 grows the reserved verb:

```
POST /ui-api/<plugin>/<path>   →   <plugin base_url><path>
```

- **Endpoint allowlist, structural:** only paths a plugin DECLARED in its
  manifest `ui` block as a write endpoint (`composer.endpoint`, and later
  `actions[].endpoint`) are POSTable. Matching is per path-segment with
  `{param}` template segments matching exactly one segment — same posture as
  the GET data-path allowlist, so the proxy can never be aimed at an
  undeclared route, another plugin, or an MCP surface.
- **Body:** JSON only, size-capped (64 KiB — a conversation message, not an
  upload). The proxy forwards verbatim and never interprets.
- **Health-aware:** DOWN plugin short-circuits to 503, same as GET.
- SDK `ui.py` schema + drift guard grow the `composer` shape (§4);
  registration validation (422 on malformed) extends to it.

## 4. Phase 1b — the `composer` declaration + shell rendering

The `thread` page kind gains an optional **`composer`** — input-shaped, so it
is NOT an §4.3 `action` (those are button-shaped with confirm semantics; both
ride the same proxy-POST enablement):

```jsonc
{
  "id": "shadow-branch",
  "kind": "thread",
  "data": "/ui-api/pages/branches/{branch}",
  "composer": {
    "endpoint": "/ui-api/pages/branches/{branch}/messages",  // POST target
    "placeholder": "Reply in this branch…",
    "disabled_when": "archived"   // thread `flags` entry (§5) that greys it out
  }
}
```

Shell (dashboard `thread` component):

- Renders a markdown textarea + send button at the thread foot when
  `composer` is declared. Send POSTs `{ "markdown": ... }` through the §3
  proxy, then refetches the page data (no optimistic append in v1 — one
  source of truth).
- **Polling:** the thread page refetches on a fixed cadence while visible
  (reuse the widget `refresh_seconds` mechanic, default 5 s for `thread`
  pages carrying a composer, pause when the tab is hidden) so phase-2 agent
  replies appear without a manual reload. SSE over the `seq` cursor is the
  designed upgrade, **deferred** until polling visibly hurts.
- **Accessibility:** the composer is part of the kind vocabulary, so it lands
  in the axe-core CI matrix (both densities, both themes); labelled control,
  keyboard submit (Cmd/Ctrl-Enter), visible focus, disabled state announced.
  `ACCESSIBILITY.md` conformance claim is unchanged (no new relaxation).

## 5. Phase 1c — governance: conversation routes

- **`POST /ui-api/pages/branches/{branch_id}/messages`** — body
  `{ "markdown": str }` (non-blank, size-capped to match the proxy). Appends
  a `message` event with `author: "human"`. 404 unknown branch, 409 archived
  branch (the composer should already be disabled — `disabled_when` — but the
  plugin owns the semantics, ui-shell §4.3). Response: the appended event
  (with `seq`), so a future shell can render receipts.
- **Thread page GET** (`/ui-api/pages/branches/{branch_id}`) — conversation
  events MERGE into `nodes` chronologically with the shadow nodes (narrative
  notes stay first): `{ author: "you" | "agent", kind: "message", markdown,
  at }`. The thread response gains a top-level `flags` list carrying the
  branch status flag the composer's
  `disabled_when` keys on. One page still tells the whole story — no second
  kind, no second route.
- MCP parity: the same events are readable from the shadow MCP surface
  (extend `get_branch`'s dict with the conversation tail) so a Claude Code
  session re-entering the branch sees what was said in the UI. Write parity
  (an MCP `add_message` verb) is **included** — trivially the same service
  function, and agent sessions should log conversational turns the same way.

## 6. Phase 2 — the turn-runner (codex carrier)

A background loop in governance (rides the lifespan task group beside
`webhook_delivery_loop`; OFF in tests, gated by env):

- **Pending turn:** a branch whose LAST conversation event is a `message` with
  `author: "human"` and no in-flight claim. Claim = an in-process set +
  a stale-claim timeout (a crashed turn un-claims after
  `SNOWLINE_SHADOW_TURN_TIMEOUT`, default 300 s); one turn per branch at a
  time, `SNOWLINE_SHADOW_TURN_BATCH` (default 1) pending branches are
  drained per tick, processed SEQUENTIALLY (true parallel turns are a
  deliberate non-feature until a real backlog exists).
  **Answered semantics** (a choice this spec left open, fixed by #71): a branch
  is pending ONLY when its LAST event is a human `message`. A delivered agent
  `message` OR an `agent.error` (a failed turn) as the last event ANSWERS the
  turn — the human must post a fresh message to retry. So a failed turn is
  terminal, never auto-retried, and a persistently-failing turn can't hot-loop.
- **Context assembly:** branch name/scope/narrative notes, its nodes +
  citations, the conversation log tail, and the scope's applicable decisions
  (the grounding that makes a shadow reply worth having). Clamped to a
  context budget; oldest conversation turns drop first.
- **Inference:** `codex exec` as a subprocess (flat-rate sub, decision
  `4bc92633` — plugins MAY embed inference; this is a CARRIER loop, so the
  "no LLM in MCP handlers" line holds). Prompt frames the SHADOW posture:
  speculative co-thinker inside an isolated branch — it can propose, refute,
  cite; it cannot touch the real graph (and structurally couldn't: the runner
  only appends events). Reply is appended as a `message` event with
  `author: "agent"`; a failed/timed-out turn appends `agent.error` so the
  thread SHOWS the failure.
- **Env:** `SNOWLINE_SHADOW_TURNS_ENABLED` (default false),
  `SNOWLINE_SHADOW_TURN_POLL_SECONDS` (default 5),
  `_TIMEOUT`, `_BATCH`, `SNOWLINE_SHADOW_TURN_MODEL` (passed to codex).
- **Security posture (documented, not solved):** the prompt contains
  verbatim branch content, and `--sandbox read-only` restricts codex's
  WRITES, not reads — so a prompt-injected turn could read host files and
  leak them into its logged reply (the log IS the only exfil channel; the
  sandbox blocks network). Accepted under the current trust model: a
  single-operator tailnet where everyone who can post a branch message
  already has host-level access. The runner uses an empty scratch cwd as
  casual-context hygiene. REVISIT TRIGGER: any multi-user or off-host
  message source (e.g. exposing the composer beyond the tailnet).
- **Out of scope for the first turn-runner:** the agent driving shadow MCP
  verbs (adding nodes/citations from inside a turn). The reply is markdown
  into the log; crystallizing a discussion into nodes stays with interactive
  sessions until the plain loop proves out. Graduation flows are untouched.

## 7. Acceptance

1. From the dashboard (localhost AND tailnet IP), open a shadow branch, send
   a message: it renders in the thread after refetch, and
   `shadow_conversation_events` holds it with the right `seq`.
2. Archived branch: composer greyed, direct POST 409s.
3. A POST to an undeclared plugin path through the proxy 403s; oversize body
   413s; DOWN plugin 503s.
4. With turns enabled and codex on PATH: a human message gets an agent reply
   appended within poll+turn time, visible in the UI without reload (thread
   polling); killing codex mid-turn yields an `agent.error` event, not a
   stuck claim.
5. axe-core matrix green with the composer rendered, both densities/themes.
6. A Claude Code session reading the branch over shadow MCP sees the same
   conversation the UI shows.

## 8. Build order

1. **#1 platform+SDK:** proxy POST + endpoint allowlist + `composer` schema.
2. **#2 dashboard:** thread composer + thread polling + a11y coverage.
3. **#3 governance:** message route + thread merge + MCP parity (phase 1
   lands usable here).
4. **#4 governance:** turn-runner on codex (phase 2).
