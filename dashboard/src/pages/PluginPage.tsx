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
import { contractSupported, templateData } from "../registry";

export function PluginPage(props: { plugin: PluginEntry; page: UIPage }) {
  const params = useParams();
  const dataPath = templateData(props.page.data, params);
  const contractOk = contractSupported(props.plugin);
  const loadable = useUiData(
    props.plugin.name,
    dataPath,
    props.page.kind,
    contractOk,
  );

  return (
    <Layout title={props.page.title ?? props.page.id}>
      <Card>
        <RegisteredKind
          plugin={props.plugin.name}
          path={dataPath}
          kind={props.page.kind}
          contractOk={contractOk}
          loadable={loadable}
        />
      </Card>
    </Layout>
  );
}
