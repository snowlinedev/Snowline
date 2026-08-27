/** The ONE live /plugins result, shared by context. App owns the fetch;
 * Layout's nav, the section pages, and Today all consume the same result —
 * previously each ran its own useData poll loop (three identical GETs per
 * view, forever), and nav vs page could transiently disagree about the
 * registry when their independent fetches resolved at different moments.
 * One fetch makes the §3 "nav and the section page cannot disagree" claim
 * true for the DATA, not just the composition function. */

import { createContext, useContext } from "react";

import type { PluginEntry } from "./api";
import type { DataResult } from "./useData";

const PluginsContext = createContext<DataResult<PluginEntry[]> | null>(null);

export const PluginsProvider = PluginsContext.Provider;

export function usePlugins(): DataResult<PluginEntry[]> {
  const value = useContext(PluginsContext);
  if (value === null) {
    // A programming error, not a data state: every page renders inside App's
    // provider. Throwing beats silently poll-looping a private fetch.
    throw new Error("usePlugins() outside <PluginsProvider>");
  }
  return value;
}
