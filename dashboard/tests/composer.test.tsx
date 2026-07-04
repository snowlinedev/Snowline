/** Thread composer (shadow-conversations.md §4): the input-shaped write path
 * at the thread foot. `Thread`-level tests exercise the composer's own
 * behavior directly (render-only-when-declared, send/refetch wiring via the
 * `onComposerSent` callback, error mapping, disabled/flags, keyboard submit)
 * without going through the full plugin/page/router machinery; one App-level
 * integration test confirms the real wiring end to end — the composer
 * reaches the declared endpoint through the `/ui-api` proxy path shape and a
 * successful send triggers an actual refetch of the thread data (no
 * optimistic append — one source of truth), using the SAME registered
 * `/governance/shadow/main-plan-x` fixture the axe and registered-ui suites
 * already cover (setup.ts's `shadow-branch` page composer declaration).
 *
 * No jest-dom in this project (see registered-ui.test.tsx) — assertions use
 * plain queries and property/attribute checks. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/App";
import { Thread } from "../src/kinds/kinds";
import { FIXTURES } from "./setup";

// `composer.path` is PLUGIN-RELATIVE, mirroring the `data` path convention
// (ui-shell.md §5): `postUiApi` applies the `/ui-api/<plugin>` shell prefix
// itself (via `uiApiUrl`), same as `fetchUiData` does for GET — so this is
// what `PluginPage` actually hands down (the composer's `endpoint`, already
// route-templated), not the final proxied URL.
const COMPOSER = {
  plugin: "governance",
  path: "/ui-api/pages/branches/main-plan-x/messages",
  placeholder: "Reply in this branch…",
  disabledWhen: "archived",
};
// The URL `postUiApi` actually calls `fetch` with, once the shell proxy
// prefix is applied.
const PROXIED_PATH = "/ui-api/governance/pages/branches/main-plan-x/messages";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("thread composer", () => {
  it("renders the composer only when the page declares one", () => {
    const { rerender } = render(<Thread title="main-plan-x" nodes={[]} />);
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(screen.queryByLabelText("Reply")).toBeNull();

    rerender(<Thread title="main-plan-x" nodes={[]} composer={COMPOSER} />);
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    const textarea = screen.getByLabelText("Reply") as HTMLTextAreaElement;
    expect(textarea.placeholder).toBe("Reply in this branch…");
  });

  it("greys the composer with a visible reason when flags contains disabled_when", () => {
    render(
      <Thread title="main-plan-x" nodes={[]} flags={["archived"]} composer={COMPOSER} />,
    );
    const textarea = screen.getByLabelText("Reply") as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(screen.getByText(/archived — read-only/)).toBeTruthy();
    expect(textarea.getAttribute("aria-describedby")).toBeTruthy();
  });

  it("does not disable the composer when flags omits disabled_when", () => {
    render(<Thread title="main-plan-x" nodes={[]} flags={["something-else"]} composer={COMPOSER} />);
    const textarea = screen.getByLabelText("Reply") as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(false);
  });

  it("send POSTs { markdown } and, on success, clears the box and asks for a refetch", async () => {
    const onComposerSent = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, init?: RequestInit) => {
        expect(path).toBe(PROXIED_PATH);
        expect(init?.method).toBe("POST");
        expect(JSON.parse(String(init?.body))).toEqual({ markdown: "hello there" });
        return jsonResponse({ seq: 2 });
      }),
    );

    const user = userEvent.setup();
    render(<Thread title="main-plan-x" nodes={[]} composer={COMPOSER} onComposerSent={onComposerSent} />);
    const textarea = screen.getByLabelText("Reply") as HTMLTextAreaElement;
    await user.type(textarea, "hello there");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(textarea.value).toBe(""));
    expect(onComposerSent).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a 409 response shows the fixed archived message, dismissibly, without clearing the draft", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "branch is archived" }, 409)),
    );

    const user = userEvent.setup();
    render(<Thread title="main-plan-x" nodes={[]} composer={COMPOSER} />);
    const textarea = screen.getByLabelText("Reply") as HTMLTextAreaElement;
    await user.type(textarea, "hi");
    await user.click(screen.getByRole("button", { name: "Send" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("This branch is archived");
    // Fail-visible, not silent — and the draft is preserved so nothing is lost.
    expect(textarea.value).toBe("hi");

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a 422 response surfaces the server's own message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "message must not be blank" }, 422)),
    );
    const user = userEvent.setup();
    render(<Thread title="main-plan-x" nodes={[]} composer={COMPOSER} />);
    await user.type(screen.getByLabelText("Reply"), "x");
    await user.click(screen.getByRole("button", { name: "Send" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("message must not be blank");
  });

  it("a 503 response shows the fixed plugin-down message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "plugin 'governance' is down" }, 503)),
    );
    const user = userEvent.setup();
    render(<Thread title="main-plan-x" nodes={[]} composer={COMPOSER} />);
    await user.type(screen.getByLabelText("Reply"), "x");
    await user.click(screen.getByRole("button", { name: "Send" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("currently down");
  });

  it("Cmd/Ctrl+Enter submits without clicking Send; plain Enter would just insert a newline", async () => {
    const onComposerSent = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ seq: 3 })));

    const user = userEvent.setup();
    render(<Thread title="main-plan-x" nodes={[]} composer={COMPOSER} onComposerSent={onComposerSent} />);
    const textarea = screen.getByLabelText("Reply") as HTMLTextAreaElement;
    await user.type(textarea, "quick reply");
    await user.keyboard("{Control>}{Enter}{/Control}");

    await waitFor(() => expect(onComposerSent).toHaveBeenCalledTimes(1));
  });

  it("end to end: a registered thread page's composer POSTs through the /ui-api proxy path shape and refetches", async () => {
    const dataPath = "/ui-api/governance/pages/branches/main-plan-x";
    let posted = false;
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method === "POST") {
        expect(path).toBe(`${dataPath}/messages`);
        expect(JSON.parse(String(init.body))).toEqual({ markdown: "hello there" });
        posted = true;
        return jsonResponse({ seq: 2 });
      }
      if (path === dataPath && posted) {
        return jsonResponse({
          title: "main-plan-x",
          nodes: [
            {
              author: "sean",
              kind: "comment",
              markdown: "Discussion about main-plan-x.",
              at: "2026-07-01T12:00:00Z",
            },
            { author: "you", kind: "message", markdown: "hello there", at: "2026-07-02T00:00:00Z" },
          ],
        });
      }
      const body = FIXTURES[path];
      if (!body) return new Response("not found", { status: 404 });
      return jsonResponse(body);
    });

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/governance/shadow/main-plan-x"]}>
        <App />
      </MemoryRouter>,
    );
    const textarea = (await screen.findByLabelText("Reply")) as HTMLTextAreaElement;
    await user.type(textarea, "hello there");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(textarea.value).toBe(""));
    await screen.findByText("hello there");
  });
});
