#!/usr/bin/env python3
"""Require the head being checked out to carry every commit main holds that
touches a file the REQUIRED status checks read as a parameter. A `push` run
passes a head main already holds: main moved past it, and no rebase answers.

The merge ruleset sets strict_required_status_checks_policy to false, so a
pull request merges on a green head whose checks ran against a main that had
not yet moved. Without this check, a commit that lands on main changing a
file a required check reads is never applied to the pull request before it
merges, and the merge publishes a tree whose green run proved nothing about
it.

The set of such files is DERIVED from the workflows under .github/workflows,
not kept in a list here. A remembered list is only as current as the last time
somebody remembered to add to it, which is this defect wearing different
clothes: a gate whose own inputs are enumerated by hand goes stale exactly the
way this check exists to prevent.

The required jobs come from main's ruleset, which the calling step reads with
gh into PR_GATE_REQUIRED_CHECKS, since repository settings are not in the
repository. Every name there must still be FOUND in a workflow below, as a
job id or a display name, or the run refuses: a job renamed out of every
workflow would otherwise shrink the set in silence, and a narrower set reports
green over a wider gate. A context is a check run's name: a job's `name:`
(an expression in it matching any text), else its id, a matrix leg's
`(axes)` dropped.

WHAT IT FOLLOWS (scripts/ci/freshness_reach.py). From each required job, every
job GitHub runs for it: the jobs it `needs`, transitively, since an aggregate
that folds others reads what they read; and every job of a local reusable
workflow it calls (`./` or the `$/` self reference). Each reached job's
workflow is in the set, as it defines the check. Each tracked script those
jobs run is followed to a fixpoint: a shell script's text is read like a
step's, and a Python module brings the tracked modules it imports (indexed
under every suffix of their paths, since the runner's `sys.path` is not text)
and the tracked files its string literals, `/`-chains and `join` calls name
exactly, from the root or its own directory. A step's token names a tracked
file or every file under a tracked directory; a glob such as
`.github/workflows/*.yml` breaks at the `*`, and the directory before it is
taken whole, which is what the glob meant. Over-broad on purpose: a wider
set is a rebase nobody strictly owed, a narrower one a green nobody earned.

WHAT IT CANNOT SEE. The derivation reads text, so a file a required check
reads with no `run:` block or `with:` value naming it is outside it. Named
here rather than left for a reader to infer, because each of these makes the
gate NARROWER and a narrower gate looks the same as a correct one:

  - A tool reading a configuration file nothing names. actionlint reads
    `.github/actionlint.yaml`; zizmor reads `zizmor.yml` at the root of the
    path it is given and under `.github/`; pytest reads its ini-file.
  - The whole-tree spelling. `git grep ... -- .` in the `actionlint` job's
    merge-marker step reads EVERY tracked file, and `.` resolves to nothing
    here, so that step contributes no paths at all. Giving `resolve()` a
    whole-tree arm would be correct and would make this "rebase before you
    merge" on any change whatsoever — which is
    `strict_required_status_checks_policy: true` reached through the back
    door, against a ruleset the maintainer set to false on purpose. That is
    the maintainer's decision to make, so the limit is named and the answer
    stays visible rather than taken here.
  - What a gate script reaches past its imports and the files it names: a
    module imported by a computed name, a path built from a variable, a
    directory a Python literal names, anything a non-Python, non-shell
    script reads, and a remote reusable workflow's own files.

Runs on the standard library alone, so a gate script in syntax newer than the
runner's `python3` refuses. The workflows are read with a parser for the
block layout GitHub workflows use (scripts/ci/freshness_workflows.py writes
down its subset) rather than a YAML dependency: nothing installs one for it,
and a parser that refuses a shape it does not model is better than a silent
misreading.

    scripts/ci/gate_base_freshness.py [--root DIR] [--print-paths]

--print-paths writes the derived set, one path per line, and is how the suite
pins the derivation.
"""

import os
import subprocess
import sys
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level
    from .freshness_reach import (
        LOCAL, candidates, reached_jobs, required_jobs, resolve, script_reach)
    # pylint: disable-next=relative-beyond-top-level
    from .freshness_workflows import GateError, WorkflowError, workflow_jobs
else:
    # Loaded by path, by the action and the suite alike: its modules sit beside it.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from freshness_reach import (
        LOCAL, candidates, reached_jobs, required_jobs, resolve, script_reach)
    from freshness_workflows import GateError, WorkflowError, workflow_jobs

__all__ = ("GateError", "WorkflowError", "gate_paths", "check", "main")

# main's ruleset's required contexts, one per line, as the check runs publishing them are named.
REQUIRED_CHECKS = tuple(dict.fromkeys(
    context.strip() for context in
    os.environ.get("PR_GATE_REQUIRED_CHECKS", "").splitlines() if context.strip()))

