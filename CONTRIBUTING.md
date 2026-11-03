# Contributing

How every change (feature, fix, docs, anything) gets from an idea to `main`, whether a person
or an agent such as Claude does the work. The owner approves every merge; this process tells
them what to check.

## 1. A branch per change

Start from an up-to-date `main` and give the branch a prefix for the kind of change:

| Prefix | For |
|---|---|
| `feat/` | a new feature |
| `fix/` | a bug fix |
| `perf/` | faster or lighter, same behaviour |
| `refactor/` | restructuring, same behaviour |
| `docs/` | documentation only |
| `test/` | tests only |
| `chore/` | tooling, build, process |
| `ci/` | continuous integration |

```bash
git switch main && git pull --ff-only
git switch -c fix/upload-replaces-video     # short, descriptive; mention the issue in the PR
```

One branch per issue (or per small group of related issues). Never commit to `main` directly.

## 2. Try it on a local instance, which shows the branch

Run the branch outside Docker, on **copies** of scans, never the real ones, on a port that
doesn't clash with the container (8765):

```bash
.venv/bin/mg ui --root /path/to/copies --port 8766
```

Run from a checkout, the version names its branch: the About page and `mg --version` show
`0.2.0-fix-upload-replaces-video` (slashes become dashes). It's the plain version only on a
release's own commit, and in the Docker image, which has no checkout. So you can always tell
which code a running instance is.

Run the tests too: `.venv/bin/python -m pytest tests/ -q`. They stop at once if they would import
another installed copy of `motiongrammetry` instead of this checkout.

GitHub runs the same tests on every pull request into `main` (the **tests / pytest** check, from
`.github/workflows/tests.yml`, about 5 minutes), on the versions in `requirements-lock.txt`.
Branch protection on `main` requires it to pass before a merge.

## 3. Before merging: say what the approver has to validate

Open a pull request. The template asks for this. It has to list **what the approver needs to
check by hand**, as concrete steps with the expected result. For example:

> - [ ] On a copy of a scan with a video, choose **Upload video…** with no name typed: a warning
>   shows both thumbnails and asks before replacing.

It also has to give:
- **what changed and why,** with the issue it closes (`Closes #25`)
- **how to run it:** the branch name and the version the About page should show
- **what was already checked:** the tests, and any end-to-end runs with their results
- **what was not checked,** and any known gaps

Whoever does the work writes this, an agent included, so the approver knows exactly what to
do. Add a line under **Unreleased** in `CHANGELOG.md` for anything a user would notice.

## 4. Merge: rebase and merge

The approver's "approved" is the go-ahead; nothing merges without it. Merges use GitHub's
**Rebase and merge**: the branch's commits are replayed on top of `main` one by one, with
the same messages, authors and changes, so history stays linear without squashing anything.
No merge commits, no squashes.

```bash
gh pr update-branch 25 --rebase    # only if main moved since the review: then rerun the tests
                                   # and say so, since the code changed after it was checked
gh pr merge 25 --rebase --delete-branch     # or the "Rebase and merge" button
git switch main && git pull --ff-only
git branch -D fix/upload-replaces-video     # -D: GitHub replayed the commits, so new hashes
```

`gh pr update-branch --rebase` rebases the branch on GitHub, so no local force push is needed.

## 5. Releases

See the top of [CHANGELOG.md](CHANGELOG.md): set the version in `pyproject.toml`, date the
Unreleased notes, commit, tag `vX.Y.Z`, push, then `./install.sh update` and check the About page.
