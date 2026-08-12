"""Code provenance for pipeline stages.

The graph is static source analysis, not ``sys.modules`` state: a stage digest
must depend on the code it can execute, not on which command happened to run
first in the current process.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Callable

CODE_KEY = "<code>"

STAGE_CODE_SEEDS = {
    "inspected": ("office.deck_digest", "office.render", "office.roundtrip"),
    "proposed": ("orchestration.agent",),
    "recipe": ("orchestration.agent",),
    "degraded": ("office.degrade_exec", "office.pkg_check"),
    "materialised": ("tasks.assets",),
    "reconciled": ("orchestration.agent",),
    "solvable": ("orchestration.agent",),
    "scored": ("evaluation.comparators", "evaluation.inventory"),
    "hardened": ("evaluation.attacks",),
    "packaged": ("evaluation.consistency", "tasks.emit", "tasks.emit_tests",
                 "delivery.publish"),
}

CODE_LEAVES = frozenset({"core.pipeline", "orchestration.agent",
                         "orchestration.prompts"})
STAGE_PROMPT = {
    "proposed": "propose_prompt",
    "recipe": "recipe_prompt",
    "reconciled": "reconcile_prompt",
    "solvable": "solvability_prompt",
}
STAGE_PROMPT_MODULE = {stage: "orchestration.prompts" for stage in STAGE_PROMPT}
STAGE_CODE_EXTRA = {
    stage: (("orchestration.prompts", "orchestration.profiles")
            if stage == "proposed" else
            ("orchestration.prompts", "orchestration.solvability")
            if stage == "solvable" else ("orchestration.prompts",))
    for stage in STAGE_PROMPT
}
CODE_EXCLUDED = frozenset({"commands.cli", "office.tools",
                           "orchestration.observe", "delivery.corpus",
                           "office.fonts"})


def module_sources(package: Path) -> dict[str, Path]:
    """Implementation modules keyed by canonical package-relative name."""
    return {
        ".".join(path.relative_to(package).with_suffix("").parts): path
        for path in package.rglob("*.py") if path.name != "__init__.py"
    }


def imported_modules(current: str, node, names: set[str]) -> set[str]:
    """Resolve one AST import to canonical package-relative module names."""
    if isinstance(node, ast.Import):
        return {name for alias in node.names
                if (name := alias.name.removeprefix("pptxgym.")) in names}
    if not isinstance(node, ast.ImportFrom):
        return set()
    if node.level:
        parent = current.split(".")[:-1]
        prefix = parent[:max(0, len(parent) - (node.level - 1))]
        if node.module:
            prefix += node.module.split(".")
    else:
        module = (node.module or "").removeprefix("pptxgym.")
        prefix = module.split(".") if module else []
    base = ".".join(prefix)
    result = {base} if base in names else set()
    for alias in node.names:
        candidate = ".".join([*prefix, alias.name])
        if candidate in names:
            result.add(candidate)
    return result


def import_graph(sources: dict[str, Path]) -> dict[str, set[str]]:
    names = set(sources)
    graph = {}
    for name, path in sorted(sources.items()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (OSError, SyntaxError):
            graph[name] = set()
            continue
        dependencies = set()
        for node in ast.walk(tree):
            dependencies |= imported_modules(name, node, names)
        graph[name] = dependencies - {name} - CODE_EXCLUDED
    return graph


def stage_module_keys(stage: str, sources: dict[str, Path],
                      cache: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    if stage in cache:
        return cache[stage]
    seeds = STAGE_CODE_SEEDS.get(stage)
    if not seeds:
        cache[stage] = ()
        return ()
    graph = import_graph(sources)
    seen = set()
    stack = [*seeds, *STAGE_CODE_EXTRA.get(stage, ())]
    while stack:
        module = stack.pop()
        if module in seen or module in CODE_EXCLUDED:
            continue
        seen.add(module)
        if module not in CODE_LEAVES:
            stack += sorted(graph.get(module, ()))
    cache[stage] = result = tuple(sorted(seen))
    return result


def display_modules(keys: tuple[str, ...]) -> tuple[str, ...]:
    ordered = sorted(keys, key=lambda key: (key.rsplit(".", 1)[-1], key))
    return tuple(key.rsplit(".", 1)[-1] for key in ordered)


def prompt_parts(path: Path, prompt: str, digest: Callable[[Path], str],
                 cache: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Hash shared prompt machinery separately from one prompt function."""
    key = f"{path}:{prompt}"
    if key in cache:
        return cache[key]
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(path))
    except (OSError, SyntaxError):
        return (digest(path) if path.exists() else "-"), prompt
    lines = source.splitlines(keepends=True)
    prompts = {node.name: node for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in set(STAGE_PROMPT.values())}
    if prompt not in prompts:
        return (digest(path) if path.exists() else "-"), prompt
    cut = {index for node in prompts.values()
           for index in range(node.lineno - 1, node.end_lineno)}
    shared = "".join(line for index, line in enumerate(lines) if index not in cut)
    node = prompts[prompt]
    body = "".join(lines[node.lineno - 1:node.end_lineno])
    result = (hashlib.sha1(shared.encode()).hexdigest()[:16],
              hashlib.sha1(body.encode()).hexdigest()[:16])
    cache[key] = result
    return result


def code_digest(stage: str, sources: dict[str, Path],
                digest: Callable[[Path], str],
                closure_cache: dict[str, tuple[str, ...]],
                digest_cache: dict[str, str],
                prompt_cache: dict[str, tuple[str, str]]) -> str | None:
    modules = stage_module_keys(stage, sources, closure_cache)
    if not modules:
        return None
    if stage in digest_cache:
        return digest_cache[stage]
    hasher = hashlib.sha1()
    for name in sorted(modules,
                       key=lambda key: (key.rsplit(".", 1)[-1], key)):
        path = sources.get(name)
        hasher.update(name.rsplit(".", 1)[-1].encode())
        hasher.update(b"\0")
        if path is None:
            hasher.update(b"-")
        elif stage in STAGE_PROMPT and name == STAGE_PROMPT_MODULE[stage]:
            shared, prompt = prompt_parts(
                path, STAGE_PROMPT[stage], digest, prompt_cache)
            hasher.update(shared.encode())
            hasher.update(b"\0")
            hasher.update(prompt.encode())
        else:
            hasher.update((digest(path) if path.exists() else "-").encode())
        hasher.update(b"\n")
    digest_cache[stage] = result = hasher.hexdigest()[:16]
    return result
