/** Keyboard operability (ACCESSIBILITY.md: never relaxed, either density):
 * the theme/density toggles are reachable and operable by keyboard, announce
 * state via aria-pressed, and nav links expose the current page. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "../src/App";

describe("keyboard operability", () => {
  beforeEach(() => localStorage.clear());

  it("density toggle is keyboard-operable and announces state", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    const toggle = screen.getByRole("button", { name: "Compact" });
    expect(toggle).toHaveProperty("type", "button");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");

    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect(document.documentElement.getAttribute("data-density")).toBe(
      "compact",
    );
    expect(localStorage.getItem("snowline.density")).toBe("compact");
  });

  it("theme toggle flips data-theme and persists", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    const toggle = screen.getByRole("button", { name: "Dark theme" });
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(localStorage.getItem("snowline.theme")).toBe("dark");
  });

  it("nav marks the current page for assistive tech", async () => {
    render(
      <MemoryRouter initialEntries={["/plugins"]}>
        <App />
      </MemoryRouter>,
    );
    const link = await screen.findByRole("link", { name: "Plugins" });
    expect(link.getAttribute("aria-current")).toBe("page");
  });
});
