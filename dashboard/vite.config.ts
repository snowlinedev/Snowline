/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Served by the platform at /ui (ui-shell.md §6) — assets must resolve there.
export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    // Dev-only: the shell fetches the platform's JSON routes same-origin.
    proxy: {
      "/plugins": "http://127.0.0.1:8850",
      "/scopes": "http://127.0.0.1:8850",
      "/surfaces": "http://127.0.0.1:8850",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["tests/setup.ts"],
  },
});
