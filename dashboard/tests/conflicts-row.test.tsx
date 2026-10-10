/** The Plugins page's milestone-conflict attention row (#248): rendered only
 * when `/health` carries a positive `milestones.conflicts`. */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURES } from "./setup";

function renderPlugins() {
  return render(
    <MemoryRouter initialEntries={["/plugins"]}>
      <App />
    </MemoryRouter>,
  );
}

afterEach(() => {
  FIXTURES["/health"] = { status: "ok", milestones: { conflicts: 0 } };
});

describe("milestone conflicts attention row", () => {
  it("is absent when there are no conflicts", async () => {
    renderPlugins();
    await screen.findByText("Registered plugins");
    expect(screen.queryByText(/milestone conflict/)).toBeNull();
  });

  it("shows the count with a link to the milestones page", async () => {
    FIXTURES["/health"] = { status: "ok", milestones: { conflicts: 2 } };
    renderPlugins();
    const link = await screen.findByRole("link", { name: "2 milestone conflicts" });
    expect(link.getAttribute("href")).toBe("/ui/pm/milestones");
  });

  it("stays absent when the health read fails", async () => {
    delete FIXTURES["/health"];
    renderPlugins();
    await screen.findByText("Registered plugins");
    expect(screen.queryByText(/milestone conflict/)).toBeNull();
  });
});
