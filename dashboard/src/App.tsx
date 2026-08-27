import { Route, Routes } from "react-router-dom";

import { fetchPlugins } from "./api";
import { PendingNote } from "./kinds/kinds";
import { Plugins } from "./pages/Plugins";
import { PluginPage } from "./pages/PluginPage";
import { Scopes } from "./pages/Scopes";
import { SectionPage } from "./pages/Section";
import { Surfaces } from "./pages/Surfaces";
import { Today } from "./pages/Today";
import { PluginsProvider } from "./plugins-context";
import { SECTIONS, pluginRoutes } from "./registry";
import { Layout } from "./shell/Layout";
import { useData } from "./useData";

export function App() {
  // THE /plugins fetch (see plugins-context.tsx): registered pages are ROUTES
  // computed from it, and nav/section pages/Today consume the same result via
  // context. 10s poll — the strictest cadence any consumer had (Today's
  // health cards). Until it resolves, only the native routes exist; a
  // directly-loaded plugin URL briefly falls through to the catch-all below
  // and resolves once plugins arrive.
  const plugins = useData(fetchPlugins, 10);
  const routes = plugins.state === "ready" ? pluginRoutes(plugins.data) : [];

  return (
    <PluginsProvider value={plugins}>
      <Routes>
        {/* The five sections (dashboard-ia.md §3) route from the same table
         * nav lists them from, so a section can never be nav-listed without
         * resolving. Today (by id, not by route string — the route is the
         * changeable fact, the identity isn't) is the widget grid at `/`; the
         * others are section pages. The native views keep their own cited
         * routes — System is where they're FOUND, not a re-namespacing. */}
        <Route path="/" element={<Today />} />
        {SECTIONS.filter((s) => s.id !== "today").map((s) => (
          <Route key={s.id} path={s.to} element={<SectionPage section={s} />} />
        ))}
        <Route path="/plugins" element={<Plugins />} />
        <Route path="/surfaces" element={<Surfaces />} />
        <Route path="/scopes" element={<Scopes />} />
        {routes.map((r) => (
          <Route
            key={r.key}
            path={r.routerPath}
            element={<PluginPage plugin={r.plugin} page={r.page} />}
          />
        ))}
        <Route
          path="*"
          element={
            <Layout title={plugins.state === "loading" ? "Loading…" : "Not found"}>
              {plugins.state === "ready" ? (
                <p className="state-note">No such page.</p>
              ) : (
                <PendingNote loadable={plugins} />
              )}
            </Layout>
          }
        />
      </Routes>
    </PluginsProvider>
  );
}
