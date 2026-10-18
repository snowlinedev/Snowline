/** The System page's Standby panel (#259): the rollup badge, per-peer rows,
 * and reasons — UNKNOWN styled neutral, never as healthy. */
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../src/App";
import { FIXTURES } from "./setup";

const READY = FIXTURES["/standby"];

function renderSystem() {
  return render(
    <MemoryRouter initialEntries={["/system"]}>
      <App />
    </MemoryRouter>,
  );
}

async function panel() {
  const heading = await screen.findByRole("heading", { name: "Standby" });
  const card = heading.closest("section");
  if (!card) throw new Error("no Standby card");
  await within(card).findByText(/Hub latest cut/);
  return within(card);
}

afterEach(() => {
  FIXTURES["/standby"] = READY;
});

describe("Standby panel", () => {
  it("shows READY with the peer's code and sync", async () => {
    renderSystem();
    const card = await panel();
    expect(card.getAllByText("READY").length).toBeGreaterThanOrEqual(2);
    expect(card.getByText("safe to fail over")).toBeTruthy();
    expect(card.getByText("MBP")).toBeTruthy();
    expect(card.getByText("v0.5.1 — current")).toBeTruthy();
    expect(card.getByText(/ok · v0\.5\.1/)).toBeTruthy();
  });

  it("shows DEGRADED with reasons and a code-behind count", async () => {
    FIXTURES["/standby"] = {
      rollup: "DEGRADED",
      reasons: ["MBP: data behind: pm hub->peer (40 pending, oldest 86400s)"],
      hub: { latest_cut: { version: "v0.5.3" }, autocut: null },
      peers: [
        {
          peer: "MBP",
          platform_url: "http://mbp.tail:8850",
          reachable: true,
          code: { peer_train: "v0.5.1", hub_train: "v0.5.3", behind_by_trains: 2, behind_by_minor: 0, flag: "behind" },
          data: {
            streams: [
              { participant: "pm", direction: "hub->peer", pending: 40, oldest_pending_age_s: 86400, last_success_at: null, version_hold: 0, flag: "behind" },
            ],
          },
          sync: null,
          front: { reachable: true, upstream: "hub", hub_healthy: true },
          rollup: "DEGRADED",
          reasons: ["data behind: pm hub->peer (40 pending, oldest 86400s)"],
        },
      ],
    };
    renderSystem();
    const card = await panel();
    expect(card.getAllByText("DEGRADED").length).toBe(2);
    expect(card.getByText("check before failing over")).toBeTruthy();
    expect(card.getByText("v0.5.1 — behind by 2 trains (hub v0.5.3)")).toBeTruthy();
    expect(card.getByText("1 stream, 1 behind, 40 pending")).toBeTruthy();
    expect(card.getByText("data behind: pm hub->peer (40 pending, oldest 86400s)")).toBeTruthy();
    expect(card.getByText("routing to hub, hub healthy")).toBeTruthy();
    expect(card.getByText("no sync report")).toBeTruthy();
  });

  it("styles an unreachable peer as UNKNOWN, not healthy", async () => {
    FIXTURES["/standby"] = {
      rollup: "UNKNOWN",
      reasons: ["MBP: peer platform unreachable (ConnectError)"],
      hub: { latest_cut: { version: "v0.5.1" } },
      peers: [
        {
          peer: "MBP",
          platform_url: "http://mbp.tail:8850",
          reachable: false,
          code: { peer_train: null, hub_train: "v0.5.1", behind_by_trains: null, behind_by_minor: null, flag: "unknown" },
          data: { streams: [] },
          sync: null,
          front: null,
          rollup: "UNKNOWN",
          reasons: ["peer platform unreachable (ConnectError)"],
        },
      ],
    };
    const { container } = renderSystem();
    const card = await panel();
    expect(card.getByText("unreachable")).toBeTruthy();
    expect(card.queryByText("READY")).toBeNull();
    expect(container.querySelector(".standby-peer-unknown .chip.unknown")).not.toBeNull();
  });

  it("explains an unpaired instance", async () => {
    FIXTURES["/standby"] = { rollup: "UNKNOWN", reasons: ["no paired standby peer"], peers: [], hub: {} };
    renderSystem();
    const card = await panel();
    expect(card.getByText("no paired standby peer")).toBeTruthy();
    expect(card.getByText("Hub latest cut unknown")).toBeTruthy();
  });
});
