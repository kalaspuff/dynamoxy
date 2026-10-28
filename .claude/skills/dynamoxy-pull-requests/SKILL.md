---
name: dynamoxy-pull-requests
description: Commit, push and open pull requests for dynamoxy - conventional titles, descriptions that become the squash commit, stacked pull requests (splitting work, verifying every stage, updating the stack after squash merges without force-pushing), fixing failing checks and review comments, and publish kits for agents that cannot push to GitHub. Use when committing, pushing, opening, updating or merging pull requests, splitting a change into a stack, handling a failing CI check, or handing work over from a machine without GitHub credentials.
---

# Pull requests and stacks

Every change reaches `main` as a squash-merged pull request. An agent that can push to GitHub
(`git push` works and `gh auth status` is logged in) does all of it itself, in the checkout it works
in: it branches, commits, pushes, opens the pull request with `gh` and follows its checks and
reviews. That takes no publish kit, no scratch clone and no separate go-ahead to push the branch or
open the pull request. Only an agent that cannot push hands its work over as a publish kit
(section 6). The commands below run from the top of the repository, with
`SKILL=.claude/skills/dynamoxy-pull-requests/scripts`.

## 1. How a pull request lands

- **Squash merge** is the only merge method the repository allows. The commit on main is
  `<PR title> (#N)`, with the PR description as its body and the `Co-authored-by` trailers of the
  branch's commits; the branch's own commit messages are dropped. Building that commit, GitHub
  wraps description lines longer than 72 characters (not code blocks). The merged branch is
  deleted, and GitHub points the pull requests that were based on it at main.
- **Changelog.** git-cliff (`cliff.toml`) builds the release notes from the subjects on main: the
  type picks the group, the scope becomes a bold prefix, and `!` or `BREAKING CHANGE:` marks a
  breaking change. The PR title is the changelog line, so write it for users of dynamoxy. What a
  title cannot say goes into `CHANGELOG.md` under `[Unreleased]`, in the same PR.
- **Title check.** `.github/workflows/pr-title.yml` accepts `feat`, `fix`, `perf`, `refactor`,
  `docs`, `test`, `ci`, `build`, `chore` and `revert`, with an optional scope and `!`. Editing the
  title runs the check again.
- **CI** (`ci.yml`) runs on every pull request, also on one based on another branch: lint, types
  and docs consistency; Rust tests and the MiniStack parity scenarios; Python 3.12-3.15 on Linux
  x86_64 and arm64 with the e2e tests; macOS and Windows on 3.12 and 3.15 without them; packaging.
  CodeQL runs too.
- **Merging is the maintainer's call.** Do not merge pull requests, push to main or start a
  release unless you are asked to.

## 2. One pull request

1. **Branch** from the current main: `git fetch origin && git switch -c <type>/<topic> origin/main`,
   for example `docs/credentials-and-region`, or use the branch your harness created for the task.
2. **Commit** as you go, with a conventional subject and a body, wrapped at 72 columns, that says
   why. End it with the attribution trailers your harness gives you.
3. **Check** with `make check` before you open the pull request; CI runs the same on more
   Pythons and platforms (section 1).
4. **Push and open it**: `git push -u origin HEAD`, then `gh pr create --base main --title
   "<title>" --body-file <description>`. The title is the subject the squash commit will have.
5. **Describe it for main's history**, since the description becomes the squash commit's body:
   what changes and why, a `## Breaking change` section with the migration when there is one, and
   how it was verified. Write each paragraph and list item on one line: GitHub shows it as written
   and wraps it at 72 columns in the squash commit, whereas text hard-wrapped wider than 72
   columns comes out ragged there. Leave out what does not belong in main's history: instructions
   for whoever merges, local paths, the order of a stack (section 3). End it with the attribution
   lines your harness asks for. When later commits change what the pull request does, rewrite the
   description (`gh pr edit <n> --body-file <file>`) and, if needed, the title.
6. **Follow it**: `gh pr checks <n> --watch` waits for CI. Fix a failed check (section 5) and
   answer reviews (section 4) with new commits.

## 3. Stacked pull requests

Split a change into a stack when its steps can be reviewed on their own: fixes the feature needs,
refactors, the feature, and a breaking change last. Fixes go first, so that they can be merged
while the feature is still in review.

- Each pull request passes CI on its own and has its own tests, docs and changelog lines.
- Pull request N is based on branch N-1, the first one on main. Branches are named
  `<topic>-stack/<n>-<name>`, for example `instances-stack/2-copies`.
- They are merged strictly in order, each once its base is main: a pull request based on another
  branch would merge into that branch. After each merge, the next branch is updated (section 4)
  before it is merged.
- The order of the stack goes in a comment on each pull request, not into the descriptions, which
  become commit messages.

**Build the stages as branches** in your checkout, each on the one before it:

```bash
git fetch origin
git switch -c <topic>-stack/1-<name> origin/main   # commit stage 1
git switch -c <topic>-stack/2-<name>               # on top of stage 1: commit stage 2, and so on
```

When the work already exists as one change, commit it on a branch of its own first, then build
the stages from it: on each stage's branch, `git restore --source <work> -- <files>` takes that
stage's files (edit a file to its intermediate state when a stage has only part of it), and you
commit them. The last stage must be exactly the work: `git diff <work> <last stage>` shows
nothing.

