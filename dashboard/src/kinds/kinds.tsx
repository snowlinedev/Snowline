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

import {
  createElement,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  fetchScopeSlugs,
  fetchUiData,
  postUiApi,
  UiApiError,
  type UIAction,
  type UIComposer,
} from "../api";
import { useData, type DataResult, type Loadable } from "../useData";

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

export function StateNote(props: {
  children: ReactNode;
  error?: boolean;
  /** Optional element id so other controls can point at the note (e.g. the
   * thread composer's disabled reason via `aria-describedby`). */
  id?: string;
  /** Extra class(es) appended after the state-note base classes. */
  className?: string;
}) {
  // Both arms are STATUS MESSAGES (WCAG 4.1.3): announced without moving
  // focus — errors assertively, loading/empty politely.
  const base = props.error ? "state-note state-error" : "state-note";
  return (
    <p
      id={props.id}
      className={props.className ? `${base} ${props.className}` : base}
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

/** The `thread` page's composer (shadow-conversations.md §4): the templated
 * plugin/path pair the shell POSTs to, plus the declaration's placeholder and
 * the `flags` value that greys it off. Assembled by `PluginPage`/
 * `RegisteredKind` from the registered `UIComposer` + the route params
 * already templated into `data`, exactly like the page's own data path. */
export type ThreadComposerConfig = {
  plugin: string;
  path: string;
  placeholder?: string;
  disabledWhen?: string;
};

/** Maps a composer POST failure to the spec's fixed user-facing copy
 * (shadow-conversations.md §4): 409 archived and 503 down-plugin get fixed,
 * reassuring copy; everything else (422/413 body-shape, network failure)
 * surfaces the server's own message verbatim — fail-visible, never a bare
 * status code. */
// ONE copy of the archived sentence: the pre-disabled reason note and the 409
// error path must say the same thing (two surfaces of one state).
const ARCHIVED_COPY = "This branch is archived — read-only.";

/** The greyed-composer reason. `disabled_when` is a MACHINE flag name
 * (manifest.py: "the plugin owns the semantics"), never display copy — the
 * known flag maps to its sentence and anything else gets neutral copy rather
 * than leaking the raw token into the UI. */
function composerDisabledReason(disabledWhen: string | undefined): string {
  if (disabledWhen === undefined || disabledWhen === "archived") {
    return ARCHIVED_COPY;
  }
  return "Read-only — this thread is not accepting new messages.";
}

function composerErrorMessage(err: unknown): string {
  if (err instanceof UiApiError) {
    if (err.status === 409) return ARCHIVED_COPY;
    if (err.status === 503) return "This plugin is currently down — try again shortly.";
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

/** The thread-foot composer (shadow-conversations.md §4): a labelled markdown
 * textarea + Send. Send POSTs `{ markdown }` through the `/ui-api` proxy,
 * disables while in flight, and on success clears the box and asks the
 * thread to refetch (no optimistic append — one source of truth). Errors
 * render as an inline, dismissible, assertive (`role="alert"`) note rather
 * than vanishing silently. `disabled` (the branch's `flags` matching
 * `disabledWhen`) greys the whole control off with a visible reason,
 * announced via the native `disabled` attribute + `aria-describedby`. */
function ThreadComposer(props: {
  composer: ThreadComposerConfig;
  disabled: boolean;
  onSent?: () => void;
}) {
  const [value, setValue] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  const reasonId = `${id}-reason`;
  const controlsDisabled = props.disabled || sending;
  // Ref latch, not state: two events in one tick (keydown burst + click)
  // both read a stale `sending === false` before React commits, so the state
  // flag alone can double-POST. The ref flips synchronously.
  const inflight = useRef(false);
  // Liveness: a send resolving after unmount (or after the route re-bound
  // this composer to a DIFFERENT branch) must not clear the new branch's
  // draft or trigger its refetch.
  const boundPath = useRef(props.composer.path);
  boundPath.current = props.composer.path;
  useEffect(() => {
    return () => {
      boundPath.current = null as unknown as string;
    };
  }, []);

  const send = () => {
    const markdown = value.trim();
    if (!markdown || controlsDisabled || inflight.current) return;
    inflight.current = true;
    const sentFor = props.composer.path;
    setSending(true);
    const stillLive = () => boundPath.current === sentFor;
    postUiApi(props.composer.plugin, props.composer.path, { markdown }).then(
      () => {
        inflight.current = false;
        if (!stillLive()) return;
        setSending(false);
        setError(null);
        setValue("");
        props.onSent?.();
      },
      (err) => {
        inflight.current = false;
        if (!stillLive()) return;
        setSending(false);
        setError(composerErrorMessage(err));
      },
    );
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Cmd/Ctrl+Enter submits; plain Enter falls through to the textarea's
    // native newline-insertion behavior (shadow-conversations.md §4).
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="thread-composer">
      {props.disabled && (
        <StateNote id={reasonId} className="thread-composer-reason">
          {composerDisabledReason(props.composer.disabledWhen)}
        </StateNote>
      )}
      {error != null && (
        <div className="thread-composer-error">
          <StateNote error>{error}</StateNote>
          <button type="button" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}
      <label className="thread-composer-label" htmlFor={id}>
        Reply
      </label>
      <textarea
        id={id}
        className="thread-composer-input"
        value={value}
        placeholder={props.composer.placeholder}
        disabled={controlsDisabled}
        aria-describedby={props.disabled ? reasonId : undefined}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="thread-composer-actions">
        <button
          type="button"
          disabled={controlsDisabled || value.trim() === ""}
          onClick={send}
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}

export function Thread(props: {
  title: string;
  meta?: ReactNode;
  nodes: ThreadNode[];
  flags?: string[];
  composer?: ThreadComposerConfig;
  onComposerSent?: () => void;
}) {
  const archived =
    props.composer?.disabledWhen != null &&
    (props.flags ?? []).includes(props.composer.disabledWhen);
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
      {props.composer && (
        <ThreadComposer
          composer={props.composer}
          disabled={archived}
          onSent={props.onComposerSent}
        />
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
  // `flags` is optional (shadow-conversations.md §5: present only when the
  // branch carries a status flag the composer's `disabled_when` keys on) but
  // malformed content still fails visible rather than being coerced.
  if (
    d.flags !== undefined &&
    (!Array.isArray(d.flags) || d.flags.some((f) => typeof f !== "string"))
  ) {
    return null;
  }
  return d as { title: string; meta?: ReactNode; nodes: ThreadNode[]; flags?: string[] };
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
  /** `thread` pages only (ui-shell.md §4.2/shadow-conversations.md §4): the
   * registered composer declaration, its route-templated POST path (already
   * resolved by the caller the same way the page's own `data` path is), and
   * a refetch callback wired to the page's own data hook. Absent for
   * composer-less threads and every other kind. */
  composer?: UIComposer | null;
  composerPath?: string;
  onComposerSent?: () => void;
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
      return (
        <Thread
          title={v.title}
          meta={v.meta}
          nodes={v.nodes}
          flags={v.flags}
          composer={
            props.composer && props.composerPath
              ? {
                  plugin: props.plugin,
                  path: props.composerPath,
                  placeholder: props.composer.placeholder,
                  disabledWhen: props.composer.disabled_when,
                }
              : undefined
          }
          onComposerSent={props.onComposerSent}
        />
      );
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

/* ---- page actions (ui-shell.md §5 actions[]) ------------------------------
 * The button/form-shaped write seam a page declares (issue #123), rendered
 * GENERICALLY: the shell knows only the declared label, endpoint, and field
 * list — never anything plugin-specific. A button toggles a minimal form of
 * the declared fields; submit POSTs the field values through the same
 * `/ui-api` proxy the composer uses; on a 2xx the shell follows an optional
 * plugin-relative `navigate` href in the response. */

/** Maps a page-action POST failure to user copy: a 503 down-plugin gets fixed,
 * reassuring text; everything else (422 validation, 409 conflict, network)
 * surfaces the server's own message verbatim — fail-visible, never a bare
 * status code. */
function actionErrorMessage(err: unknown): string {
  if (err instanceof UiApiError) {
    if (err.status === 503) return "This plugin is currently down — try again shortly.";
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

/** The action response's `navigate` href, CONFINED explicitly (same posture as
 * the markdown renderer's `safeHref` above, but stricter — a navigate target is
 * always an in-shell route, never an external link): it must start with a
 * SINGLE `/`. Everything else is DROPPED — treated as absent, so the form just
 * closes and no navigation happens. That rejects absolute URLs
 * (`https://evil.com`), scheme-looking values (`javascript:`, `data:`),
 * protocol-relative `//host`, and ANY backslash form (browsers treat `/\host`
 * and `\/host` like `//host`) — explicit here rather than an implicit reliance
 * on react-router v6 string-`to` semantics and pushState same-origin behavior
 * staying that way. */
function safeNavigateHref(nav: string | undefined): string | undefined {
  if (nav == null) return undefined;
  if (!nav.startsWith("/") || nav.startsWith("//") || nav.includes("\\")) {
    return undefined;
  }
  return nav;
}

/** The scope slugs backing a `scope`-kind field's `<datalist>` (ui-shell.md
 * §5.1). Fetched LAZILY and once per `enabled` transition — the caller only
 * enables it while the form is open AND actually declares a scope field, so a
 * form with no scope field (the common case) never hits `/scopes/tree`.
 *
 * The datalist is ASSISTANCE, not validation: loading and error both degrade
 * SILENTLY to an empty list — the field stays a plain text input, free text is
 * always accepted, and a failed scope fetch never surfaces an error or blocks
 * the form (a phone on a flaky tailnet still gets a working input). */
function useScopeSlugs(enabled: boolean): string[] {
  const [slugs, setSlugs] = useState<string[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    fetchScopeSlugs().then(
      (s) => {
        if (live) setSlugs(s);
      },
      () => {
        // Silent degrade — no error surface; the field is a plain input.
        if (live) setSlugs([]);
      },
    );
    return () => {
      live = false;
    };
  }, [enabled]);
  return slugs;
}

function PageAction(props: { plugin: string; action: UIAction }) {
  const navigate = useNavigate();
  const fields = props.action.fields ?? [];
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Ref latch (same rationale as the composer's): a keydown burst + click in
  // one tick both read a stale `submitting === false` before React commits, so
  // the state flag alone could double-POST. The ref flips synchronously.
  const inflight = useRef(false);
  const id = useId();

  // Scope-typeahead source: fetched only while the form is open AND a scope
  // field exists (ui-shell.md §5.1) — once per form open, degrading silently.
  const hasScopeField = fields.some((f) => f.kind === "scope");
  const scopeSlugs = useScopeSlugs(open && hasScopeField);

  const setField = (name: string, v: string) =>
    setValues((prev) => ({ ...prev, [name]: v }));

  const missingRequired = fields.some(
    (f) => f.required && (values[f.name] ?? "").trim() === "",
  );

  const close = () => {
    setOpen(false);
    setError(null);
    setValues({});
  };

  const submit = () => {
    if (submitting || inflight.current || missingRequired) return;
    inflight.current = true;
    setSubmitting(true);
    // Every declared field is sent (an empty optional field as "" — the plugin
    // owns what that means). The shell never invents fields the manifest
    // didn't declare.
    const body: Record<string, string> = {};
    for (const f of fields) body[f.name] = values[f.name] ?? "";
    postUiApi(props.plugin, props.action.endpoint, body).then(
      (resp) => {
        inflight.current = false;
        setSubmitting(false);
        setError(null);
        const nav =
          resp && typeof resp === "object" &&
          typeof (resp as { navigate?: unknown }).navigate === "string"
            ? (resp as { navigate: string }).navigate
            : undefined;
        // `navigate` is plugin-relative (§5) — CONFINED first
        // (safeNavigateHref: a non-in-shell value is dropped, never
        // followed), then re-prefixed with `/<plugin>`, same treatment as a
        // table row `href`. Absent/dropped = nothing to land on, so just
        // close the form.
        const to = prefixHref(props.plugin, safeNavigateHref(nav));
        if (to) navigate(to);
        else close();
      },
      (err) => {
        inflight.current = false;
        setSubmitting(false);
        setError(actionErrorMessage(err));
      },
    );
  };

  if (!open) {
    return (
      <button
        type="button"
        className="page-action-open"
        aria-expanded={false}
        onClick={() => setOpen(true)}
      >
        {props.action.label}
      </button>
    );
  }

  return (
    <form
      className="page-action-form"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      {error != null && (
        <div className="page-action-error">
          <StateNote error>{error}</StateNote>
          <button type="button" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}
      {fields.map((f) => {
        const fieldId = `${id}-${f.name}`;
        const label = f.label ?? f.name;
        return (
          <div key={f.name} className="page-action-field">
            <label htmlFor={fieldId}>
              {label}
              {!f.required && <span className="page-action-optional"> (optional)</span>}
            </label>
            {f.kind === "multiline" ? (
              <textarea
                id={fieldId}
                className="page-action-input"
                value={values[f.name] ?? ""}
                required={f.required}
                disabled={submitting}
                onChange={(e) => setField(f.name, e.target.value)}
              />
            ) : f.kind === "scope" ? (
              // A plain text input backed by a native <datalist> of the scope
              // slugs (ui-shell.md §5.1): iOS Safari renders it as native
              // suggestions and it's the lightest fully-accessible typeahead.
              // Free text stays allowed — the datalist is assistance, never a
              // restriction (PM-style scopes may not be registered yet). When
              // the fetch is loading or failed, `scopeSlugs` is empty and this
              // is indistinguishable from a plain text input.
              <>
                <input
                  id={fieldId}
                  type="text"
                  className="page-action-input"
                  list={`${fieldId}-scopes`}
                  value={values[f.name] ?? ""}
                  required={f.required}
                  disabled={submitting}
                  onChange={(e) => setField(f.name, e.target.value)}
                />
                <datalist id={`${fieldId}-scopes`}>
                  {scopeSlugs.map((slug) => (
                    <option key={slug} value={slug} />
                  ))}
                </datalist>
              </>
            ) : (
              <input
                id={fieldId}
                type="text"
                className="page-action-input"
                value={values[f.name] ?? ""}
                required={f.required}
                disabled={submitting}
                onChange={(e) => setField(f.name, e.target.value)}
              />
            )}
          </div>
        );
      })}
      <div className="page-action-actions">
        <button
          type="button"
          className="page-action-cancel"
          disabled={submitting}
          onClick={close}
        >
          Cancel
        </button>
        <button type="submit" disabled={submitting || missingRequired}>
          {submitting ? "Submitting…" : props.action.label}
        </button>
      </div>
    </form>
  );
}

/** All of a page's declared actions (ui-shell.md §5), rendered above the
 * page's kind content. Null when the page declares none — the read-only case,
 * unchanged. */
export function PageActions(props: { plugin: string; actions: UIAction[] }) {
  if (props.actions.length === 0) return null;
  return (
    <div className="page-actions">
      {props.actions.map((a) => (
        <PageAction key={a.id} plugin={props.plugin} action={a} />
      ))}
    </div>
  );
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
  pauseWhenHidden?: boolean,
): DataResult<unknown> {
  const renderable = contractOk && KNOWN_KINDS.has(kind);
  return useData(
    () => (renderable ? fetchUiData(plugin, path) : Promise.resolve(undefined)),
    refreshSeconds,
    [plugin, path, renderable],
    pauseWhenHidden ? { pauseWhenHidden: true } : undefined,
  );
}
