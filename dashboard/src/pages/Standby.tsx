/** The Standby panel (issue #259) on System: "can I fail over right now?" as
 * one glance — the rollup badge, then per paired peer its code / data / sync /
 * front rows and the reasons behind anything short of READY.
 *
 * Reads `GET /standby` (the platform computes and caches it; this only lays
 * it out). The rollup is shown as text beside a dot, never color alone
 * (ACCESSIBILITY.md 1.4.1). An unreachable peer is UNKNOWN, styled neutral —
 * "can't tell" must not read as either healthy or broken. */

import type { Standby, StandbyPeer, StandbyRollup, StandbyStream } from "../api";
import { fetchStandby } from "../api";
import { LoadableCard } from "../kinds/kinds";
import { useData } from "../useData";

const CHIP: Record<StandbyRollup, string> = {
  READY: "up",
  DEGRADED: "degraded",
  UNKNOWN: "unknown",
};

export function RollupChip(props: { rollup: StandbyRollup }) {
  return (
    <span className={`chip ${CHIP[props.rollup] ?? "unknown"}`}>
      <span className="dot" aria-hidden="true" />
      {props.rollup}
    </span>
  );
}

function codeText(code: StandbyPeer["code"]): string {
  const peer = code.peer_train ?? "unknown";
  const hub = code.hub_train ?? "unknown";
  if (code.flag === "current") return `${peer} — current`;
  if (code.flag === "behind") {
    const gap =
      code.behind_by_trains != null
        ? `${code.behind_by_trains} train${code.behind_by_trains === 1 ? "" : "s"}`
        : `${code.behind_by_minor ?? "?"} minor`;
    return `${peer} — behind by ${gap} (hub ${hub})`;
  }
  if (code.flag === "ahead") return `${peer} — ahead of hub ${hub}`;
  return `${peer} vs hub ${hub} — unknown`;
}

function dataText(streams: StandbyStream[]): string {
  if (streams.length === 0) return "no streams reported";
  const behind = streams.filter((s) => s.flag === "behind").length;
  const unknown = streams.filter((s) => s.flag === "unknown").length;
  const pending = streams.reduce((n, s) => n + (s.pending ?? 0), 0);
  const parts = [`${streams.length} stream${streams.length === 1 ? "" : "s"}`];
  if (behind) parts.push(`${behind} behind`);
  if (unknown) parts.push(`${unknown} unknown`);
  parts.push(`${pending} pending`);
  return parts.join(", ");
}

function syncText(sync: StandbyPeer["sync"]): string {
  if (!sync) return "no sync report";
  return [sync.last_outcome ?? "?", sync.version, sync.at && `at ${sync.at}`]
    .filter(Boolean)
    .join(" · ");
}

function frontText(front: StandbyPeer["front"]): string | null {
  if (!front) return null;
  if (front.reachable === false) return "front unreachable";
  const hub = front.hub_healthy == null ? "" : front.hub_healthy ? ", hub healthy" : ", hub unhealthy";
  return `routing to ${front.upstream ?? "?"}${hub}`;
}

function PeerRow(props: { peer: StandbyPeer }) {
  const p = props.peer;
  const front = frontText(p.front);
  return (
    <li className={`standby-peer standby-peer-${p.rollup.toLowerCase()}`}>
      <div className="standby-peer-head">
        <span className="standby-peer-name">{p.peer}</span>
        <RollupChip rollup={p.rollup} />
      </div>
      <dl className="standby-facts">
        <dt>Code</dt>
        <dd>{codeText(p.code)}</dd>
        <dt>Data</dt>
        <dd>{p.reachable || p.data.streams.length ? dataText(p.data.streams) : "unreachable"}</dd>
        <dt>Sync</dt>
        <dd>{syncText(p.sync)}</dd>
        {front && (
          <>
            <dt>Front</dt>
            <dd>{front}</dd>
          </>
        )}
      </dl>
      {p.reasons.length > 0 && (
        <ul className="standby-reasons">
          {p.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function StandbyBody(props: { data: Standby }) {
  const d = props.data;
  const cut = d.hub?.latest_cut?.version;
  const autocut = d.hub?.autocut;
  return (
    <div className={`standby standby-${d.rollup.toLowerCase()}`}>
      <p className="standby-rollup">
        <RollupChip rollup={d.rollup} />
        <span className="standby-caption">
          {d.rollup === "READY" ? "safe to fail over" : "check before failing over"}
        </span>
      </p>
      <p className="standby-hub">
        Hub latest cut {cut ?? "unknown"}
        {autocut?.last_outcome ? ` · last auto-cut ${autocut.last_outcome}` : ""}
      </p>
      {d.peers.length === 0 ? (
        <ul className="standby-reasons">
          {d.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      ) : (
        <ul className="standby-peers">
          {d.peers.map((p) => (
            <PeerRow key={p.peer} peer={p} />
          ))}
        </ul>
      )}
    </div>
  );
}

export function StandbyCard() {
  const standby = useData(fetchStandby, 30);
  return (
    <LoadableCard title="Standby" loadable={standby}>
      {(data) => <StandbyBody data={data} />}
    </LoadableCard>
  );
}
