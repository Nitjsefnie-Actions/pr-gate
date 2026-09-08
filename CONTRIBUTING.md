# Contributing to pr-gate

This repository ships a GitHub composite action, not a wheel or application.
Changes affect a privileged admission workflow, so tests must exercise real
parser/controller behavior and place fakes only at GitHub's external API boundary.

## Local setup

Use Python 3.11, 3.12, 3.13, or 3.14. Bash and GitHub CLI are required for
action-entrypoint tests; tests replace GitHub with a local executable fixture,
so they need no token and make no live PR/comment/state changes.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
```

On Windows, create the environment with `python -m venv .venv` and activate
`.venv\Scripts\Activate.ps1`. The action test uses Git Bash. PyYAML and Ruff
are pinned development tools only; runtime modules must stay standard-library-only.

## Required checks

Run these from the repository root with the virtual environment active:

```bash
python run_tests.py
python -m ruff check --select E9,F63,F7,F82 .
actionlint -color .github/workflows/*.yml
zizmor --no-progress action.yml .github/workflows/
git diff --check
```

`run_tests.py` discovers every `tests/test_*.py` module and refuses empty
discovery/execution. Individual suites also run directly, for example
`python tests/test_action.py` or `python tests/test_pr_rendered_content.py`.
CI runs the same tests and Python lint on Linux, Windows, and macOS across all
four supported Python versions. The separate workflow audit pins actionlint
1.7.12 and zizmor 1.29.0; its installation steps checksum-verify actionlint.
Zizmor can run offline locally; disclose that limitation when reporting results.
No separate static type checker is currently configured.

Use strict test-driven development: add a behavioral regression, run it RED for
the intended reason, implement the smallest fix, and run it GREEN. For gate
changes, plant a representative defect in the actual runtime/action file,
prove the control catches it, restore the correct code, and rerun the suite.
Use captured GitHub HTML for disputed rendering behavior; do not approximate
Markdown with another source parser. Enumerate constructions and consumers
before changing structured parser outputs.

## Security boundaries

Consumer workflows use `pull_request_target` only for opened, edited, and
reopened events, skip Bot authors, grant only the permissions documented in
README, and never cancel an in-progress run for the same PR. Their action
reference must be a reviewed full SHA.

The action executes only code under its immutable `github.action_path`.
Never add a PR-head checkout, execute consumer scripts, interpolate PR text
into shell source, or read policy from a moving branch/head revision. Consumer
templates are fetched at the PR snapshot's base SHA. Keep state/closer
revalidation, the bounded issue lookup cache, and retryable close ownership.

Use pinned action references and explicit least-privilege permissions in this
repository's own CI. Add new code paths to the deny-by-default `.gitignore` and
prove they are visible with `git check-ignore -q` (exit 1) and `git status`.
Seed representative junk when reopening directories and prove it stays ignored.

## Issues, pull requests, and commits

Use the shipped [bug report](.github/ISSUE_TEMPLATE/bug-report.md) and
[PR template](.github/PULL_REQUEST_TEMPLATE.md). Keep required sections, remove
inapplicable optional/conditional sections and instructions, report exact
commands/results, and put closing references only in Related Issues and Pull
Requests. Bugs Discovered is a pointer list to already-filed issues with exact
titles, not a narrative findings section. The Footer is the final attribution line.

Every agent-authored commit carries the actual model's plain-name trailer:

```text
Co-Authored-By: GPT-6 Astra <noreply@openai.com>
```

Substitute the model that actually authored the work; omit effort or context
suffixes. Stage explicit paths, keep commits focused, and verify before pushing.
Published action revisions are immutable commit SHAs; do not create mutable
release tags as a substitute for reviewable pins.

Follow the [Code of Conduct](CODE_OF_CONDUCT.md). Enforcement reports go privately
to Nitjsefnie at [zmatek.peter@gmail.com](mailto:zmatek.peter@gmail.com).