**Test the stages against each other** before you push them:

- Every stage passes the CI checks on its own:
  `$SKILL/verify_stages.sh . <topic>-stack/1-<name> <topic>-stack/2-<name> ...` checks each branch
  out in a clone of its own (your checkout stays as it is) and runs lint, types, the consistency
  checks and the tests on Python 3.12 and 3.15 with the e2e tests, and the Rust checks when the
  stack changes Rust.
- Each pull request contains only its own change: read `git diff <branch N-1> <branch N>` for every
  stage.

**Open them** in order: push each branch with `git push -u origin <branch N>` and open its pull
request with `gh pr create --base <branch N-1> --head <branch N> ...` (the first one with
`--base main`). The `-u` makes each branch track its own remote branch, which the plain `git push`
and `git pull --ff-only` of section 4 rely on: otherwise the first branch tracks `origin/main`,
where `git switch -c` started it, and the others track nothing. Then comment the order on each
pull request (`gh pr comment <n> --body-file <file>`):

```markdown
**Stack** of 3 pull requests, merged in this order with "Squash and merge":

1. #61 fix(query): ...
2. #62 feat(query): ... ← this pull request
3. #63 docs: ...
```

On GitHub, the CI of each pull request tests its branch merged into the branch before it.

## 4. After publishing: reviews, merges, a moving main

Published branches only move forward: never force-push them, and never amend or rebase their
commits. Every change is a new commit, merges included, so that reviews keep their context; the
squash merge leaves none of these commits on main.

- **A review comment or a failed check** becomes a new commit on the branch that needs it. Then
  merge each branch into the next one, in order, and push them: `git switch <branch N+1> && git
  merge --no-edit <branch N> && git push`.
- **After a squash merge** of the bottom pull request, update the next branch with three merges.
  A plain merge of main would conflict wherever the next stage changes lines the merged one wrote;
  these give the tree a rebase onto main would give:

  ```bash
  git fetch origin
  squash=$(gh pr view <merged n> --json mergeCommit --jq .mergeCommit.oid)
  head=$(gh pr view <merged n> --json headRefOid --jq .headRefOid)
  git switch <next branch> && git pull --ff-only
  git merge-base --is-ancestor "$head" HEAD   # holds the merged branch's last commit? (see below)
  git merge --no-edit "$squash^"              # main up to the squash commit
  git merge --no-edit -s ours "$squash"       # the squash commit, whose changes are here already
  git merge --no-edit origin/main             # what main got after it
  git diff origin/main...HEAD --stat          # only this pull request's change
  git push
  ```

  If the ancestor check fails, the merged branch got commits that never reached this one: merge
  them first (`git fetch origin pull/<merged n>/head && git merge --no-edit FETCH_HEAD`). GitHub
  has already pointed the pull request at main (otherwise: `gh pr edit <n> --base main`). Then
  merge each later branch's updated predecessor into it, as for a review commit.
- **When main moves** under an open stack (other merges, a conflict in `CHANGELOG.md`), merge
  `origin/main` into the bottom branch, resolve the conflicts, and merge each branch into the next.

## 5. Fixing a failing check

1. **Find what failed**: `gh pr checks <n>`, then `gh run view <run id> --log-failed` (or
   `gh run view --job <job id> --log-failed`).
2. **Reproduce it on the exact commit** with the failing job's command and Python. Give another
   Python an environment of its own, since `uv run -p 3.12` would replace `.venv`:
   `UV_PROJECT_ENVIRONMENT=~/.cache/dynamoxy-venvs/3.12 uv run -p 3.12 pytest ...` (uv downloads
   the interpreter if needed). For a platform you cannot run (Windows, macOS), work out what differs
   there: clock resolution, path separators, line endings, the event loop, case-insensitive or
   locked files. Emulate it so that the test fails first and passes after the fix. Example: PR #10
   failed only on Windows with Python 3.12, whose clock advances in steps of about 15.6 ms. Two
   writes got the same timestamp, which the test had assumed impossible; a pytest plugin with a
   frozen clock reproduced it.
3. **Fix it on the earliest branch that has the cause** (`git log -S`, `git blame`), as a commit of
   its own (`test: ...`, `fix(<scope>): ...`).
4. **Verify that stage and every stage after it** again, push the fix and merge it forward through
   the later branches (section 4).

## 6. Without push access: publish kits

When you cannot push to GitHub, or the workspace is the user's own checkout and you must not
commit there, hand the work over as a **publish kit**: a directory `.stacks/<topic>/` in the
workspace that git ignores, with each pull request's patch, commit message and description, the
tested trees, and `publish.sh`, which the user runs to push the branches, open the pull requests,
restack after merges and apply fixes. [kits.md](kits.md) shows how to build, test and hand over a
kit, and the rules for the scripts in `scripts/` (`kit.py`, `publish.sh`, `rehearse.sh`,
`gh_stub.py`, `selftest.sh`). After changing them, run `$SKILL/selftest.sh`, the same with
`--bash <bash 3.2>` (macOS ships bash 3.2; kits.md shows how to build one) and
`shellcheck $SKILL/*.sh`.
