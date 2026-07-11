# Releasing

Non-obvious — both must agree or the add-on build fails. The add-on Dockerfile
installs `git+…@v${BUILD_VERSION}` where `BUILD_VERSION` =
`ha_spark_addon/config.yaml` `version`. A release needs BOTH:

1. `version` bumped in `config.yaml` **on `master`** (the store advertises the
   default branch's version — a tag alone won't surface an update), and
2. a matching annotated `vX.Y.Z` git **tag pushed** (or the build fails with
   `pathspec 'vX.Y.Z' did not match`).

Sequence: commit bump → tag `vX.Y.Z` → push branch + tag → merge to `master`.
Keep `config.yaml` `options`/`schema` in sync with `config.py` `_OPTION_KEYS`
(a test enforces this); bump the version + `CHANGELOG.md` + `DOCS.md` for any
option/behaviour change.

## Add-on base image

Supervisor 2026.04.0+ ignores `build.yaml`/`BUILD_FROM` — set the base with
`FROM` in the Dockerfile. The `[habits]` ML extra (scikit-learn/numpy) has no
musllinux wheel, so the base is glibc (`python:3.13-slim-bookworm`); no
s6/bashio, so `run.sh` is plain shell and `config.yaml` sets `init: true`.
