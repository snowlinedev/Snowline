/** A registered plugin page (ui-shell.md §3/§4.2/§6): renders through the
 * exact same kind vocabulary + fail-visible dispatch as everything else
 * (`RegisteredKind`), so density/a11y hold by construction. Mounted by
 * App.tsx at the page's plugin-namespaced router path; `useParams` supplies
 * whatever path params the route declared (e.g. `{branch}` -> `branch`),
 * which are templated into the page's `data` path here. */

import { useParams } from "react-router-dom";

import type { PluginEntry, UIPage } from "../api";
import { Card, RegisteredKind, useUiData } from "../kinds/kinds";
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

  return (
    <Layout title={props.page.title ?? props.page.id}>
      <Card>
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
