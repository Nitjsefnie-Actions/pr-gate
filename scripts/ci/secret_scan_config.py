#!/usr/bin/env python3
"""Prepare the secret scan's gitleaks config, refusing any change it cannot prove.

    secret_scan_config.py DEFAULT STRIPPED TRUSTED

Writes DEFAULT minus every allowlist that exempts a file by its path alone to
STRIPPED, then points a TRUSTED config that extends the default rules at it.
"""
import copy
import json
import os
import re
import sys
import tomllib


class ConfigError(Exception):
    """A config this module will not hand to the scan."""


def _keys(table, prefix=""):
    for key, value in table.items():
        name = prefix + key.lower()
        yield name, value
        if isinstance(value, dict):
            yield from _keys(value, name + ".")


def refuse_extend_path(config):
    if any(name == "extend.path" and value != "" for name, value in _keys(config)):
        raise ConfigError("trusted .gitleaks.toml sets [extend] path, which gitleaks "
                          "resolves inside the judged checkout: refusing to scan")


def _without_path_allowlists(config):
    expected = copy.deepcopy(config)
    for rule in expected.get("rules", []):
        kept = [entry for entry in rule.get("allowlists", []) if set(entry) != {"paths"}]
        if kept:
            rule["allowlists"] = kept
        else:
            rule.pop("allowlists", None)
    expected.get("allowlist", {}).pop("paths", None)
    return expected


def strip_path_allowlists(text):
    sections = re.split(r"(?m)^(?=\[)", text)
    headers = [section.partition("\n")[0].strip() for section in sections]
    if headers.count("[allowlist]") != 1:
        raise ConfigError("the default config has no single global [allowlist]")
    index = headers.index("[allowlist]")
    sections[index], found = re.subn(r"(?ms)^paths[ \t]*=[ \t]*\[\n.*?^\][ \t]*\n", "",
                                     sections[index])
    if found != 1:
        raise ConfigError("the default config's [allowlist] has no single paths array")
    stripped = "".join(section for header, section in zip(headers, sections)
                       if header != "[[rules.allowlists]]"
                       or set(tomllib.loads(section.partition("\n")[2])) != {"paths"})
    if tomllib.loads(stripped) != _without_path_allowlists(tomllib.loads(text)):
        raise ConfigError("stripping the default config changed more than its path allowlists")
    return stripped


def _one(table, name):
    keys = [key for key in table if key.lower() == name]
    if len(keys) > 1:
        raise ConfigError(f"trusted .gitleaks.toml spells {name} more than once: {keys}")
    return keys[0] if keys else None


def point_at_stripped(text, stripped):
    config = tomllib.loads(text)
    extend_key = _one(config, "extend")
    extend = config[extend_key] if extend_key else {}
    flag = _one(extend, "usedefault") if isinstance(extend, dict) else None
    if flag is None or extend[flag] is False:
        return text
    if _one(extend, "path"):
        raise ConfigError("trusted .gitleaks.toml's [extend] sets path beside useDefault")
    line = re.search(r"(?m)^[ \t]*(?i:usedefault)[ \t]*=[ \t]*true[ \t]*(?:#.*)?$", text)
    if line is None:
        raise ConfigError("trusted .gitleaks.toml has no useDefault = true line to rewrite")
    rewritten = (text[:line.start()] + "path = " + json.dumps(stripped, ensure_ascii=False)
                 + text[line.end():])
    expected = {**config, extend_key: {**{key: value for key, value in extend.items()
                                          if key != flag}, "path": stripped}}
    if tomllib.loads(rewritten) != expected:
        raise ConfigError("rewriting useDefault changed more of trusted .gitleaks.toml")
    return rewritten


def _read(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path, text):
    with open(path + ".new", "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(path + ".new", path)


def main(argv=None):
    default, stripped, trusted = sys.argv[1:] if argv is None else argv
    try:
        text = _read(trusted)
        refuse_extend_path(tomllib.loads(text))
        _write(stripped, strip_path_allowlists(_read(default)))
        _write(trusted, point_at_stripped(text, os.path.abspath(stripped)))
    except (ConfigError, tomllib.TOMLDecodeError) as error:
        sys.exit(f"secret_scan_config.py: {error}")


if __name__ == "__main__":
    main()
