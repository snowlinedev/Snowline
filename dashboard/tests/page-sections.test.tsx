/** ui-shell.md §4.2 additive `sections`: extra table sections under a page's
 * main content. Fixtures (sectioned / sections-empty / sections-absent) live
 * in tests/setup.ts; the axe pass over /governance/sectioned is in
 * a11y.test.tsx. */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("page sections", () => {
  it("renders sections in payload order as labelled h2 regions", async () => {
    renderAt("/governance/sectioned");
    await screen.findByRole("heading", { level: 2, name: "Channels" });
    const h2s = screen
      .getAllByRole("heading", { level: 2 })
      .map((h) => h.textContent)
      .filter((t) => t === "Channels" || t === "Scope growth");
    expect(h2s).toEqual(["Channels", "Scope growth"]);

    const region = screen.getByRole("region", { name: "Channels" });
    expect(within(region).getByText("beta")).toBeTruthy();
    expect(
      within(region).getByRole("link", { name: "alpha" }).getAttribute("href"),
    ).toBe("/governance/shadow/alpha");
    expect(screen.getByRole("region", { name: "Scope growth" })).toBeTruthy();
    // Main content still renders.
    expect(screen.getByText("main-plan-x")).toBeTruthy();
  });

  it("renders the empty-state line, not an empty table", async () => {
    renderAt("/governance/sectioned");
    const region = await screen.findByRole("region", { name: "Scope growth" });
    expect(within(region).getByText("No scope growth yet.")).toBeTruthy();
    expect(within(region).queryByRole("table")).toBeNull();
  });

  it("leaves the page byte-identical when sections is absent or empty", async () => {
    const absent = renderAt("/governance/sections-absent");
    await screen.findByText("main-plan-x");
    const absentHtml = absent.container.innerHTML;
    absent.unmount();

    const empty = renderAt("/governance/sections-empty");
    await screen.findByText("main-plan-x");
    expect(empty.container.innerHTML).toBe(absentHtml);
  });
});
