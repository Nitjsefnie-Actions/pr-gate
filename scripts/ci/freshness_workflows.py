"""The GitHub workflow reader behind scripts/ci/gate_base_freshness.py, standard library only.

ADMITTED SUBSET: one block `jobs:` mapping, job ids at two columns, job keys at
four, steps at six, step keys at eight. A job's `name:`, `uses:` and `needs:`
(a scalar, a flow list, a block list) are read; `env:` and `with:` values (the
workflow's, the job's, a step's) and a step's `run:` and `uses:` are its text.
A value is a block scalar (`|`/`>`, a trailing comment allowed), a quoted
scalar (escapes `\\"` and `\\\\` only), or a plain scalar folded with its deeper
continuation lines. Anything else refuses, anchors, aliases and tags first: a
guessed shape is a smaller set, and a smaller set is a green nobody earned.
"""

import re
from typing import NamedTuple


class GateError(Exception):
    pass


class WorkflowError(GateError):
    pass


class Job(NamedTuple):

    name: str | None
    needs: tuple[str, ...]
    uses: str | None
    parameters: str
    steps: list


MAPPING = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?:[ \t]+(?P<value>.*))?$")
SEQUENCE = re.compile(r"^- (?P<rest>.+)$")
BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*(?:[ \t]+#.*)?$")
JOB_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
ANCHORED = ("&", "*", "!")


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def skippable(line):
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def block_end(lines, start, indent):
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if not skippable(line) and indent_of(line) <= indent:
            break
        index += 1
    return index


def block_scalar(lines, start, key_indent, limit):
    # Every body line is kept, `#` lines too: they are bytes bash receives.
    body = []
    index = start + 1
    while index < limit:
        line = lines[index]
        if line.strip() and indent_of(line) <= key_indent:
            break
        body.append(line)
        index += 1
    leads = [indent_of(line) for line in body if line.strip()]
    lead = min(leads) if leads else key_indent + 2
    return "\n".join(line[lead:] if len(line) > lead else "" for line in body), index


def inline_value(lines, at, end, key_indent, first, workflow):
    # A key-shaped continuation is not part of a plain scalar; the caller refuses it.
    if first[:1] in ANCHORED:
        raise WorkflowError(f"{workflow}: an anchor, alias or tag is not modelled: {lines[at]!r}")
    parts = [first]
    index = at + 1
    while index < end:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if indent_of(line) <= key_indent or line.strip().startswith("#") \
                or MAPPING.match(line.strip()):
            break
        parts.append(line.strip())
        index += 1
    return " ".join(parts), index


def leftover(lines, after, end, workflow, what):
    rest = [line for line in lines[after:end] if not skippable(line)]
    if rest:
        raise WorkflowError(f"{workflow}: {what} continues as {rest[0]!r}")


def scalar(text, workflow, what):
    text = text.strip()
    quoted = (re.fullmatch(r'"((?:[^"\\]|\\["\\])*)"(?:[ \t]+#.*)?', text)
              or re.fullmatch(r"'((?:[^']|'')*)'(?:[ \t]+#.*)?", text))
    if quoted:
        body = quoted[1]
        return (body.replace("''", "'") if text[0] == "'"
                else re.sub(r'\\(["\\])', r"\1", body))
    if not text or text[0] in "\"'[{&*!|>%@`":
        raise WorkflowError(f"{workflow}: {what} is not a scalar this reader models: {text!r}")
    return re.split(r"[ \t]+#", text, maxsplit=1)[0].strip()


def dependency_names(lines, at, end, workflow, name, value):
    if value:
        text, after = inline_value(lines, at, end, 4, value, workflow)
        text = re.split(r"(?<=\])[ \t]+#", text, maxsplit=1)[0].strip()
        if text.startswith("[") and text.endswith("]") and "[" not in text[1:-1]:
            names = [item for item in text[1:-1].split(",") if item.strip()]
        else:
            names = [text]
        leftover(lines, after, end, workflow, f"`needs` of job `{name}`")
    else:
        names = []
        for line in lines[at + 1:end]:
            if skippable(line):
                continue
            entry = SEQUENCE.match(line.strip()) if indent_of(line) == 6 else None
            if entry is None:
                raise WorkflowError(
                    f"{workflow}: `needs` of job `{name}` is not a job list: {line!r}")
            names.append(entry["rest"])
    needs = tuple(scalar(item, workflow, f"`needs` of job `{name}`") for item in names)
    for need in needs:
        if not JOB_ID.match(need):
            raise WorkflowError(f"{workflow}: `needs` of job `{name}` names {need!r}, not a job id")
    if not needs:
        raise WorkflowError(f"{workflow}: `needs` of job `{name}` names no job")
    return needs


def parameter_text(lines, at, end, value, workflow):
    if value:
        return inline_value(lines, at, end, indent_of(lines[at]), value, workflow)[0]
    values = []
    for line in filter(lambda line: not skippable(line), lines[at + 1:end]):
        match = MAPPING.match(line.strip())
        values.append((match["value"] or "") if match else line.strip())
        if re.match(r"<<\s*:", line.strip()) or match and values[-1].startswith(ANCHORED):
            raise WorkflowError(f"{workflow}: an anchor, alias or tag is not modelled: {line!r}")
    return "\n".join(values)


def top_level_keys(lines, key):
    found = []
    for index, line in enumerate(lines):
        if skippable(line) or indent_of(line) != 0:
            continue
        match = MAPPING.match(line)
        if match is not None and match["key"] == key:
            found.append((index, match))
    return found


def workflow_jobs(text, workflow):
    lines = text.splitlines()
    # Two `jobs:`, or a flow `jobs:`, say nothing this can read: refused, never a smaller set.
    job_matches = top_level_keys(lines, "jobs")
    tops = [index for index, match in job_matches]
    if len(tops) != 1:
        raise WorkflowError(
            f"{workflow} has {len(tops)} top-level `jobs:` mappings; this "
            f"reader models exactly one, so which jobs it defines cannot be "
            f"established")
    if job_matches[0][1]["value"] is not None:
        raise WorkflowError(
            f"{workflow} writes `jobs:` as a flow mapping; this reader models "
            f"only the block form")
    inherited = "\n".join(
        parameter_text(lines, at, block_end(lines, at, 0), match["value"], workflow)
        for at, match in top_level_keys(lines, "env"))

    spans = {}
    index = tops[0] + 1
    while index < len(lines):
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) < 2:
            break
        if indent_of(line) != 2:
            raise WorkflowError(
                f"{workflow}: expected a job entry under `jobs:`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match or match["value"] is not None:
            raise WorkflowError(f"{workflow}: expected a job entry, found {line!r}")
        name = match["key"]
        if name in spans:
            raise WorkflowError(f"{workflow}: duplicate job {name!r}")
        spans[name] = (index + 1, block_end(lines, index, 2))
        index = spans[name][1]

    return {name: job_fields(lines, name, span, workflow, inherited)
            for name, span in spans.items()}


def job_fields(lines, name, span, workflow, inherited):
    start, end = span
    fields = {"name": None, "needs": (), "uses": None}
    parameters, steps = [inherited], []
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) != 4:
            raise WorkflowError(
                f"{workflow}: expected a step list or a job key in job "
                f"`{name}`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a job key in job `{name}`, found {line!r}")
        key, value = match["key"], (match["value"] or "").strip()
        stop = block_end(lines, index, 4)
        # A flow `steps:` read as a block would be an empty, plausible step list.
        if key == "steps" and match["value"] is not None:
            raise WorkflowError(
                f"{workflow}: `steps:` carries a value in job `{name}`; this "
                f"reader models only the block form")
        if key == "steps":
            step_entries(lines, name, index + 1, stop, workflow, steps)
        elif key == "needs":
            fields["needs"] = dependency_names(lines, index, stop, workflow, name, value)
        elif key in ("name", "uses"):
            text, after = inline_value(lines, index, stop, 4, value, workflow)
            leftover(lines, after, stop, workflow, f"`{key}` of job `{name}`")
            fields[key] = scalar(text, workflow, f"`{key}` of job `{name}`")
        elif key in ("env", "with"):
            parameters.append(parameter_text(lines, index, stop, value, workflow))
        # Other job keys name no file a step reads; their whole block is skipped.
        index = stop
    return Job(fields["name"], fields["needs"], fields["uses"], "\n".join(parameters), steps)


