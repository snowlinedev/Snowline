/** A registered plugin page (ui-shell.md §3/§4.2/§6): renders through the
 * exact same kind vocabulary + fail-visible dispatch as everything else
 * (`RegisteredKind`), so density/a11y hold by construction. Mounted by
 * App.tsx at the page's plugin-namespaced router path; `useParams` supplies
 * whatever path params the route declared (e.g. `{branch}` -> `branch`),
 * which are templated into the page's `data` path here. */

import { useParams } from "react-router-dom";

import type { PluginEntry, UIPage } from "../api";
import { Card, PageActions, RegisteredKind, useUiData } from "../kinds/kinds";
import { Layout } from "../shell/Layout";
import { THREAD_COMPOSER_POLL_SECONDS, contractSupported, templateData } from "../registry";

export function PluginPage(props: { plugin: PluginEntry; page: UIPage }) {
  const params = useParams();
  const dataPath = templateData(props.page.data, params);
  const contractOk = contractSupported(props.plugin);
  // shadow-conversations.md §4: only thread pages that declare a composer
  // poll (5s, paused when hidden) — every other page/kind keeps its
  // fetch-once-on-mount behavior unchanged.
  const composer = props.page.composer ?? undefined;
  const pollsForComposer = props.page.kind === "thread" && composer != null;
  const loadable = useUiData(
    props.plugin.name,
    dataPath,
    props.page.kind,
    contractOk,
    pollsForComposer ? THREAD_COMPOSER_POLL_SECONDS : undefined,
    pollsForComposer,
  );
  const composerPath = composer ? templateData(composer.endpoint, params) : undefined;
  // Page-level actions (ui-shell.md §5): rendered generically above the page's
  // kind content. Endpoints are route-templated the same way `data`/`composer`
  // are, so an action on a `{param}` route reaches the right concrete path.
  const actions = (props.page.actions ?? []).map((a) => ({
    ...a,
    endpoint: templateData(a.endpoint, params),
  }));

  // Additive `page_title` (ui-shell.md §4.2): a page payload may name the
  // concrete entity behind a param-keyed route (which milestone, which
  // branch) — id-keyed routes carry an opaque id, so only the data plane
  // knows a human name. Manifest title stays the pre-load and fallback
  // header.
  const payload = loadable.state === "ready" ? loadable.data : undefined;
  const payloadTitle =
    typeof payload === "object" &&
    payload !== null &&
    !Array.isArray(payload) &&
    typeof (payload as { page_title?: unknown }).page_title === "string" &&
    (payload as { page_title: string }).page_title.trim() !== ""
      ? (payload as { page_title: string }).page_title
      : undefined;

  return (
    <Layout title={payloadTitle ?? props.page.title ?? props.page.id}>
      <Card>
        <PageActions plugin={props.plugin.name} actions={actions} />
        <RegisteredKind
          plugin={props.plugin.name}
          path={dataPath}
          kind={props.page.kind}
          contractOk={contractOk}
          loadable={loadable}
          composer={composer}
          composerPath={composerPath}
          onComposerSent={loadable.reload}
        />
      </Card>
    </Layout>
  );
}
