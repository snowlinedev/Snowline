# Dependency alerts (Dependabot)

Two lockfiles carry dependencies: `uv.lock` (one lockfile for the whole Python
workspace: platform, governance, memory, sdk, ops/remote-front) and
`dashboard/package-lock.json`.

**List open alerts**

    gh api repos/snowlinedev/Snowline/dependabot/alerts --paginate \
      -q '.[] | select(.state=="open") | [.number, .security_advisory.severity, .dependency.package.name, .dependency.manifest_path, .dependency.scope, (.security_vulnerability.first_patched_version.identifier // "none")] | @tsv'

**Bump**

- Python: `uv tree --invert --package <pkg>` shows which member pulls it in,
  then `uv lock --upgrade-package <pkg>`. Run the full suite and
  `uv run --package snowline-remote-front python -m pytest ops/remote-front/tests -q`.
  Deploy: `uv sync` and kickstart every hub service.
- Dashboard: `cd dashboard && npm audit fix` (never `--force`); a fix that needs
  a new major is a deliberate upgrade, not an alert chore. Then `npm test` and
  `npm run build`. Deploy: rebuild and kickstart the platform.

**Dismiss**

Dismiss only alerts that are clearly dev-only toolchain and not shipped
(vite, vitest, esbuild, postcss, browserslist and similar: the shipped
dashboard is a static bundle served by the platform):

    gh api -X PATCH repos/snowlinedev/Snowline/dependabot/alerts/<n> \
      -f state=dismissed -f dismissed_reason=not_used \
      -f dismissed_comment='dev-only build/test toolchain, not in the shipped bundle'

Never dismiss a runtime-reachable alert (Python packages, react-router) that
could not be fixed; leave it open and note why on the PR or work item.
