/** axe-core over every native page in BOTH densities × BOTH themes
 * (ACCESSIBILITY.md: compact relaxes target-size only — semantics, labels,
 * roles, and structure must hold everywhere). The color-contrast rule is
 * excluded here because jsdom has no layout/rendering; contrast is enforced
 * for real by scripts/validate-tokens.mjs over the token pairs. */
import { render, screen } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";

const PAGES = ["/", "/plugins", "/surfaces", "/scopes"];
const DENSITIES = ["comfortable", "compact"] as const;
const THEMES = ["light", "dark"] as const;

async function renderAndAudit(path: string) {
  const { container } = render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
  // Wait for data to land (every page renders fixture-backed content).
  await screen.findAllByText(/governance|SnowlineDev|main/, undefined, {
    timeout: 2000,
  });
  const results = await axe.run(container, {
    rules: { "color-contrast": { enabled: false } },
  });
  return results.violations;
}

describe("axe: native pages", () => {
  for (const density of DENSITIES) {
    for (const theme of THEMES) {
      for (const path of PAGES) {
        it(`${path} is violation-free (${density}, ${theme})`, async () => {
          document.documentElement.setAttribute("data-theme", theme);
          document.documentElement.setAttribute("data-density", density);
          const violations = await renderAndAudit(path);
          expect(
            violations.map((v) => `${v.id}: ${v.nodes[0]?.html}`),
          ).toEqual([]);
        });
      }
    }
  }
});
