import { fetchMilestoneConflicts, fetchPlugins } from "../api";
import { Card, KindTable, PendingNote, PluginStatusCell } from "../kinds/kinds";
import { Layout } from "../shell/Layout";
import { useData } from "../useData";


export function Plugins() {
  const plugins = useData(fetchPlugins, 10);
  const conflicts = useData(fetchMilestoneConflicts, 30);
  const n = conflicts.state === "ready" ? conflicts.data : 0;
  return (
    <Layout title="Plugins">
      {n > 0 && (
        <p className="attention-row" role="status">
          <a href="/ui/pm/milestones">
            {n} milestone {n === 1 ? "conflict" : "conflicts"}
          </a>{" "}
          awaiting triage. Resolve with the platform__resolve_milestone_conflict tool.
        </p>
      )}
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
                status: <PluginStatusCell status={p.status} reason={p.degraded_reason} />,
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
