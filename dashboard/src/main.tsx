import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { applyPrefs, currentDensity, currentTheme } from "./prefs";
import "./tokens.css";
import "./app.css";

// Apply persisted theme/density before first paint (no flash of wrong theme).
applyPrefs(currentTheme(), currentDensity());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Served by the platform at /ui — the router must agree. */}
    <BrowserRouter basename="/ui">
      <App />
    </BrowserRouter>
  </StrictMode>,
);
