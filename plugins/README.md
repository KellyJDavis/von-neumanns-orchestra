# plugins/

Anything that cannot be open-sourced lives here.

- Excluded from the default install and from all benchmark configurations, enforced in CI.
- No module under `packages/*` may import from `plugins/`, in either direction — this boundary is checked in CI
  (see `.github/workflows/ci.yml`) and is what keeps every `packages/*` entry independently publishable under
  Apache-2.0.

This directory is intentionally empty until something needs to live here.
