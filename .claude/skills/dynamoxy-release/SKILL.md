---
name: dynamoxy-release
description: Prepare and publish a dynamoxy release - version bump, changelog from conventional commits, release PR, tag, wheels for every platform, GitHub release and PyPI. Use when asked to release, bump the version, or fix the release pipeline.
---

# Releasing dynamoxy

1. **Check main is green** (CI; `gh run list --workflow ci.yml --branch main --limit 1`) and
   `CHANGELOG.md` `[Unreleased]` notes are accurate.
2. **Prepare**: GitHub → Actions → "Prepare release" → version (`0.2.0`, or `patch`/`minor`/
   `major`; while 0.x, `major` bumps the minor), or
   `gh workflow run prepare-release.yml -f version=<version>`. It runs `scripts/release.py` (bumps
   `[workspace.package].version` and the `dynamoxy-core` dependency, `cargo update --workspace`,
   generates the CHANGELOG section with git-cliff; the first release dates the `[0.1.0] -
   Unreleased` section) and opens a PR titled `chore(release): vX.Y.Z`.
   Locally: `uv run --no-project scripts/release.py 0.2.0`.
3. **Polish** the CHANGELOG section in the PR (it becomes the GitHub release notes via
   `scripts/release_notes.py`), with commits on its branch `release/vX.Y.Z`, and squash-merge it.
   If the docs check fails because a deprecated API is due for removal in this version
   (`DEPRECATED` in `scripts/check_consistency.py`), remove it in a pull request of its own
   (`feat!:`) and merge that first.
4. **Automatic**: `tag-release.yml` tags `vX.Y.Z` and dispatches `release.yml`, which verifies the
   version, builds wheels (Linux x86_64/aarch64 glibc+musl via zig, macOS arm64/x86_64, Windows
   x64, Windows arm64 best effort) and the sdist, smoke-tests them, creates the GitHub release
   with attestations, and publishes to PyPI when the repository variable
   `PUBLISH_TO_PYPI` is `true` (trusted publishing, environment `pypi`).
5. **Retry** a failed release: Actions → Release → Run workflow → the existing tag, or
   `gh workflow run release.yml --ref vX.Y.Z -f tag=vX.Y.Z`.
6. **Local wheel check**: `make wheels`, then smoke-test foreign wheels with uv-managed
   interpreters (`uv venv -p cpython-3.12-linux-x86_64-musl ...`, `python -I
   scripts/smoke_test.py`).
