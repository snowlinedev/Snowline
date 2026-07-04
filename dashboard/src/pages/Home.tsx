/** Home — the widget grid (ui-shell.md §6): platform-native widgets built
 * THROUGH the kind vocabulary, then registered plugin widgets (slot "home")
 * appended after them, each its own polling Card. */

import type { PluginEntry, UIWidget } from "../api";
import { fetchPlugins, fetchSurfaces } from "../api";
import {
  Card,
  KindList,
  PendingNote,
  RegisteredKind,
  Stat,
  StatusChip,
  useUiData,
} from "../kinds/kinds";
import { clampRefreshSeconds, contractSupported, pluginWidgets } from "../registry";
import { Layout } from "../shell/Layout";
import { useData } from "../useData";

/** One registered widget's own Card — its own fetch/poll hook, so a slow or
 * down plugin's widget can't block or clobber another's. */
function WidgetCard(props: { plugin: PluginEntry; widget: UIWidget }) {
  const contractOk = contractSupported(props.plugin);
  const loadable = useUiData(
    props.plugin.name,
    props.widget.data,
    props.widget.kind,
    contractOk,
    clampRefreshSeconds(props.widget.refresh_seconds),
  );
  return (
    <Card title={props.widget.title ?? props.widget.id}>
      <RegisteredKind
        plugin={props.plugin.name}
        path={props.widget.data}
        kind={props.widget.kind}
        contractOk={contractOk}
        loadable={loadable}
      />
    </Card>
  );
}

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
        {plugins.state === "ready" &&
          pluginWidgets(plugins.data).map(({ key, plugin, widget }) => (
            <WidgetCard key={key} plugin={plugin} widget={widget} />
          ))}
      </div>
    </Layout>
  );
}
