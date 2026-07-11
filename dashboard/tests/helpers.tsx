/** Shared test helpers — the fetch-stub Response builder every suite mocks
 * with, and the PageActions-in-a-router harness the actions[] suites drive.
 * Extracted so the copies can't drift (they were byte-identical). */
import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { UIAction } from "../src/api";
import { PageActions } from "../src/kinds/kinds";

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** `PageActions` in a router whose destination route renders a marker
 * ("LANDED HERE"), so a successful `navigate` is observable. */
export function renderActionsInRouter(actions: UIAction[]) {
  return render(
    <MemoryRouter initialEntries={["/governance/shadow"]}>
      <Routes>
        <Route
          path="/governance/shadow"
          element={<PageActions plugin="governance" actions={actions} />}
        />
        <Route path="/governance/shadow/:branch" element={<div>LANDED HERE</div>} />
      </Routes>
    </MemoryRouter>,
  );
}
