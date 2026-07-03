# Accessibility

> **This document is a registered Snowline governance artifact** (scope
> `snowlinedev/snowline`, governing the dashboard). The conformance commitments
> below change only through governance revision with recorded rationale —
> never a drive-by edit. Spec: `docs/specs/ui-shell.md` §7.

## Conformance target

The Snowline dashboard's **default presentation conforms to WCAG 2.2 AA.**

Because the platform shell renders *everything* — plugins contribute data,
never markup or JavaScript (the declarative composition model) — conformance
is a property of one component library and one token set, enforced in CI. A
plugin cannot opt out, half-comply, or drift.

## Compact mode: the one documented trade

Compact is an explicit, persisted, **user-selected** density preference. It
relaxes exactly the sizing criteria and nothing else:

**Relaxed in compact:**
- Target size (2.5.8 Target Size (Minimum)) and the control heights, row
  heights, padding, and font-size minimums that constitute the density axis.

**How the default satisfies 2.5.8, precisely:** dedicated controls (buttons,
nav links) are ≥24 CSS px tall directly (`--control-h: 32px`). Inline links
inside list/table rows are line-box sized (<24px) and conform via the
criterion's **spacing exception**: the 40px row height guarantees a
24px-diameter circle centered on each row's target intersects no adjacent
target. Compact's 26px rows narrow that spacing below the exception's
threshold — that is exactly the documented trade.

**Never relaxed, in either density:**
- Contrast (1.4.3 text 4.5:1; 1.4.11 non-text 3:1) — color tokens are
  density-independent by construction.
- Keyboard operability (2.1.1), visible focus (2.4.7), focus not obscured
  (2.4.11).
- Semantics, names/roles/values, labels.
- Resize text to 200% (1.4.4), reflow (1.4.10), text spacing (1.4.12).
- Motion: `prefers-reduced-motion` is honored (2.3.3).
- Status is never conveyed by color alone (1.4.1) — status colors always pair
  a dot/icon with a text label.

The default (comfortable) presentation conforms; compact is an opt-in trade of
the target-size minimum for information density. Users who never touch the
toggle never leave the conforming presentation.

## How this is enforced — what each check actually proves

CI checks are stated with their real coverage; nothing below claims more than
it verifies:

- `scripts/validate-tokens.mjs` — **contrast, both themes.** Computes WCAG
  ratios for every declared ink/surface/status/focus token pair; a failing
  pair fails the build (`npm run build` runs it first). Because color tokens
  are density-independent by construction, this covers compact too.
- `tests/a11y.test.tsx` — **semantics, roles, names, structure** via axe-core
  per page. Runs in jsdom, which applies no layout or styling: the contrast
  rule is delegated to the token validator, and theme/density (CSS-only token
  swaps) cannot change what axe sees — so the audit runs once per page, not
  as a fake matrix.
- `tests/keyboard.test.tsx` — keyboard operability, state announcement
  (`aria-pressed`), current-page marking, and per-route document titles
  (2.4.2).
- **Verified by construction + eyeball, not CI:** visible focus
  (`:focus-visible` outline tokens — jsdom cannot observe it), reflow at
  320 px (single-column stacking below 640 px; wide tables scroll inside
  their own container, never the page), and reduced-motion. Layout-level
  regressions here need a real browser; re-check them when shell layout
  changes.
- Status changes are announced (4.1.3): loading/empty notes carry
  `role="status"`, errors `role="alert"`; transient poll failures never
  replace already-rendered data.

## Reporting issues

Accessibility defects are prioritized as functional bugs, not enhancements.
Open an issue on the repository; include the page, density, theme, and the
assistive technology or input method affected.
