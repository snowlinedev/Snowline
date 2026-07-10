/** The `scope` action-field kind (ui-shell.md §5.1): a text input backed by a
 * native `<datalist>` typeahead over the platform's scope slugs, fetched once
 * per form open from the existing `/scopes/tree` data path. The datalist is
 * ASSISTANCE, not validation — free text always submits, and a loading/failed
 * scope fetch degrades SILENTLY to a plain text input (no error surface, form
 * never blocked). These drive `PageActions` directly.
 *
 * No jest-dom in this project (see registered-ui.test.tsx) — assertions use
 * plain queries and property/attribute checks. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { UIAction } from "../src/api";
import { PageActions } from "../src/kinds/kinds";

const ACTION: UIAction = {
  id: "new-branch",
  label: "New branch",
  endpoint: "/ui-api/pages/branches",
  fields: [
    { name: "scope", label: "Scope", kind: "scope", required: true },
    { name: "name", label: "Branch name", kind: "text", required: true },
  ],
};

const PROXIED_PATH = "/ui-api/governance/pages/branches";

// A scope tree returned DELIBERATELY out of slug order, to pin that the shell
// flattens depth-first AND sorts before rendering the datalist options.
const SCOPE_TREE = {
  tree: [
    {
      slug: "snowlinedev",
      name: "SnowlineDev",
      kind: "org",
      status: "active",
      isolated: false,
      children: [
        {
          slug: "snowlinedev/snowline-pm",
          name: "PM",
          kind: "project",
          status: "active",
          isolated: false,
          children: [],
        },
        {
          slug: "snowlinedev/snowline",
          name: "Snowline",
          kind: "project",
          status: "active",
          isolated: false,
          children: [],
        },
      ],
    },
  ],
};
const SORTED_SLUGS = [
  "snowlinedev",
  "snowlinedev/snowline",
  "snowlinedev/snowline-pm",
];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

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

/** The `<option>` values of the datalist the scope input points at. */
function datalistOptionsFor(input: HTMLInputElement): string[] {
  const listId = input.getAttribute("list");
  expect(listId).toBeTruthy();
  const list = document.getElementById(listId!) as HTMLDataListElement | null;
  expect(list?.tagName).toBe("DATALIST");
  return [...(list?.querySelectorAll("option") ?? [])].map((o) =>
    (o as HTMLOptionElement).value,
  );
}

describe("scope action-field kind", () => {
  it("renders a text input backed by a datalist of the fetched, sorted slugs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) => {
        expect(path).toBe("/scopes/tree");
        return jsonResponse(SCOPE_TREE);
      }),
    );
    const user = userEvent.setup();
    renderInRouter([ACTION]);
    await user.click(screen.getByRole("button", { name: "New branch" }));

    // The scope field is a native text input (not a hard <select>), labelled.
    const scope = screen.getByLabelText("Scope") as HTMLInputElement;
    expect(scope.tagName).toBe("INPUT");
    expect(scope.type).toBe("text");

    // The datalist populates from /scopes/tree — flattened depth-first + sorted.
    await waitFor(() => expect(datalistOptionsFor(scope)).toEqual(SORTED_SLUGS));
  });

  it("only fetches scopes once the form with a scope field is opened", async () => {
    const fetchMock = vi.fn(async (_path: string) => jsonResponse(SCOPE_TREE));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderInRouter([ACTION]);
    // Closed form: no scope fetch yet (the datalist source is lazy).
    expect(fetchMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "New branch" }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((c) => c[0] === "/scopes/tree")).toBe(true),
    );
    // Exactly one scope fetch for one form open (not per-field, not per-render).
    expect(fetchMock.mock.calls.filter((c) => c[0] === "/scopes/tree")).toHaveLength(
      1,
    );
  });

  it("does not fetch scopes for a form with no scope field", async () => {
    const fetchMock = vi.fn(async (_path: string) => jsonResponse(SCOPE_TREE));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderInRouter([
      {
        id: "plain",
        label: "Plain",
        endpoint: "/ui-api/pages/branches",
        fields: [{ name: "note", label: "Note", kind: "text" }],
      },
    ]);
    await user.click(screen.getByRole("button", { name: "Plain" }));
    // Give any (erroneous) fetch a chance to fire.
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts free text and submits the typed value (datalist is assistance, not a restriction)", async () => {
    let postedBody: unknown;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          expect(path).toBe(PROXIED_PATH);
          postedBody = JSON.parse(String(init.body));
          return jsonResponse({ id: "b1", navigate: "/shadow/b1" });
        }
        return jsonResponse(SCOPE_TREE);
      }),
    );
    const user = userEvent.setup();
    renderInRouter([ACTION]);
    await user.click(screen.getByRole("button", { name: "New branch" }));

    // A scope that is NOT in the datalist (a not-yet-registered PM-style scope)
    // still types and submits verbatim.
    await user.type(screen.getByLabelText("Scope"), "acme/not-registered-yet");
    await user.type(screen.getByLabelText("Branch name"), "born-in-ui");
    await user.click(screen.getByRole("button", { name: "New branch" }));

    await screen.findByText("LANDED HERE");
    expect(postedBody).toEqual({
      scope: "acme/not-registered-yet",
      name: "born-in-ui",
    });
  });

  it("degrades to a plain text input with no error surface when the scope fetch fails", async () => {
    let postedBody: unknown;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_path: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          postedBody = JSON.parse(String(init.body));
          return jsonResponse({ id: "b1", navigate: "/shadow/b1" });
        }
        // The scope fetch fails (flaky tailnet / down platform route).
        return new Response("boom", { status: 500 });
      }),
    );
    const user = userEvent.setup();
    renderInRouter([ACTION]);
    await user.click(screen.getByRole("button", { name: "New branch" }));

    const scope = screen.getByLabelText("Scope") as HTMLInputElement;
    // The field is still a usable text input; the datalist just has no options.
    expect(scope.tagName).toBe("INPUT");
    await waitFor(() => expect(datalistOptionsFor(scope)).toEqual([]));
    // No error surface — a failed typeahead fetch is NOT a form error.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Failed to load/)).toBeNull();

    // And the form still works: free text submits.
    await user.type(scope, "acme/widget");
    await user.type(screen.getByLabelText("Branch name"), "x");
    await user.click(screen.getByRole("button", { name: "New branch" }));
    await screen.findByText("LANDED HERE");
    expect(postedBody).toEqual({ scope: "acme/widget", name: "x" });
  });

  it("is accessible: label wired to the input, input associated with its datalist (axe)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(SCOPE_TREE)));
    const user = userEvent.setup();
    const { container } = renderInRouter([ACTION]);
    await user.click(screen.getByRole("button", { name: "New branch" }));

    const scope = screen.getByLabelText("Scope") as HTMLInputElement;
    // Label association: getByLabelText already proves the <label htmlFor> ↔
    // input id wiring; the datalist association is the input's `list` → the
    // datalist's `id`.
    await waitFor(() => expect(datalistOptionsFor(scope)).toEqual(SORTED_SLUGS));
    expect(scope.getAttribute("list")).toBe(
      document.getElementById(scope.getAttribute("list")!)?.id,
    );

    const results = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes[0]?.html}`)).toEqual([]);
  });
});