def step_entries(lines, name, start, end, workflow, steps):
    index = start
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        entry = SEQUENCE.match(line.strip()) if indent_of(line) == 6 else None
        if entry is None:
            raise WorkflowError(
                f"{workflow}: expected a step entry in job `{name}`, found {line!r}")
        stop = block_end(lines, index, 6)
        steps.append(step_fields(lines, name, index + 1, stop, workflow,
                                 entry["rest"]))
        index = stop
    return index


def apply_field(lines, end, key, value, at, step, workflow):
    if value and BLOCK_SCALAR.match(value) or not value and key == "run":
        text, after = block_scalar(lines, at, 8, end)
    elif value:
        text, after = inline_value(lines, at, end, 8, value, workflow)
    elif key in ("with", "env"):
        after = block_end(lines, at, 8)
        text = parameter_text(lines, at, after, "", workflow)
    else:
        return block_end(lines, at, 8)
    if key == "run":
        step["run"] = text
    elif key == "uses":
        # The reference alone: a `# v7.0.1` after it is a comment.
        step["uses"] = text.split()[0]
    elif key in ("with", "env"):
        step["with"] = "\n".join(filter(None, (step["with"], text)))
    return after


def step_fields(lines, name, start, end, workflow, dash):
    # `dash`, the text after the step's `- `, is its first key.
    step = {"run": None, "uses": None, "with": None}
    index = start
    if dash:
        match = MAPPING.match(dash)
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a key on the step entry in job `{name}`, "
                f"found {dash!r}")
        index = apply_field(lines, end, match["key"],
                            (match["value"] or "").strip(), start - 1, step, workflow)
    while index < end:
        line = lines[index]
        if skippable(line):
            index += 1
            continue
        if indent_of(line) != 8:
            raise WorkflowError(
                f"{workflow}: expected a key in a step of job `{name}`, found {line!r}")
        match = MAPPING.match(line.strip())
        if not match:
            raise WorkflowError(
                f"{workflow}: expected a key in a step of job `{name}`, found {line!r}")
        index = apply_field(lines, end, match["key"],
                            (match["value"] or "").strip(), index, step, workflow)
    return step
