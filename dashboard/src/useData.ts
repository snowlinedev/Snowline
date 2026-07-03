import { useEffect, useState } from "react";

export type Loadable<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: T };

/** Fetch-on-mount with optional polling (the spec's liveness answer: poll
 * first, sockets when polling annoys someone). */
export function useData<T>(
  load: () => Promise<T>,
  refreshSeconds?: number,
): Loadable<T> {
  const [value, setValue] = useState<Loadable<T>>({ state: "loading" });
  useEffect(() => {
    let live = true;
    const run = () =>
      load().then(
        (data) => live && setValue({ state: "ready", data }),
        (err) => live && setValue({ state: "error", message: String(err) }),
      );
    void run();
    const timer = refreshSeconds
      ? window.setInterval(run, refreshSeconds * 1000)
      : undefined;
    return () => {
      live = false;
      if (timer) window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return value;
}
