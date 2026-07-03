/** axe-core over every native page (ACCESSIBILITY.md "what each check
 * actually proves"): this audits SEMANTICS — roles, names, labels, structure.
 * It runs once per page: theme/density are CSS-only token swaps that jsdom
 * (no layout, no styling) cannot surface to axe, so a matrix over them would
 * be theater. Contrast is enforced by scripts/validate-tokens.mjs; layout
 * concerns (focus visibility, reflow) are verified by construction + browser
 * eyeball per ACCESSIBILITY.md. */
import { render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";

const PAGES = ["/", "/plugins", "/surfaces", "/scopes"];

describe("axe: native pages", () => {
  for (const path of PAGES) {
    it(`${path} is violation-free`, async () => {
      const { container } = render(
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>,
      );
      // Fully rendered = no pending loading notes and no error notes — every
      // fixture-backed card has landed before axe runs (a violation in a
      // late-arriving widget must not slip past the audit).
      await waitFor(() => {
        expect(screen.queryAllByText(/Loading…/)).toHaveLength(0);
        expect(screen.queryAllByText(/Failed to load/)).toHaveLength(0);
      });
      const results = await axe.run(container, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(results.violations.map((v) => `${v.id}: ${v.nodes[0]?.html}`)).toEqual(
        [],
      );
    });
  }
});
