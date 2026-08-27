/** A demand-side section page (dashboard-ia.md §3): Roadmap, Features,
 * Review, System. A section is a PLACE in the IA, not a plugin's page — it
 * lists the contributions the platform composed into it (the same entries nav
 * shows under the section's link, from the same function over the same shared
 * fetch, so the two cannot disagree) and, when nothing landed there, says so
 * in words rather than vanishing.
 *
 * System additionally hosts the platform's own health cards — the plugins-up
 * stat, the plugin status list, and the mounted surfaces — which §3 moved off
 * Today ("Today's content is the owner's work, not the machine's"). They are
 * native views built through the kind vocabulary, each self-polling exactly as
 * they did on the Home grid.
 *
 * Today (`/`) is its own page: banded composition (§5), in pages/Today.tsx. */

import type { PluginEntry } from "../api";
import { fetchSurfaces } from "../api";
import { Card, KindList, PendingNote, StateNote, Stat, StatusChip } from "../kinds/kinds";
import { usePlugins } from "../plugins-context";
import { sectionNavEntries, type SectionDef } from "../registry";
import { Layout } from "../shell/Layout";
import { useData } from "../useData";

function SectionTenants(props: { section: SectionDef; plugins: PluginEntry[] }) {
  const entries = sectionNavEntries(props.plugins, props.section);
  // §3: a section with nothing to show renders an explicit empty state (a
  // status message, so it's announced) — it never renders blank, and it never
  // drops out of nav.
  if (entries.length === 0) return <StateNote>{props.section.empty}</StateNote>;
  return <KindList items={entries.map((e) => ({ text: e.label, href: e.to }))} />;
}

/** "Is the machine healthy?" — the three native cards System absorbed from
 * Today (§3). Plugins come from App's ONE /plugins fetch via context (the same
 * result nav and the tenant list read); surfaces keep their own 30s poll, the
 * only fetch that moved with the cards. Each card renders its own pending
 * state, so a failing fetch greys out one card rather than the page. */
function SystemHealth() {
  const plugins = usePlugins();
  const surfaces = useData(fetchSurfaces, 30);
  return (
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
  );
}

export function SectionPage(props: { section: SectionDef }) {
  const plugins = usePlugins();
  const tenants = (
    <Card>
      {plugins.state === "ready" ? (
        <SectionTenants section={props.section} plugins={plugins.data} />
      ) : (
        <PendingNote loadable={plugins} />
      )}
    </Card>
  );
  // System is the one section with native content of its own; every other
  // section is exactly its tenant list, unchanged.
  if (props.section.id !== "system") {
    return <Layout title={props.section.label}>{tenants}</Layout>;
  }
  return (
    <Layout title={props.section.label}>
      <div className="page-stack">
        <SystemHealth />
        {tenants}
      </div>
    </Layout>
  );
}
