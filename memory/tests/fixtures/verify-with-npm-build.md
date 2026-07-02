---
name: verify-with-npm-build
description: verify dashboard changes with npm run build, not tsc --noEmit
metadata:
  type: gotcha
---
The dashboard deploy build is `tsc -b && vite build`, which is stricter than
`tsc --noEmit`. A `tsc --noEmit`-clean change once crash-looped the live
dashboard. Always verify `dashboard/` changes with `npm run build` and confirm it
serves.
