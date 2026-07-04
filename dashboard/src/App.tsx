import { Route, Routes } from "react-router-dom";

import { fetchPlugins } from "./api";
import { PendingNote } from "./kinds/kinds";
import { Home } from "./pages/Home";
import { Plugins } from "./pages/Plugins";
import { PluginPage } from "./pages/PluginPage";
import { Scopes } from "./pages/Scopes";
import { Surfaces } from "./pages/Surfaces";
import { pluginRoutes } from "./registry";
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
      <Route path="/" element={<Home />} />
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
