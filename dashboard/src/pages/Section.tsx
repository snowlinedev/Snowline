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
import { KindList, LoadableCard, StateNote, Stat, StatusChip } from "../kinds/kinds";
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
      <LoadableCard title="Plugins up" loadable={plugins}>
        {(data) => (
          <Stat
            value={`${data.filter((p) => p.status === "up").length} / ${data.length}`}
            label="registered plugins healthy"
          />
        )}
      </LoadableCard>
      <LoadableCard title="Plugin status" loadable={plugins}>
        {(data) => (
          <KindList
            items={data.map((p) => ({
              text: p.name,
              meta: <StatusChip status={p.status} />,
            }))}
            empty="No plugins registered."
          />
        )}
      </LoadableCard>
      <LoadableCard title="Surfaces" loadable={surfaces}>
        {(data) => (
          <KindList
            items={data.map((s) => ({
              text: s.name,
              href: "/surfaces",
              meta: `${s.plugins.length} plugin${s.plugins.length === 1 ? "" : "s"}`,
            }))}
            empty="No surfaces mounted."
          />
        )}
      </LoadableCard>
    </div>
  );
}

export function SectionPage(props: { section: SectionDef }) {
  const plugins = usePlugins();
  const system = props.section.id === "system";
  const tenants = (
    // On System the tenant list has titled siblings (the health cards), so it
    // needs a heading of its own — an untitled section is no boundary for
    // heading navigation, and the links would read as content of "Surfaces".
    // Elsewhere the card is the page's sole content under the h1: untitled.
    <LoadableCard title={system ? "Views" : undefined} loadable={plugins}>
      {(data) => <SectionTenants section={props.section} plugins={data} />}
    </LoadableCard>
  );
  // System is the one section with native content of its own; every other
  // section is exactly its tenant list, unchanged.
  if (!system) {
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
