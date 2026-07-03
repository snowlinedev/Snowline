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
- Target size (2.5.8 Target Size (Minimum), 24×24 CSS px) and the control
  heights, row heights, and padding minimums that flow from it.

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

## How this is enforced (CI, not vigilance)

- `scripts/validate-tokens.mjs` — computes WCAG contrast for every declared
  ink/surface/status/focus token pair, both themes; a failing pair fails the
  build (`npm run build` runs it first).
- `tests/a11y.test.tsx` — axe-core over every native page in both densities ×
  both themes (contrast rule delegated to the token validator; jsdom has no
  layout).
- `tests/keyboard.test.tsx` — keyboard operability and state announcement for
  the shell controls.

## Reporting issues

Accessibility defects are prioritized as functional bugs, not enhancements.
Open an issue on the repository; include the page, density, theme, and the
assistive technology or input method affected.