# The default branch a required check's head is compared against, which the calling step names.
BASE_BRANCH = os.environ.get("PR_GATE_DEFAULT_BRANCH") or "main"

# The byte size at which a pathspec argument list is refused rather than
# attempted. The kernel's own limit is `getconf ARG_MAX`, which is not a fixed
# number and not readable from Python, so this is a deliberately low ceiling:
# every repository that reaches it is far past this one's size, and a refusal
# names the cause where an E2BIG from exec would be a traceback.
PATHSPECS_MAX_BYTES = 65536

WORKFLOW_DIR = ".github/workflows"


def git(root, *arguments, what):
    """Run one git command in `root` and return its stdout.

    Every call goes through here because a guard that reads its own error as a
    clean tree is the false green this exists to prevent: a non-zero status is
    a refusal naming what was being attempted, never an empty answer.
    """
    command = ("git", "-C", str(root)) + arguments
    # UTF-8 whatever the runner's locale: the scripts read through here are UTF-8 text.
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if done.returncode != 0:
        detail = done.stderr.strip() or "no output"
        raise GateError(
            f"cannot {what}: `{' '.join(command)}` exited {done.returncode}: {detail}")
    return done.stdout


# --- the paths a required check reads --------------------------------------

def tracked_files(root):
    return tuple(f for f in
                 git(root, "ls-tree", "-r", "--name-only", "--full-tree", "HEAD",
                     what="list the tracked tree").split("\n") if f)


def workflow_names(files):
    return sorted(f for f in files
                  if f.startswith(WORKFLOW_DIR + "/")
                  and (f.endswith(".yml") or f.endswith(".yaml")))


def gate_paths(root):
    """Every tracked file a required check reads, derived from the workflows.

    Every workflow is parsed, not only the ones a required job turns out to be
    in: a workflow this cannot read is a workflow whose steps it cannot account
    for, and skipping it would be the same quiet narrowing the refusal exists
    to stop.
    """
    files = tracked_files(root)
    if not files:
        raise GateError("HEAD tracks no files, so the tree to compare is not there")
    parsed = {}
    for workflow in workflow_names(files):
        parsed[workflow] = workflow_jobs(
            git(root, "cat-file", "blob", f"HEAD:{workflow}",
                what=f"read {workflow}"), workflow)
    if not REQUIRED_CHECKS:
        raise GateError("PR_GATE_REQUIRED_CHECKS names no required check of main's ruleset")
    reached = reached_jobs(parsed, required_jobs(parsed, REQUIRED_CHECKS, WORKFLOW_DIR))
    derived = set()
    for workflow, job_id in sorted(reached):
        job = parsed[workflow][job_id]
        texts = [job.parameters]
        for step in job.steps:
            uses = step["uses"] or ""
            if uses.startswith(LOCAL):
                # A step running a composite action out of THIS repository
                # reads that action's files as its own parameters, and they
                # are not text in this workflow. The directory is taken whole
                # rather than its `action.yml` alone: what the action reaches
                # from inside is the same question this matcher cannot
                # answer, and a partial answer would be a narrower gate than
                # it looks.
                derived.update(files if uses in LOCAL else resolve(uses[2:], files))
            texts += [step["run"] or "", step["with"] or ""]
        for candidate in candidates("\n".join(texts)):
            derived.update(resolve(candidate, files))
    if not derived:
        raise GateError(
            "the required checks name no file in this tree, so there is nothing "
            "to compare and this run can vouch for nothing")
    # A reached job's workflow defines it: a change there is a change to the check.
    derived.update(workflow for workflow, _ in reached)
    return sorted(script_reach(derived, files, lambda path: git(
        root, "cat-file", "blob", f"HEAD:{path}", what=f"read {path}")))


# --- the comparison --------------------------------------------------------


def fetch_base(root):
    """Bring main in as it is NOW, not as the checkout left it.

    actions/checkout fetches one commit of one ref by default, so a main that
    moved after that run left nothing here to compare against — which is the
    whole question. Anonymous on purpose: every checkout in this repository
    sets persist-credentials: false and the repository is public.

    --unshallow deepens the ref actually being compared. Without it the grafted
    boundary hides the head's own ancestry, git cannot tell which of main's
    commits the head already carries, and the check reports main's history
    against it — naming commits whose content is sitting in the checkout. That
    is a red nobody can satisfy by rebasing.
    """
    arguments = ["fetch", "--no-tags", "--quiet"]
    if git(root, "rev-parse", "--is-shallow-repository",
           what="ask whether the checkout is shallow").strip() == "true":
        arguments.append("--unshallow")
    arguments += ["origin", f"+refs/heads/{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}"]
    git(root, *arguments, what=f"fetch origin/{BASE_BRANCH}")


