#!/usr/bin/env node
/**
 * WCAG contrast validation of the design tokens (ui-shell.md §7) — the CI
 * teeth behind ACCESSIBILITY.md: a token pair that fails its minimum fails
 * the build, in BOTH themes. Density never enters here: color tokens are
 * density-independent by construction, so compact can never regress contrast.
 *
 * Pairs and minimums:
 *   text tokens on surfaces ................ 4.5:1  (1.4.3)
 *   text on accent fills ................... 4.5:1  (1.4.3)
 *   status + focus indicators on surfaces .. 3:1    (1.4.11 non-text; status
 *                                                    always ships icon+label)
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const css = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "..", "src", "tokens.css"),
  "utf8",
);

function themeBlock(theme) {
  const re = new RegExp(
    String.raw`:root(?:,\s*)?(?:\[data-theme="${theme}"\])?\s*\{([^}]*)\}`,
    "g",
  );
  // The light block is `:root, :root[data-theme="light"]`; dark is its own.
  const match = [...css.matchAll(/:root[^{]*\{[^}]*\}/g)]
    .map((m) => m[0])
    .find((b) => b.includes(`data-theme="${theme}"`));
  if (!match) throw new Error(`no ${theme} theme block found`);
  const tokens = {};
  for (const [, name, value] of match.matchAll(/--([\w-]+):\s*([^;]+);/g)) {
    tokens[name] = value.trim();
  }
  void re;
  return tokens;
}

function parseColor(value) {
  const hex = value.match(/^#([0-9a-f]{6})$/i);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgba = value.match(/^rgba?\(([^)]+)\)$/);
  if (rgba) return rgba[1].split(",").slice(0, 3).map((c) => Number(c.trim()));
  throw new Error(`unparseable color ${value}`);
}

function luminance([r, g, b]) {
  const lin = [r, g, b].map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

function contrast(a, b) {
  const [l1, l2] = [luminance(parseColor(a)), luminance(parseColor(b))].sort(
    (x, y) => y - x,
  );
  return (l1 + 0.05) / (l2 + 0.05);
}

// [foreground, background, minimum]
const PAIRS = [
  ["ink-primary", "surface-page", 4.5],
  ["ink-primary", "surface-card", 4.5],
  ["ink-secondary", "surface-page", 4.5],
  ["ink-secondary", "surface-card", 4.5],
  ["ink-muted", "surface-page", 4.5],
  ["ink-muted", "surface-card", 4.5],
  ["accent", "surface-page", 4.5], // accent is link/interactive TEXT
  ["accent", "surface-card", 4.5],
  ["accent-ink", "accent", 4.5],
  ["status-up", "surface-card", 3],
  ["status-down", "surface-card", 3],
  ["status-unknown", "surface-card", 3],
  ["focus-ring", "surface-page", 3],
  ["focus-ring", "surface-card", 3],
];

let failed = false;
for (const theme of ["light", "dark"]) {
  const tokens = themeBlock(theme);
  for (const [fg, bg, min] of PAIRS) {
    const ratio = contrast(tokens[fg], tokens[bg]);
    const ok = ratio >= min;
    if (!ok) failed = true;
    console.log(
      `${ok ? "PASS" : "FAIL"} [${theme}] --${fg} on --${bg}: ${ratio.toFixed(2)}:1 (min ${min}:1)`,
    );
  }
}
if (failed) {
  console.error("\ntoken contrast validation FAILED — fix tokens.css");
  process.exit(1);
}
console.log("\nall token pairs pass");
