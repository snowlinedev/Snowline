import { useCallback, useEffect, useRef, useState } from "react";

export type Loadable<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: T };

/** A `Loadable` plus a `reload()` escape hatch for callers that need to force
 * a refetch outside the poll cadence (shadow-conversations.md §4: a composer
 * send refetches the thread immediately rather than waiting on the next
 * tick). `reload` shares the same generation-counter / last-good-data
 * robustness as the poll timer — see the file-level comment on `useData`. */
export type DataResult<T> = Loadable<T> & { reload: () => void };

export type UseDataOptions = {
  /** Pause the poll timer while `document.hidden` (shadow-conversations.md
   * §4: thread polling stops when the tab isn't visible) and fire an
   * immediate refetch on return to visibility so a reply that landed while
   * hidden shows up right away rather than waiting out the rest of the
   * interval. Off by default — existing widget/native-view polling is
   * unaffected. */
  pauseWhenHidden?: boolean;
};

/** Fetch-on-mount with optional polling (the spec's liveness answer: poll
 * first, sockets when polling annoys someone).
 *
 * Poll robustness (PR #56 review): responses apply only if they belong to the
 * LATEST issued request (a generation counter — slow responses can't resolve
 * out of order and overwrite newer data), and a failed poll never clobbers
 * data that already rendered (a deploy-restart blip must not flash every card
 * into an error state; the next successful poll refreshes silently). The same
 * robustness covers a manual `reload()`.
 *
 * `deps` re-arms the hook when a parameterized loader's inputs change (e.g.
 * phase-2 plugin widgets: `useData(() => fetchWidget(name), 30, [name])`). */
export function useData<T>(
  load: () => Promise<T>,
  refreshSeconds?: number,
  deps: unknown[] = [],
  options?: UseDataOptions,
): DataResult<T> {
  const [value, setValue] = useState<Loadable<T>>({ state: "loading" });
  const hasData = useRef(false);
  const runRef = useRef<() => void>(() => {});
  const pauseWhenHidden = options?.pauseWhenHidden ?? false;
  useEffect(() => {
    let live = true;
    let generation = 0;
    hasData.current = false;
    setValue({ state: "loading" });
    const run = () => {
      const mine = ++generation;
      load().then(
        (data) => {
          if (!live || mine !== generation) return;
          hasData.current = true;
          setValue({ state: "ready", data });
        },
        (err) => {
          if (!live || mine !== generation) return;
          if (!hasData.current) {
            setValue({ state: "error", message: String(err) });
          } // else: keep last-good data through a transient poll failure
        },
      );
    };
    runRef.current = run;
    void run();

    let timer: number | undefined;
    const startTimer = () => {
      // Idempotent and visibility-aware: never stack a second interval on an
      // existing one (only the newest id would ever be cleared — a permanent
      // poll leak if a browser fires two "visible" visibilitychange events
      // back to back), and never start while paused-hidden — a page mounted
      // in an already-hidden tab gets no visibilitychange event to correct
      // an eagerly-started timer.
      if (timer !== undefined) return;
      if (pauseWhenHidden && document.hidden) return;
      if (refreshSeconds) timer = window.setInterval(run, refreshSeconds * 1000);
    };
    const stopTimer = () => {
      if (timer !== undefined) {
        window.clearInterval(timer);
        timer = undefined;
      }
    };
    startTimer();

    const onVisibility = () => {
      if (document.hidden) {
        stopTimer();
      } else {
        run(); // catch up immediately rather than wait out the paused interval
        startTimer();
      }
    };
    if (pauseWhenHidden) {
      document.addEventListener("visibilitychange", onVisibility);
    }

    return () => {
      live = false;
      stopTimer();
      if (pauseWhenHidden) {
        document.removeEventListener("visibilitychange", onVisibility);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  const reload = useCallback(() => runRef.current(), []);
  return { ...value, reload };
}
