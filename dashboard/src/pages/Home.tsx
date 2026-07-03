/** Home — the widget grid (ui-shell.md §6): platform-native widgets built
 * THROUGH the kind vocabulary; registered plugin widgets join this grid in
 * phase 2. */

import { fetchPlugins, fetchSurfaces } from "../api";
import { Card, KindList, StateNote, Stat, StatusChip } from "../kinds/kinds";
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
            <ul className="kind-list">
              {plugins.data.map((p) => (
                <li key={p.name}>
                  {p.name}
                  <span className="meta">
                    <StatusChip status={p.status} />
                  </span>
                </li>
              ))}
            </ul>
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

export function PendingNote(props: {
  loadable: { state: "loading" } | { state: "error"; message: string };
}) {
  return props.loadable.state === "loading" ? (
    <StateNote>Loading…</StateNote>
  ) : (
    <StateNote error>Failed to load: {props.loadable.message}</StateNote>
  );
}
