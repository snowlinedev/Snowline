import { Route, Routes } from "react-router-dom";

import { fetchPlugins } from "./api";
import { PendingNote } from "./kinds/kinds";
import { Plugins } from "./pages/Plugins";
import { PluginPage } from "./pages/PluginPage";
import { Scopes } from "./pages/Scopes";
import { SectionPage } from "./pages/Section";
import { Surfaces } from "./pages/Surfaces";
import { Today } from "./pages/Today";
import { SECTIONS, pluginRoutes } from "./registry";
import { Layout } from "./shell/Layout";
import { useData } from "./useData";

export function App() {
  // Registered pages are ROUTES, computed from the same /plugins fetch every
  // page already makes (ui-shell.md §3/§6) — until it resolves, only the
  // native routes exist; a directly-loaded plugin URL briefly falls through
  // to the catch-all below and resolves once plugins arrive.
  const plugins = useData(fetchPlugins, 30);
  const routes = plugins.state === "ready" ? pluginRoutes(plugins.data) : [];

  return (
    <Routes>
      {/* The five sections (dashboard-ia.md §3) route from the same table nav
       * lists them from, so a section can never be nav-listed without
       * resolving. Today is the widget grid at `/`; the others are section
       * pages. The native views keep their own cited routes — System is
       * where they're FOUND, not a re-namespacing. */}
      <Route path="/" element={<Today />} />
      {SECTIONS.filter((s) => s.to !== "/").map((s) => (
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
  );
}
