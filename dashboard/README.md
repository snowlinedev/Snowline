# Snowline dashboard

The platform-owned UI shell (spec: `../docs/specs/ui-shell.md`). Served by the
platform app at `/ui` from the built bundle in `dist/`; plugins contribute
widgets/pages declaratively (phase 2) — they never ship JavaScript here.

## Requirements

- Node **20+** (see `engines`; `tests/setup.ts` works around Node 22+'s
  experimental `localStorage` global).

## Commands

```bash
npm install
npm run dev     # Vite dev server; proxies /plugins,/scopes,/surfaces to the
                # platform on 127.0.0.1:8850 (start the platform first)
npm test        # token contrast validation + vitest (axe, keyboard)
npm run build   # the deploy gate: validate tokens → tsc -b → vite build
```

Deploy = `npm run build`, then kickstart the platform LaunchAgent — the
platform serves `dist/` directly (override the location with
`SNOWLINE_DASHBOARD_DIST`). Always verify with `npm run build`, not
`tsc --noEmit` — the build is stricter.

## Accessibility

`ACCESSIBILITY.md` is the governed conformance statement (WCAG 2.2 AA by
default; compact density relaxes target-size only). It is a registered
Snowline governance artifact — change it via governance revision, not a
drive-by edit.
