/** A demand-side section page (dashboard-ia.md §3): Roadmap, Features,
 * Review, System. A section is a PLACE in the IA, not a plugin's page — it
 * lists the contributions the platform composed into it (the same entries nav
 * shows under the section's link, from the same function over the same shared
 * fetch, so the two cannot disagree) and, when nothing landed there, says so
 * in words rather than vanishing.
 *
 * Today (`/`) is its own page — it keeps the widget grid until step 3's
 * banded composition (§5). */

import type { PluginEntry } from "../api";
import { Card, KindList, PendingNote, StateNote } from "../kinds/kinds";
import { usePlugins } from "../plugins-context";
import { sectionNavEntries, type SectionDef } from "../registry";
import { Layout } from "../shell/Layout";

function SectionTenants(props: { section: SectionDef; plugins: PluginEntry[] }) {
  const entries = sectionNavEntries(props.plugins, props.section);
  // §3: a section with nothing to show renders an explicit empty state (a
  // status message, so it's announced) — it never renders blank, and it never
  // drops out of nav.
  if (entries.length === 0) return <StateNote>{props.section.empty}</StateNote>;
  return <KindList items={entries.map((e) => ({ text: e.label, href: e.to }))} />;
}

export function SectionPage(props: { section: SectionDef }) {
  const plugins = usePlugins();
  return (
    <Layout title={props.section.label}>
      <Card>
        {plugins.state === "ready" ? (
          <SectionTenants section={props.section} plugins={plugins.data} />
        ) : (
          <PendingNote loadable={plugins} />
        )}
      </Card>
    </Layout>
  );
}
