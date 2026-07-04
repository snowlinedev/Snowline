/** The v1 kind components (ui-shell.md §4) — the ONLY way anything renders a
 * stat, list, table, thread, or document in the shell. Platform-native views
 * consume these exactly as registered plugin contributions do (phase 2), so
 * density and accessibility hold everywhere by construction.
 *
 * `RegisteredKind` (bottom of file) is the phase-2 entry point: given a
 * plugin's declared `kind` + a `Loadable` of its `/ui-api` response, it maps
 * kind -> component, fails visible on an unknown kind / unsupported contract
 * version (§4.4), and renders an error card on a malformed response — the
 * ONLY code path registered widgets/pages render through. */

import { createElement, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { fetchUiData } from "../api";
import { useData, type Loadable } from "../useData";

export function Card(props: { title?: string; children: ReactNode }) {
  return (
    <section className="card">
      {props.title && <h2 className="card-title">{props.title}</h2>}
      {props.children}
    </section>
  );
}

/* ---- kind: stat ---------------------------------------------------------- */

export function Stat(props: {
  value: string | number;
  label?: string;
  delta?: string | number;
  intent?: "good" | "bad" | "neutral" | string;
}) {
  return (
    <div className="stat">
      <div className="stat-value">{props.value}</div>
      {props.label && <div className="stat-label">{props.label}</div>}
      {props.delta != null && (
        <div className={`stat-delta intent-${props.intent ?? "neutral"}`}>
          {props.delta}
        </div>
      )}
    </div>
  );
}

/* ---- kind: list ---------------------------------------------------------- */

export type ListItem = {
  text: string;
  href?: string;
  meta?: ReactNode;
  intent?: "good" | "bad" | "neutral" | string;
};

export function KindList(props: { items: ListItem[]; empty?: string }) {
  if (props.items.length === 0) {
    return <p className="state-note">{props.empty ?? "Nothing here."}</p>;
  }
  return (
    <ul className="kind-list">
      {props.items.map((item, i) => (
        <li key={i}>
          {/* intent is a decoration alongside the text label, never a
           * color-only signal (ACCESSIBILITY.md 1.4.1). */}
          {item.intent && (
            <span className={`dot intent-${item.intent}`} aria-hidden="true" />
          )}
          {item.href ? <Link to={item.href}>{item.text}</Link> : item.text}
          {item.meta != null && <span className="meta">{item.meta}</span>}
        </li>
      ))}
    </ul>
  );
}

/* ---- kind: table --------------------------------------------------------- */

export type Column = { key: string; label: string };
export type Row = { cells: Record<string, ReactNode>; href?: string };

export function KindTable(props: {
  columns: Column[];
  rows: Row[];
  caption: string; // screen-reader table name; visually the card title serves
  empty?: string;
}) {
  if (props.rows.length === 0) {
    return <p className="state-note">{props.empty ?? "Nothing here."}</p>;
  }
  return (
    // Wide tables scroll inside their own container (reflow, 1.4.10): the
    // page never scrolls horizontally; the data table may.
    <div className="table-scroll">
      <table className="kind-table">
        <caption className="sr-only">{props.caption}</caption>
        <thead>
          <tr>
            {props.columns.map((c) => (
              <th key={c.key} scope="col">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map((row, i) => (
            <tr key={i}>
              {props.columns.map((c, j) => (
                <td key={c.key}>
                  {j === 0 && row.href ? (
                    <Link to={row.href}>{row.cells[c.key]}</Link>
                  ) : (
                    row.cells[c.key]
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---- status chip: dot + label, never color alone ------------------------- */

export function StatusChip(props: { status: "up" | "down" | "unknown" }) {
  return (
    <span className={`chip ${props.status}`}>
      <span className="dot" aria-hidden="true" />
      {props.status}
    </span>
  );
}

/* ---- loading/error states ------------------------------------------------ */

export function StateNote(props: { children: ReactNode; error?: boolean }) {
  // Both arms are STATUS MESSAGES (WCAG 4.1.3): announced without moving
  // focus — errors assertively, loading/empty politely.
  return (
    <p
      className={props.error ? "state-note state-error" : "state-note"}
      role={props.error ? "alert" : "status"}
    >
      {props.children}
    </p>
  );
}

/** The shared renderer for a Loadable's non-ready states (null when ready —
 * the caller renders its kind component). */
export function PendingNote(props: { loadable: Loadable<unknown> }) {
  if (props.loadable.state === "ready") return null;
  return props.loadable.state === "loading" ? (
    <StateNote>Loading…</StateNote>
  ) : (
    <StateNote error>Failed to load: {props.loadable.message}</StateNote>
  );
}

/* ---- markdown: a tiny SAFE subset renderer --------------------------------
 *
 * Plugin data is "trusted-ish" (§4.4 rationale) but the shell must not be an
 * XSS vector, so this NEVER uses dangerouslySetInnerHTML: every run of
 * plugin-supplied text becomes a React text child, and React always escapes
 * text children — a literal "<script>" in markdown source can only ever
 * render as the text "<script>", never as a DOM element. That property holds
 * regardless of how much of the tokenizer below is right or wrong, which is
 * the point: the sanitizer is "never build an element from a string", not
 * "get the tokenizer correct."
 *
 * Deliberately no markdown dependency: thread/document content (shadow
 * discussion turns, branch notes) is short-form authored text, not a document
 * editor's worth of syntax, so a ~60-line subset (paragraphs, #-headings,
 * bold/italic/code, [text](url) links, -/1. lists) covers it without adding a
 * bundle/security-surface for parsing full CommonMark. */

function safeHref(href: string): string | undefined {
  // Deliberately narrow: relative paths, in-page anchors, and http(s) links.
  // Everything else (javascript:, data:, mailto: oddities, etc.) is dropped —
  // the link text still renders, just not as a clickable href.
  if (href.startsWith("/") || href.startsWith("#") || /^https?:\/\//i.test(href)) {
    return href;
  }
  return undefined;
}

const INLINE_RE =
  /`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*|_([^_]+)_/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let i = 0;
  INLINE_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = INLINE_RE.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const key = `${keyPrefix}-${i++}`;
    if (m[1] !== undefined) {
      nodes.push(<code key={key}>{m[1]}</code>);
    } else if (m[2] !== undefined) {
      const href = safeHref(m[3]);
      nodes.push(
        href ? (
          <a key={key} href={href}>
            {m[2]}
          </a>
        ) : (
          m[2]
        ),
      );
    } else if (m[4] !== undefined) {
      nodes.push(<strong key={key}>{m[4]}</strong>);
    } else {
      nodes.push(<em key={key}>{m[5] ?? m[6]}</em>);
    }
    last = INLINE_RE.lastIndex;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// Offset so a plugin's markdown heading can never outrank — or, just as
// importantly, SKIP a level below — the container's own heading: Layout's
// page title is h1, Thread/Document's own title (below) is h2, so the first
// markdown heading level starts at h3 (axe's heading-order rule flags any
// skipped level in rendered order, not just outranking).
const HEADING_TAGS = ["h3", "h4", "h5", "h6", "h6", "h6"] as const;

/** Renders a markdown STRING as safe React elements — see the file-level
 * comment above for the sanitization argument. Block grammar: blank-line
 * separated paragraphs; `#`..`######` headings (offset to h3..h6, see above);
 * `-`/`*` or `1.`/`1)` lists; everything else is a paragraph with inline
 * formatting. */
export function Markdown(props: { text: string }) {
  const lines = props.text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let key = 0;

  const flushPara = () => {
    if (para.length === 0) return;
    const id = key++;
    blocks.push(<p key={`p-${id}`}>{renderInline(para.join(" "), `p-${id}`)}</p>);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const id = key++;
    const items = list.items;
    blocks.push(
      list.ordered ? (
        <ol key={`l-${id}`}>
          {items.map((item, i) => (
            <li key={i}>{renderInline(item, `l-${id}-${i}`)}</li>
          ))}
        </ol>
      ) : (
        <ul key={`l-${id}`}>
          {items.map((item, i) => (
            <li key={i}>{renderInline(item, `l-${id}-${i}`)}</li>
          ))}
        </ul>
      ),
    );
    list = null;
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line === "") {
      flushPara();
      flushList();
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara();
      flushList();
      const id = key++;
      const tag = HEADING_TAGS[heading[1].length - 1];
      blocks.push(createElement(tag, { key: `h-${id}` }, renderInline(heading[2], `h-${id}`)));
      continue;
    }
    const ordered = /^\d+[.)]\s+(.*)$/.exec(line);
    const unordered = /^[-*]\s+(.*)$/.exec(line);
    if (ordered || unordered) {
      flushPara();
      const isOrdered = !!ordered;
      const content = (ordered ?? unordered)![1];
      if (!list || list.ordered !== isOrdered) {
        flushList();
        list = { ordered: isOrdered, items: [] };
      }
      list.items.push(content);
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();

  return <div className="markdown">{blocks}</div>;
}

/* ---- kind: thread ---------------------------------------------------------
 * The shadow discussion view (ui-shell.md §4.2): ordered authored markdown
 * nodes, each with an author/kind/timestamp header line and optional
 * citations. */

export type ThreadNode = {
  author: string;
  kind: string;
  markdown: string;
  at: string;
  citations?: unknown[];
};

export function Thread(props: { title: string; meta?: ReactNode; nodes: ThreadNode[] }) {
  return (
    <div className="thread">
      {/* h2: the next level below Layout's own page h1 (heading-order,
       * WCAG 1.3.1/axe) — the page never titles its wrapping Card, so this
       * is the first heading after the page title. */}
      <h2 className="thread-title">{props.title}</h2>
      {props.meta != null && <div className="thread-meta meta">{props.meta}</div>}
      {props.nodes.length === 0 ? (
        <p className="state-note">Nothing here.</p>
      ) : (
        <ol className="thread-nodes">
          {props.nodes.map((node, i) => (
            <li key={i} className="thread-node">
              <div className="thread-node-header">
                <span className="thread-node-author">{node.author}</span>
                <span className="thread-node-kind">{node.kind}</span>
                <time className="thread-node-at" dateTime={node.at}>
                  {node.at}
                </time>
              </div>
              <Markdown text={node.markdown} />
              {node.citations != null && node.citations.length > 0 && (
                <p className="thread-node-citations meta">
                  {node.citations.map((c) => String(c)).join(", ")}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/* ---- kind: document -------------------------------------------------------
 * A rendered markdown document with a title and optional metadata (e.g.
 * branch narrative notes). */

export function Document(props: { title: string; markdown: string; meta?: ReactNode }) {
  return (
    <div className="document">
      <h2 className="document-title">{props.title}</h2>
      {props.meta != null && <div className="document-meta meta">{props.meta}</div>}
      <Markdown text={props.markdown} />
    </div>
  );
}

/* ---- registered-contribution dispatch (ui-shell.md §4.4 fail-visible) ----- */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

const KNOWN_KINDS = new Set(["stat", "list", "table", "thread", "document"]);

function validateStat(d: unknown) {
  if (!isRecord(d)) return null;
  if (typeof d.value !== "string" && typeof d.value !== "number") return null;
  if (d.label !== undefined && typeof d.label !== "string") return null;
  return d as {
    value: string | number;
    label?: string;
    delta?: string | number;
    intent?: string;
  };
}

function validateListData(d: unknown) {
  if (!isRecord(d) || !Array.isArray(d.items)) return null;
  for (const item of d.items) {
    if (!isRecord(item) || typeof item.text !== "string") return null;
  }
  return d as { items: ListItem[]; empty?: string };
}

function validateTableData(d: unknown) {
  if (!isRecord(d) || !Array.isArray(d.columns) || !Array.isArray(d.rows)) return null;
  for (const c of d.columns) {
    if (!isRecord(c) || typeof c.key !== "string" || typeof c.label !== "string") {
      return null;
    }
  }
  for (const r of d.rows) {
    if (!isRecord(r) || !isRecord(r.cells)) return null;
  }
  return d as { columns: Column[]; rows: Row[]; empty?: string };
}

function validateThreadData(d: unknown) {
  if (!isRecord(d) || typeof d.title !== "string" || !Array.isArray(d.nodes)) {
    return null;
  }
  for (const n of d.nodes) {
    if (
      !isRecord(n) ||
      typeof n.author !== "string" ||
      typeof n.kind !== "string" ||
      typeof n.markdown !== "string" ||
      typeof n.at !== "string"
    ) {
      return null;
    }
  }
  return d as { title: string; meta?: ReactNode; nodes: ThreadNode[] };
}

function validateDocumentData(d: unknown) {
  if (!isRecord(d) || typeof d.title !== "string" || typeof d.markdown !== "string") {
    return null;
  }
  return d as { title: string; markdown: string; meta?: ReactNode };
}

/** Row/item `href` values are SHELL routes, and a plugin only knows its OWN
 * page namespace (ui-shell.md §3) — so a leading-'/' href from a plugin's
 * data response is treated as plugin-relative and re-prefixed with
 * `/<plugin>` here, mirroring the same `/<plugin>/<route>` namespacing
 * App.tsx applies to registered page routes. A non-'/' href (shouldn't
 * happen; nothing plugin data could legitimately produce) passes through
 * unchanged rather than guessing. */
function prefixHref(plugin: string, href: string | undefined): string | undefined {
  if (href == null) return undefined;
  return href.startsWith("/") ? `/${plugin}${href}` : href;
}

export function UnsupportedKindCard(props: { plugin: string; kind: string }) {
  // A single template-literal child (rather than text/expression children
  // spanning multiple JSX source lines) so the rendered text is exactly the
  // spec's wording (ui-shell.md §4.4) with no JSX whitespace-collapsing
  // surprises.
  return (
    <StateNote>
      {`${props.plugin} offers a view this platform version can't render (kind '${props.kind}')`}
    </StateNote>
  );
}

export function MalformedDataCard(props: { plugin: string; path: string }) {
  return (
    <StateNote error>
      {props.plugin} returned malformed data for {props.path}
    </StateNote>
  );
}

/** The ONE place a registered widget/page's `kind` becomes a rendered
 * component (ui-shell.md §4). Fails visible (§4.4) rather than dropping
 * silently: an unsupported `contract_version` or unknown `kind` renders the
 * placeholder card BEFORE ever looking at `loadable` (no need to wait on a
 * fetch we can't do anything with); a fetch that succeeded but doesn't match
 * the kind's minimal shape renders the malformed-data error card, named with
 * the plugin + the exact path that was fetched. */
export function RegisteredKind(props: {
  plugin: string;
  path: string;
  kind: string;
  contractOk: boolean;
  loadable: Loadable<unknown>;
}) {
  if (!props.contractOk || !KNOWN_KINDS.has(props.kind)) {
    return <UnsupportedKindCard plugin={props.plugin} kind={props.kind} />;
  }
  if (props.loadable.state !== "ready") {
    return <PendingNote loadable={props.loadable} />;
  }
  const data = props.loadable.data;
  const malformed = <MalformedDataCard plugin={props.plugin} path={props.path} />;

  switch (props.kind) {
    case "stat": {
      const v = validateStat(data);
      if (!v) return malformed;
      return <Stat value={v.value} label={v.label} delta={v.delta} intent={v.intent} />;
    }
    case "list": {
      const v = validateListData(data);
      if (!v) return malformed;
      return (
        <KindList
          items={v.items.map((item) => ({
            ...item,
            href: prefixHref(props.plugin, item.href),
          }))}
          empty={v.empty}
        />
      );
    }
    case "table": {
      const v = validateTableData(data);
      if (!v) return malformed;
      return (
        <KindTable
          caption={`Data from ${props.plugin}`}
          columns={v.columns}
          rows={v.rows.map((row) => ({ ...row, href: prefixHref(props.plugin, row.href) }))}
          empty={v.empty}
        />
      );
    }
    case "thread": {
      const v = validateThreadData(data);
      if (!v) return malformed;
      return <Thread title={v.title} meta={v.meta} nodes={v.nodes} />;
    }
    case "document": {
      const v = validateDocumentData(data);
      if (!v) return malformed;
      return <Document title={v.title} markdown={v.markdown} meta={v.meta} />;
    }
    default:
      return <UnsupportedKindCard plugin={props.plugin} kind={props.kind} />;
  }
}

/** Fetch-and-poll hook for a registered contribution's `/ui-api` data. Skips
 * the network call entirely when the contribution isn't renderable (unknown
 * kind / unsupported contract version) — `RegisteredKind` shows the §4.4
 * placeholder immediately rather than waiting on a fetch whose result it
 * would just discard. */
export function useUiData(
  plugin: string,
  path: string,
  kind: string,
  contractOk: boolean,
  refreshSeconds?: number,
): Loadable<unknown> {
  const renderable = contractOk && KNOWN_KINDS.has(kind);
  return useData(
    () => (renderable ? fetchUiData(plugin, path) : Promise.resolve(undefined)),
    refreshSeconds,
    [plugin, path, renderable],
  );
}
