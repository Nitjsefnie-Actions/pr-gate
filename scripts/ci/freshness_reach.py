"""The jobs a required check runs and the files they read, for scripts/ci/gate_base_freshness.py."""

import ast
import posixpath
import re

if __package__:
    # pylint: disable-next=relative-beyond-top-level
    from .freshness_workflows import GateError
else:
    from freshness_workflows import GateError

# Stripped, so an expression's identifiers are never read as paths.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
CANDIDATE = re.compile(r"[A-Za-z0-9._/-]+")
# A workspace path, or GitHub's `$/` self reference: this repository's own action or workflow.
LOCAL = ("./", "$/")
SCRIPTS = (".py", ".sh", ".bash")


def candidates(text):
    return CANDIDATE.findall(EXPRESSION.sub(" ", text))


def resolve(candidate, files):
    # The tree is the filter; `.` names nothing (see the script's docstring).
    candidate = candidate[2:] if candidate.startswith("./") else candidate
    candidate = candidate.rstrip("/")
    if not candidate:
        return ()
    if candidate in files:
        return (candidate,)
    prefix = candidate + "/"
    return tuple(f for f in files if f.startswith(prefix))


def publishes(context, job_id, job):
    # A check run is named by `name:`, else the id; a matrix leg appends `(axes)`.
    leg = context.split(" (")[0]
    if job_id in (context, leg) or job.name in (context, leg):
        return True
    if job.name is None or "${{" not in job.name:
        return False
    pattern = ".*".join(re.escape(part) for part in EXPRESSION.split(job.name))
    return re.fullmatch(pattern, context) is not None


def required_jobs(parsed, contexts, workflow_dir):
    # Every workflow defining the context, unioned: a job id is unique only within its workflow.
    found = []
    for context in contexts:
        matched = sorted((workflow, job_id) for workflow, jobs in parsed.items()
                         for job_id, job in jobs.items() if publishes(context, job_id, job))
        if not matched:
            raise GateError(
                f"no workflow under {workflow_dir}/ defines the required job "
                f"`{context}`, so the files the required checks read cannot be built: "
                f"put the job back, or update the ruleset if it was renamed")
        found.extend(matched)
    return found


def reached_jobs(parsed, starts):
    # A dangling `needs` or local call refuses: GitHub runs none of it, and its files would drop.
    reached, pending = set(), list(starts)
    while pending:
        workflow, job_id = pending.pop()
        if (workflow, job_id) in reached:
            continue
        if job_id not in parsed[workflow]:
            raise GateError(f"{workflow}: a job needs `{job_id}`, which it does not define")
        reached.add((workflow, job_id))
        job = parsed[workflow][job_id]
        pending.extend((workflow, need) for need in job.needs)
        if job.uses and job.uses.startswith(LOCAL):
            callee = job.uses[2:].split("@")[0]
            if callee not in parsed:
                raise GateError(
                    f"{workflow}: job `{job_id}` calls {job.uses}, which is not a "
                    f"tracked workflow this run read")
            pending.extend((callee, callee_job) for callee_job in parsed[callee])
    return reached


def module_index(files):
    # Under every suffix of its path, as the runner's `sys.path` is not text this reads.
    index = {}
    for path in files:
        if not path.endswith(".py"):
            continue
        parts = path[:-3].split("/")
        if parts[-1] == "__init__":
            parts.pop()
        for start in range(len(parts)):
            index.setdefault(".".join(parts[start:]), set()).add(path)
    return index


def trailing_strings(operands):
    tail = []
    for operand in reversed(operands):
        if not (isinstance(operand, ast.Constant) and isinstance(operand.value, str)):
            break
        tail.insert(0, operand.value)
    return "/".join(tail)


def references(tree):
    modules, literals = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            literals.append(trailing_strings(division_chain(node)))
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            called = getattr(node.func, "attr", getattr(node.func, "id", None))
            if called in ("import_module", "__import__"):
                modules.append(trailing_strings(node.args[:1]))
            elif called in ("join", "joinpath"):
                literals.append(trailing_strings(node.args))
        elif isinstance(node, ast.ImportFrom):
            # The suffix index answers a relative `.sib` as `sib`.
            prefix = node.module or ""
            modules.append(prefix)
            modules.extend(f"{prefix}.{alias.name}".lstrip(".") for alias in node.names)
    return modules, literals


def division_chain(node):
    operands = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        operands.insert(0, node.right)
        node = node.left
    return [node, *operands]


def named_by(path, text, files, index):
    here, found = posixpath.dirname(path), set()
    if path.endswith(".py"):
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as refusal:
            raise GateError(
                f"cannot read the imports of {path}, which a required check runs: "
                f"{refusal}") from refusal
        modules, literals = references(tree)
        for module in modules:
            found.update(index.get(module, ()))
        # A literal names an exact file only; a directory literal expands nothing.
        found.update(name for literal in literals if literal for base in ("", here)
                     if (name := posixpath.normpath(posixpath.join(base, literal))) in files)
    else:
        for candidate in candidates(text):
            for base in ("", here):
                found.update(resolve(posixpath.join(base, candidate.lstrip("/")), files))
    return found


def script_reach(paths, files, read):
    tracked, index = set(files), module_index(files)
    reached = set(paths)
    pending = sorted(path for path in reached if path.endswith(SCRIPTS))
    while pending:
        path = pending.pop()
        for new in sorted(named_by(path, read(path), tracked, index) - reached):
            reached.add(new)
            if new.endswith(SCRIPTS):
                pending.append(new)
    return reached
