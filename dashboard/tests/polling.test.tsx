/** `useData`'s pauseWhenHidden polling (shadow-conversations.md §4: thread
 * pages carrying a composer poll every 5s while the document is visible,
 * paused when the tab is hidden). Exercised at the hook level with fake
 * timers and a stubbed `document.hidden` — jsdom is never actually hidden,
 * so component-level tests can't reach these branches. */
import { renderHook } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useData } from "../src/useData";

let hidden = false;

function setHidden(value: boolean) {
  hidden = value;
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("useData pauseWhenHidden polling", () => {
  beforeEach(() => {
    hidden = false;
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => hidden,
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    // Restore jsdom's own `hidden` (always false) so other suites are
    // unaffected by the stubbed getter.
    hidden = false;
  });

  it("polls on the cadence while visible, pauses while hidden, catches up on return", async () => {
    const load = vi.fn(async () => "data");
    renderHook(() => useData(load, 5, [], { pauseWhenHidden: true }));
    expect(load).toHaveBeenCalledTimes(1); // fetch-on-mount

    await act(async () => {
      vi.advanceTimersByTime(5_000);
    });
    expect(load).toHaveBeenCalledTimes(2); // one poll tick

    await act(async () => {
      setHidden(true);
      vi.advanceTimersByTime(20_000);
    });
    expect(load).toHaveBeenCalledTimes(2); // paused: no ticks while hidden

    await act(async () => {
      setHidden(false);
    });
    expect(load).toHaveBeenCalledTimes(3); // immediate catch-up refetch

    await act(async () => {
      vi.advanceTimersByTime(5_000);
    });
    expect(load).toHaveBeenCalledTimes(4); // cadence resumed
  });

  it("does not start polling when mounted in an already-hidden tab", async () => {
    hidden = true; // hidden BEFORE mount — no visibilitychange will fire
    const load = vi.fn(async () => "data");
    renderHook(() => useData(load, 5, [], { pauseWhenHidden: true }));
    expect(load).toHaveBeenCalledTimes(1); // the mount fetch still happens

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    expect(load).toHaveBeenCalledTimes(1); // but no poll ticks while hidden
  });

  it("a duplicate 'visible' event never stacks a second interval", async () => {
    const load = vi.fn(async () => "data");
    renderHook(() => useData(load, 5, [], { pauseWhenHidden: true }));
    expect(load).toHaveBeenCalledTimes(1);

    await act(async () => {
      // Two visibilitychange events with document.hidden false throughout
      // (some browsers emit extras on focus churn) — each runs the immediate
      // catch-up fetch, but the interval must not double.
      setHidden(false);
      setHidden(false);
    });
    expect(load).toHaveBeenCalledTimes(3); // 1 mount + 2 catch-ups

    await act(async () => {
      vi.advanceTimersByTime(5_000);
    });
    expect(load).toHaveBeenCalledTimes(4); // exactly ONE tick per 5s
  });

  it("polling without pauseWhenHidden ignores visibility (unchanged behavior)", async () => {
    const load = vi.fn(async () => "data");
    renderHook(() => useData(load, 5));
    expect(load).toHaveBeenCalledTimes(1);

    await act(async () => {
      setHidden(true);
      vi.advanceTimersByTime(5_000);
    });
    expect(load).toHaveBeenCalledTimes(2); // still polling — opt-in only
  });
});