def stale_commits(root, head, base, paths):
    """[(sha, subject, [paths])] for the commits base holds that head lacks.

    `--` with nothing after it means EVERY path, so the empty set is refused
    here rather than handed to git: a guard that compares against no paths
    would report the whole of main as fresh.
    """
    if not paths:
        raise GateError("the derived path set is empty, so there is nothing to compare")
    sized = sum(len(path) + 1 for path in paths)
    if sized > PATHSPECS_MAX_BYTES:
        raise GateError(
            f"the derived path set is {sized} bytes, past the {PATHSPECS_MAX_BYTES} "
            f"this run will put in an argument list")
    listing = subprocess.run(
        ("git", "-C", str(root), "log", f"{head}..{base}", "--name-only",
         "--format=%x00%H%x09%s", "--", *paths),
        capture_output=True, text=True)
    if listing.returncode != 0:
        raise GateError(
            f"cannot list what {BASE_BRANCH} holds that this head lacks: "
            f"`git log {head}..{base}` exited {listing.returncode}: "
            f"{listing.stderr.strip() or 'no output'}")
    stale = []
    for chunk in listing.stdout.split("\0"):
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        sha, _, subject = header.partition("\t")
        stale.append((sha.strip(), subject.strip(),
                      [line for line in body.split("\n") if line.strip()]))
    return stale


def check(root, event):
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}",
               what="resolve the checked-out head").strip()
    fetch_base(root)
    base = git(root, "rev-parse", "--verify", f"refs/remotes/origin/{BASE_BRANCH}^{{commit}}",
               what=f"resolve origin/{BASE_BRANCH}").strip()
    paths = gate_paths(root)
    stale = stale_commits(root, head, base, paths)
    # The push exemption the module docstring names; every other event is compared.
    if stale and event == "push" and git(
            root, "merge-base", head, base, what="find the merge base").strip() == head:
        print(f"This pushed head is already on {BASE_BRANCH}, which has moved past it.")
        return 0
    if not stale:
        # "BY NAME": the `.` spelling's whole-tree read is a named reach limit
        # (module docstring), and a green line claiming the whole set over-claims.
        print(f"This head carries every commit on {BASE_BRANCH} that touches "
              f"the {len(paths)} file(s) a required check reads BY NAME.")
        print("  A step reading every tracked file — the `.` spelling, which the "
              "actionlint\n  job's merge-marker step uses — is a named reach "
              "limit and is not\n  counted above; see the module docstring.")
        return 0
    plural = "s" if len(stale) != 1 else ""
    print(f"{BASE_BRANCH} holds {len(stale)} commit{plural} this head does not:")
    for sha, subject, touched in stale:
        print(f"  {sha} {subject}")
        if not touched:
            # `git log --name-only` reports no file for a merge commit, and the
            # commits it brought in are in the list as themselves. Saying so is
            # the whole point: a header claiming each entry names a file, over
            # an entry that names none, is a sentence the reader cannot check.
            print("    (a merge commit, which git names no file for; the "
                  "commits it brought in are listed as themselves)")
            continue
        for path in touched:
            print(f"    {path}")
    print(f"Rebase onto {BASE_BRANCH} and push again, so this run's checks read "
          f"the files {BASE_BRANCH} reads.")
    return 1


def main(argv):
    # Three parents, not two: this script sits at scripts/ci/, so two parents
    # land on scripts/, and stale_commits() compares with pathspecs —
    # `git log <head>..<base> ... -- <paths>` — which git resolves against the
    # directory `git -C` chdirs into. A root at scripts/ matches none of the
    # full-tree paths in the derived set, empties the commit listing, and
    # prints the green line over a stale head. commit_scopes.py keeps its own
    # two-parent default because nothing it runs resolves against the working
    # directory: its listing carries no pathspecs and its tree read is
    # `ls-tree --full-tree`.
    root = Path(__file__).resolve().parent.parent.parent
    print_paths = False
    rest = list(argv[1:])
    while rest:
        argument = rest.pop(0)
        if argument == "--print-paths":
            print_paths = True
        elif argument == "--root" and rest:
            root = Path(rest.pop(0))
        else:
            print("usage: gate_base_freshness.py [--root DIR] [--print-paths]",
                  file=sys.stderr)
            return 2
    try:
        if print_paths:
            for path in gate_paths(root):
                print(path)
            return 0
        return check(root, os.environ.get("GITHUB_EVENT_NAME", ""))
    except GateError as refusal:
        print(f"head freshness: {refusal}", file=sys.stderr)
        return 1
    except OSError as failure:
        # git could not be run at all — absent from the runner image, or an
        # argument list past what the kernel will exec. Named rather than
        # traced: a refusal is what this run owes the reader, and the exit
        # status is the same either way.
        print(f"head freshness: cannot run git: {failure}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
