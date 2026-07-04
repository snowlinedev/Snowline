/** Phase-2 UI shell (ui-shell.md §3/§4/§4.4): registered widgets/pages render
 * through the same kind vocabulary + fail-visible dispatch as native views.
 * Fixtures live in tests/setup.ts — governance carries a `ui` block with one
 * home widget, a `table` page (nav), a `thread` page (row-linked only), a
 * deliberately-broken widget, and a deliberately-unknown-kind widget.
 *
 * No jest-dom in this project (see the rest of the suite) — assertions use
 * plain queries (`getByText` throws if absent) and `.textContent`/attribute
 * checks rather than `toBeInTheDocument()`. */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { App } from "../src/App";
import { Document, Markdown, Thread } from "../src/kinds/kinds";

describe("registered nav + pages", () => {
  it("groups a registered nav page under its plugin's name", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    // "governance" also appears as a Plugin-status list item on Home, so
    // scope to the nav landmark to find the group heading unambiguously.
    const nav = screen.getByRole("navigation", { name: "Main" });
    await within(nav).findByText("governance");
    const link = await within(nav).findByRole("link", { name: "Shadow discussions" });
    expect(link.getAttribute("href")).toBe("/governance/shadow");
  });

  it("does not nav-list a page with nav: false", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Main" });
    await within(nav).findByText("governance");
    expect(within(nav).queryByRole("link", { name: "Shadow branch" })).toBeNull();
  });

  it("renders a registered table page by fetching its /ui-api data", async () => {
    render(
      <MemoryRouter initialEntries={["/governance/shadow"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByText("main-plan-x");
    expect(screen.getByText("Branch")).toBeTruthy();
    // Row href is plugin-relative in the fixture ("/shadow/main-plan-x") and
    // gets re-namespaced under the plugin (ui-shell.md §3 row-href rule).
    const rowLink = screen.getByRole("link", { name: "main-plan-x" });
    expect(rowLink.getAttribute("href")).toBe("/governance/shadow/main-plan-x");
  });

  it("renders a registered thread page reached via the row link's route", async () => {
    const { container } = render(
      <MemoryRouter initialEntries={["/governance/shadow/main-plan-x"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByText("sean");
    expect(container.querySelector(".thread-title")?.textContent).toBe("main-plan-x");
    expect(screen.getByText("comment")).toBeTruthy();
    expect(screen.getByText("decision-abc")).toBeTruthy();
  });
});

describe("home grid: registered widgets", () => {
  it("renders a registered stat widget as its own card", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByText("Open shadow branches");
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("open branches")).toBeTruthy();
  });

  it("fails visible on an unknown kind, naming the plugin and the kind", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByText("Unsupported widget");
    await screen.findByText(
      "governance offers a view this platform version can't render (kind 'chart')",
    );
  });

  it("renders an error card for a malformed kind payload, naming plugin + path", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByText("Broken stat");
    const error = await screen.findByRole("alert");
    expect(error.textContent).toContain("governance");
    expect(error.textContent).toContain("/ui-api/widgets/broken-stat");
  });
});

describe("markdown safety (thread/document kinds)", () => {
  const dangerous =
    "Discussion about **main-plan-x**.\n\n<script>alert(1)</script> should stay text.";

  it("Markdown never turns plugin text into a live element", () => {
    const { container } = render(<Markdown text={dangerous} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    // The safe bold token still gets its element treatment.
    const strong = container.querySelector("strong");
    expect(strong?.textContent).toBe("main-plan-x");
  });

  it("Thread renders nodes/citations without executing embedded markup", () => {
    const { container } = render(
      <Thread
        title="main-plan-x"
        meta="Status: open"
        nodes={[
          {
            author: "sean",
            kind: "comment",
            markdown: dangerous,
            at: "2026-07-01T12:00:00Z",
            citations: ["decision-abc"],
          },
        ]}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("sean")).toBeTruthy();
    expect(screen.getByText("decision-abc")).toBeTruthy();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("Document renders its markdown body without executing embedded markup", () => {
    const { container } = render(<Document title="Branch notes" markdown={dangerous} />);
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("Branch notes")).toBeTruthy();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });
});
