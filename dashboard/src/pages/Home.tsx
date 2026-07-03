/** Home — the widget grid (ui-shell.md §6): platform-native widgets built
 * THROUGH the kind vocabulary; registered plugin widgets join this grid in
 * phase 2. */

import { fetchPlugins, fetchSurfaces } from "../api";
import {
  Card,
  KindList,
  PendingNote,
  Stat,
  StatusChip,
} from "../kinds/kinds";
import { Layout } from "../shell/Layout";
import { useData } from "../useData";

export function Home() {
  const plugins = useData(fetchPlugins, 10);
  const surfaces = useData(fetchSurfaces, 30);

  return (
    <Layout title="Home">
      <div className="grid">
        <Card title="Plugins up">
          {plugins.state === "ready" ? (
            <Stat
              value={`${plugins.data.filter((p) => p.status === "up").length} / ${plugins.data.length}`}
              label="registered plugins healthy"
            />
          ) : (
            <PendingNote loadable={plugins} />
          )}
        </Card>
        <Card title="Plugin status">
          {plugins.state === "ready" ? (
            <KindList
              items={plugins.data.map((p) => ({
                text: p.name,
                meta: <StatusChip status={p.status} />,
              }))}
              empty="No plugins registered."
            />
          ) : (
            <PendingNote loadable={plugins} />
          )}
        </Card>
        <Card title="Surfaces">
          {surfaces.state === "ready" ? (
            <KindList
              items={surfaces.data.map((s) => ({
                text: s.name,
                href: "/surfaces",
                meta: `${s.plugins.length} plugin${s.plugins.length === 1 ? "" : "s"}`,
              }))}
              empty="No surfaces mounted."
            />
          ) : (
            <PendingNote loadable={surfaces} />
          )}
        </Card>
      </div>
    </Layout>
  );
}
