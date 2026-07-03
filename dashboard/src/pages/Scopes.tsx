import { fetchScopeTree, type ScopeNode } from "../api";
import { Card, PendingNote, StateNote } from "../kinds/kinds";
import { Layout } from "../shell/Layout";
import { useData } from "../useData";


function TreeLevel(props: { nodes: ScopeNode[] }) {
  return (
    <ul>
      {props.nodes.map((n) => (
        <li key={n.slug}>
          <div className="node">
            <span>{n.name}</span>
            <span className="slug">{n.slug}</span>
            {n.isolated && <span className="slug">(isolated)</span>}
          </div>
          {n.children.length > 0 && <TreeLevel nodes={n.children} />}
        </li>
      ))}
    </ul>
  );
}

export function Scopes() {
  const tree = useData(fetchScopeTree, 60);
  return (
    <Layout title="Scopes">
      <Card>
        {tree.state === "ready" ? (
          tree.data.length ? (
            <div className="tree">
              <TreeLevel nodes={tree.data} />
            </div>
          ) : (
            <StateNote>No scopes yet.</StateNote>
          )
        ) : (
          <PendingNote loadable={tree} />
        )}
      </Card>
    </Layout>
  );
}
