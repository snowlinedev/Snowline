// Atomic-ish dist promotion: vite builds into dist.staging, then this swaps
// it into place with two renames. Building straight into dist/ leaves a
// window (the whole build) where the platform's per-request /ui route can
// stream a PARTIALLY-WRITTEN content-hashed asset — and since hashed assets
// are served immutable, a truncated read is pinned in that browser until the
// next content-changing deploy. The rename window is microseconds, and a
// request landing inside it gets the route's clean no-cache 404 instead of
// poisoned content.
import { existsSync, renameSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const dist = join(root, "dist");
const staging = join(root, "dist.staging");
const old = join(root, "dist.old");

if (!existsSync(staging)) {
  console.error("promote-dist: dist.staging missing — did vite build fail?");
  process.exit(1);
}
rmSync(old, { recursive: true, force: true });
if (existsSync(dist)) renameSync(dist, old);
renameSync(staging, dist);
rmSync(old, { recursive: true, force: true });
console.log("promote-dist: dist.staging -> dist");
