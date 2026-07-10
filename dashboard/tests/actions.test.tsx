/** Page actions[] (ui-shell.md §5, issue #123): the button/form-shaped write
 * seam, rendered GENERICALLY by the shell. These drive `PageActions` directly
 * (button toggles a form of declared fields, required-field gating, POST of
 * the field values through the /ui-api proxy, success-navigation to the
 * response's plugin-relative `navigate` href, fail-visible errors), plus one
 * end-to-end pass through the real registered page (setup.ts's shadow-branches
 * `actions` declaration).
 *
 * No jest-dom in this project (see registered-ui.test.tsx) — assertions use
 * plain queries and property/attribute checks. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import type { UIAction } from "../src/api";
import { PageActions } from "../src/kinds/kinds";
import { FIXTURES } from "./setup";

const ACTION: UIAction = {
  id: "new-branch",
  label: "New branch",
  endpoint: "/ui-api/pages/branches",
  fields: [
    { name: "scope", label: "Scope", kind: "text", required: true },
    { name: "name", label: "Branch name", kind: "text", required: true },
    { name: "opening_message", label: "Opening note", kind: "multiline", required: false },
  ],
};

// The URL `postUiApi` actually calls `fetch` with, once the shell proxy prefix
// is applied to the plugin-relative endpoint (uiApiUrl).
const PROXIED_PATH = "/ui-api/governance/pages/branches";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** PageActions in a router whose destination route renders a marker, so a
 * successful `navigate` is observable. */
function renderInRouter(actions: UIAction[]) {
  return render(
    <MemoryRouter initialEntries={["/governance/shadow"]}>
      <Routes>
        <Route
          path="/governance/shadow"
          element={<PageActions plugin="governance" actions={actions} />}
        />
        <Route path="/governance/shadow/:branch" element={<div>LANDED HERE</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("page actions[]", () => {
  it("renders nothing extra when a page declares no actions", () => {
    const { container } = renderInRouter([]);
    expect(container.querySelector(".page-actions")).toBeNull();
  });

  it("renders only the labelled button until it is clicked", async () => {
    renderInRouter([ACTION]);
    expect(screen.getByRole("button", { name: "New branch" })).toBeTruthy();
    // The form fields are not present until the button opens the form.
    expect(screen.queryByLabelText("Scope")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "New branch" }));
    expect(screen.getByLabelText("Scope")).toBeTruthy();
    expect(screen.getByLabelText("Branch name")).toBeTruthy();
    // The optional multiline field renders as a textarea, labelled.
    const note = screen.getByLabelText(/Opening note/) as HTMLTextAreaElement;
    expect(note.tagName).toBe("TEXTAREA");
  });

  it("gates submit until required fields are filled", async () => {
    const user = userEvent.setup();
    renderInRouter([ACTION]);
    await user.click(screen.getByRole("button", { name: "New branch" }));

    const submit = screen.getByRole("button", { name: "New branch" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true); // scope + name empty
    await user.type(screen.getByLabelText("Scope"), "acme/widget");
    expect(submit.disabled).toBe(true); // name still empty
    await user.type(screen.getByLabelText("Branch name"), "born-in-ui");
    expect(submit.disabled).toBe(false);
  });

  it("submits the declared field values and navigates to the response's navigate href", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, init?: RequestInit) => {
        expect(path).toBe(PROXIED_PATH);
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({
          scope: "acme/widget",
          name: "born-in-ui",
          opening_message: "let us speculate",
        });
        return jsonResponse({ id: "b1", navigate: "/shadow/b1" });
      }),
    );

    const user = userEvent.setup();
    renderInRouter([ACTION]);
    await user.click(screen.getByRole("button", { name: "New branch" }));
    await user.type(screen.getByLabelText("Scope"), "acme/widget");
    await user.type(screen.getByLabelText("Branch name"), "born-in-ui");
    await user.type(screen.getByLabelText(/Opening note/), "let us speculate");
    await user.click(screen.getByRole("button", { name: "New branch" }));

    // Success → the shell followed the plugin-relative navigate href (prefixed
    // with /governance) to the new branch's thread route.
    await screen.findByText("LANDED HERE");
  });

  it("a failed submit surfaces the server's message and does not navigate", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "no such scope" }, 404)),
    );
    const user = userEvent.setup();
    renderInRouter([ACTION]);
    await user.click(screen.getByRole("button", { name: "New branch" }));
    await user.type(screen.getByLabelText("Scope"), "acme/nope");
    await user.type(screen.getByLabelText("Branch name"), "x");
    await user.click(screen.getByRole("button", { name: "New branch" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("no such scope");
    expect(screen.queryByText("LANDED HERE")).toBeNull();
    // Dismissible — fail-visible, then clearable.
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("end to end: the registered shadow-branches page's action POSTs through the /ui-api proxy and navigates", async () => {
    let posted = false;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST") {
        expect(path).toBe(PROXIED_PATH);
        expect(JSON.parse(String(init.body))).toEqual({
          scope: "acme/widget",
          name: "e2e-branch",
          opening_message: "",
        });
        posted = true;
        return jsonResponse({ id: "e2e", navigate: "/shadow/e2e" });
      }
      if (path === "/ui-api/governance/pages/branches/e2e" && posted) {
        return jsonResponse({ title: "e2e-branch", nodes: [] });
      }
      const body = FIXTURES[path];
      if (!body) return new Response("not found", { status: 404 });
      return jsonResponse(body);
    });

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/governance/shadow"]}>
        <App />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "New branch" }));
    await user.type(screen.getByLabelText("Scope"), "acme/widget");
    await user.type(screen.getByLabelText("Branch name"), "e2e-branch");
    await user.click(screen.getByRole("button", { name: "New branch" }));

    // Landed on the new branch's thread page (its title renders from the
    // mocked thread data).
    await screen.findByText("e2e-branch");
  });
});
