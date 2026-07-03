import { fetchSurfaces } from "../api";
import { Card, KindTable } from "../kinds/kinds";
import { Layout } from "../shell/Layout";
import { useData } from "../useData";
import { PendingNote } from "./Home";

export function Surfaces() {
  const surfaces = useData(fetchSurfaces, 30);
  return (
    <Layout title="Surfaces">
      <Card>
        {surfaces.state === "ready" ? (
          <KindTable
            caption="Mounted gateway surfaces"
            columns={[
              { key: "name", label: "Surface" },
              { key: "route", label: "Route" },
              { key: "allowlist", label: "Allowlist" },
              { key: "plugins", label: "Composed plugins" },
            ]}
            rows={surfaces.data.map((s) => ({
              cells: {
                name: s.name,
                route: s.route,
                allowlist:
                  s.allowlist === "*" ? "* (all plugins)" : s.allowlist.join(", "),
                plugins: s.plugins.length ? s.plugins.join(", ") : "none",
              },
            }))}
            empty="No surfaces mounted."
          />
        ) : (
          <PendingNote loadable={surfaces} />
        )}
      </Card>
    </Layout>
  );
}
