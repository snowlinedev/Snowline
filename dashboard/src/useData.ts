import { useEffect, useRef, useState } from "react";

export type Loadable<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: T };

/** Fetch-on-mount with optional polling (the spec's liveness answer: poll
 * first, sockets when polling annoys someone).
 *
 * Poll robustness (PR #56 review): responses apply only if they belong to the
 * LATEST issued request (a generation counter — slow responses can't resolve
 * out of order and overwrite newer data), and a failed poll never clobbers
 * data that already rendered (a deploy-restart blip must not flash every card
 * into an error state; the next successful poll refreshes silently).
 *
 * `deps` re-arms the hook when a parameterized loader's inputs change (e.g.
 * phase-2 plugin widgets: `useData(() => fetchWidget(name), 30, [name])`). */
export function useData<T>(
  load: () => Promise<T>,
  refreshSeconds?: number,
  deps: unknown[] = [],
): Loadable<T> {
  const [value, setValue] = useState<Loadable<T>>({ state: "loading" });
  const hasData = useRef(false);
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
    void run();
    const timer = refreshSeconds
      ? window.setInterval(run, refreshSeconds * 1000)
      : undefined;
    return () => {
      live = false;
      if (timer) window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return value;
}
