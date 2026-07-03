/** The v1 kind components (ui-shell.md §4) — the ONLY way anything renders a
 * stat, list, or table in the shell. Platform-native views consume these
 * exactly as registered plugin contributions will in phase 2, so density and
 * accessibility hold everywhere by construction. */

import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { Loadable } from "../useData";

export function Card(props: { title?: string; children: ReactNode }) {
  return (
    <section className="card">
      {props.title && <h2 className="card-title">{props.title}</h2>}
      {props.children}
    </section>
  );
}

/* ---- kind: stat ---------------------------------------------------------- */

export function Stat(props: { value: string | number; label: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{props.value}</div>
      <div className="stat-label">{props.label}</div>
    </div>
  );
}

/* ---- kind: list ---------------------------------------------------------- */

export type ListItem = { text: string; href?: string; meta?: ReactNode };

export function KindList(props: { items: ListItem[]; empty?: string }) {
  if (props.items.length === 0) {
    return <p className="state-note">{props.empty ?? "Nothing here."}</p>;
  }
  return (
    <ul className="kind-list">
      {props.items.map((item, i) => (
        <li key={i}>
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
