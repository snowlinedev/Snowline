import { fetchPlugins } from "../api";
import { Card, KindTable, StatusChip } from "../kinds/kinds";
import { Layout } from "../shell/Layout";
import { useData } from "../useData";
import { PendingNote } from "./Home";

export function Plugins() {
  const plugins = useData(fetchPlugins, 10);
  return (
    <Layout title="Plugins">
      <Card>
        {plugins.state === "ready" ? (
          <KindTable
            caption="Registered plugins"
            columns={[
              { key: "name", label: "Plugin" },
              { key: "status", label: "Status" },
              { key: "base_url", label: "Base URL" },
              { key: "surfaces", label: "Surface mappings" },
            ]}
            rows={plugins.data.map((p) => ({
              cells: {
                name: p.name,
                status: <StatusChip status={p.status} />,
                base_url: p.manifest.base_url,
                surfaces: Object.entries(p.manifest.surfaces)
                  .map(([path, surface]) => `${path} → ${surface}`)
                  .join(", "),
              },
            }))}
            empty="No plugins registered — surfaces serve no tools until registration heartbeats arrive."
          />
        ) : (
          <PendingNote loadable={plugins} />
        )}
      </Card>
    </Layout>
  );
}
