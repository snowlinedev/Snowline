/** Today (`/`) — the owner's briefing surface, composed into bands
 * (dashboard-ia.md §5): **Needs you** (`attention`), **Digest** (`digest`),
 * **Recent activity** (`activity`), then one "More from <plugin>" fallback
 * band per plugin whose home widgets declared no recognized intent.
 *
 * Composition is LAYOUT, never synthesis (§2.3): a band is N contributions
 * from N plugins arranged under one heading — nothing is merged, joined, or
 * interpreted here. Every widget keeps its own `useUiData` poll, so a slow or
 * down plugin greys out its own card and cannot blank the page.
 *
 * The machine's own health (plugins up, plugin status, surfaces) is NOT here:
 * §3 moves those native cards to System — Today is the owner's work.
 */

import { useId } from "react";

import type { PluginEntry, UIWidget } from "../api";
import { Card, KindList, PendingNote, RegisteredKind, StateNote, useUiData } from "../kinds/kinds";
import { usePlugins } from "../plugins-context";
import {
  clampRefreshSeconds,
  contractSupported,
  sectionDef,
  todayBands,
  type TodayBand,
} from "../registry";
import { Layout } from "../shell/Layout";

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
    // h3: the card sits under its band's h2 heading (see Band below).
    <Card title={props.widget.title ?? props.widget.id} headingLevel={3}>
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

/** One band: a REAL heading (§7 — h2, the level below Layout's page h1) over
 * the contributions the platform placed in it, so the document outline mirrors
 * the visual bands. Labelled `<section>`, so assistive tech can navigate
 * band-by-band rather than through one flat run of cards.
 *
 * Pages come first as a single links card — a page is a destination (an entry
 * point into the full view), and grouping them keeps one band from reading as
 * a row of near-empty cards — then the self-polling widget cards, in
 * plugin-registration order (§4.2). */
function Band(props: { band: TodayBand }) {
  // Minted, not derived from the band key: an id built from a plugin-supplied
  // name could collide after any normalization, and a duplicate idref breaks
  // the labelling for everyone (same reason the board rows mint theirs).
  const headingId = useId();
  return (
    <section className="band" aria-labelledby={headingId}>
      <h2 className="band-heading" id={headingId}>
        {props.band.heading}
      </h2>
      <div className="grid">
        {props.band.pages.length > 0 && (
          <Card>
            <KindList
              items={props.band.pages.map((e) => ({ text: e.label, href: e.to }))}
            />
          </Card>
        )}
        {props.band.widgets.map(({ key, plugin, widget }) => (
          <WidgetCard key={key} plugin={plugin} widget={widget} />
        ))}
      </div>
    </section>
  );
}

export function Today() {
  const plugins = usePlugins();
  const bands = plugins.state === "ready" ? todayBands(plugins.data) : [];

  if (plugins.state !== "ready") {
    return (
      <Layout title="Today">
        <Card>
          <PendingNote loadable={plugins} />
        </Card>
      </Layout>
    );
  }

  return (
    <Layout title="Today">
      {bands.length === 0 ? (
        // §3: nothing composed here says so in words (a status message, so
        // it's announced) — Today never renders blank.
        <Card>
          <StateNote>{sectionDef("today").empty}</StateNote>
        </Card>
      ) : (
        <div className="page-stack">
          {bands.map((band) => (
            <Band key={band.key} band={band} />
          ))}
        </div>
      )}
    </Layout>
  );
}
