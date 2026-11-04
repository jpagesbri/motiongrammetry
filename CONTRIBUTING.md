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

### The browser tests

`tests/browser/` drives the pages in a real browser (headless Chromium, through Playwright and
`pytest-playwright`, from the `dev` extra): the top bar, the scan chooser, the tabs and modes, scrolling,
the Export window and the editor's tools. They start `mg ui` on a free port of their own (never 8765 or
8766) over two small scans built in a temp folder (`tests/browser/scan_fixture.py`), so they never touch
your scans. They take about a minute. Until the browser is installed they are skipped, with the reason.

```bash
.venv/bin/playwright install chromium-headless-shell     # once (~100 MB, in ~/.cache/ms-playwright)
# Linux, if the browser is missing system libraries:  sudo .venv/bin/playwright install-deps chromium-headless-shell
.venv/bin/python -m pytest -m browser -q                  # only the browser tests
.venv/bin/python -m pytest -m "not browser" -q            # everything else
.venv/bin/python -m pytest tests/browser/test_editor.py -k decimate --headed --slowmo 300   # watch one
```

**When one fails**, run it again with a trace, then open the trace: every step, with the page as it
was (DOM, screenshot), the console and the network.

```bash
.venv/bin/python -m pytest -m browser --tracing retain-on-failure --screenshot only-on-failure
.venv/bin/playwright show-trace test-results/<the test>/trace.zip
```

On GitHub, a failed run uploads the same files as the artifact **browser-test-results** (on the run's
summary page).

**Adding one:** put a `test_*.py` in `tests/browser/` that starts like the others, with
`pytest.importorskip("playwright", …)`, and take the `page` fixture. Each test gets fresh copies of the
scans `box` (keyframes, cameras, a clean mesh and a textured model) and `blank` (a new, empty scan), and
a fresh browser (empty browser storage). Then:
- open a page with `ui.open_page(page, "scan" | "edit" | "help" | "about")`, which waits until it has
  loaded the scan; `ui.py` also has a stroke on the editor's view (`drag_on_view`)
- check what the page shows with `expect(...)`, which waits for it: never a fixed wait
  (`wait_for_timeout`, `sleep`)
- the test fails if the page throws or logs a console error. If an error is expected, mark the test
  `@pytest.mark.console_error("<regex>")` and say why.

A known bug is marked `xfail(strict=True)` with the reason, so the run stays green and the mark has to
go once it is fixed (see `NARROW_SCAN` in `test_scrolling.py`).

GitHub runs the same tests on every pull request into `main` (the **tests / pytest** check, from
`.github/workflows/tests.yml`, about 6 minutes, browser tests included: they must run, not skip),
on the versions in `requirements-lock.txt`.
That file is the project's only lock: there is no `uv.lock` (it is ignored, since `uv run` and
`uv sync` would write one that nothing tests). When a dependency changes in `pyproject.toml`,
update `requirements-lock.txt` in the same pull request.
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
