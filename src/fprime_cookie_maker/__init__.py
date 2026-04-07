"""Build a cookiecutter template from an existing F Prime artifact folder.

Supported artifact types (auto-detected from folder contents, or override with --type):
  deployment   - folder with Main.cpp and Top/topology.fpp
  component    - folder with a .fpp containing an 'active|passive|queued component' declaration
  subtopology  - folder with a .fpp containing a 'topology <name>' declaration
  module       - any other folder containing .fpp files

Examples:
  fprime-cookie-maker \\
      --source FprimePhasedDeploymentReference/ReferenceDeployment \\
      --output out/cookiecutter-fprime-deployment-custom

  fprime-cookie-maker \\
      --source MyProject/Components/MyComponent \\
      --output out/cookiecutter-fprime-component-custom
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, List, NamedTuple, Optional


# ---------------------------------------------------------------------------
# Replacement descriptor
# ---------------------------------------------------------------------------

class Replacement(NamedTuple):
    """A single text (and path) substitution.

    ``extensions`` restricts the replacement to files whose suffix is in the
    set (e.g. ``frozenset({".fpp"})``).  ``None`` means apply to all files
    and also to path/filename segments.
    """
    old: str
    new: str
    extensions: Optional[FrozenSet[str]] = None  # None → global (files + paths)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def fail(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def first_fpp_at_root(source: Path) -> Optional[Path]:
    """Return the first .fpp file directly inside ``source`` (non-recursive)."""
    for p in sorted(source.iterdir()):
        if p.is_file() and p.suffix == ".fpp":
            return p
    return None


def read_fpp_texts(directory: Path, recursive: bool = False) -> str:
    glob = directory.rglob("*.fpp") if recursive else directory.glob("*.fpp")
    parts: List[str] = []
    for p in sorted(glob):
        try:
            parts.append(p.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            pass
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

ARTIFACT_TYPES = ("deployment", "component", "subtopology", "module")

# Directories and file suffixes that are runtime/build artifacts and should be
# excluded from the generated template by default.
DEFAULT_EXCLUDE_DIRS: FrozenSet[str] = frozenset({
    "logs", ".git", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", ".venv", "venv", "env", "fprime-venv", "DpCat",
})
DEFAULT_EXCLUDE_SUFFIXES: FrozenSet[str] = frozenset({
    ".bin", ".o", ".a", ".so", ".dylib", ".pyc", ".pyo",
    ".obj", ".exe", ".out",
})


def detect_type(source: Path) -> str:
    """Infer the F Prime artifact type from the folder layout."""
    # Deployment: has Main.cpp + Top/ directory
    if (source / "Main.cpp").exists() and (source / "Top").is_dir():
        return "deployment"
    # Scan root-level .fpp content
    text = read_fpp_texts(source, recursive=False)
    if re.search(r'\b(active|passive|queued)\s+component\b', text):
        return "component"
    if re.search(r'\btopology\s+\w', text):
        return "subtopology"
    return "module"


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

@dataclass
class DeploymentContext:
    deployment_name: str
    deployment_namespace: str
    include_path_prefix: str  # trailing slash included, or ""


def detect_deployment(source: Path) -> DeploymentContext:
    name = source.name

    # Namespace: first 'module <X>' in Top/topology.fpp (or any Top .fpp)
    ns = name
    topo_fpp = source / "Top" / "topology.fpp"
    search_text = read_fpp_texts(source / "Top", recursive=False) if (source / "Top").is_dir() else ""
    ns_m = re.search(r'^\s*module\s+([A-Za-z_]\w*)', search_text, re.MULTILINE)
    if ns_m:
        ns = ns_m.group(1)

    # Include prefix: extracted from #include in Main.cpp
    prefix = ""
    main_cpp = source / "Main.cpp"
    if main_cpp.exists():
        text = read_text(main_cpp)
        m = re.search(
            r'#include\s*<(.*?)/' + re.escape(name) + r'/Top/' + re.escape(name) + r'Topology',
            text,
        )
        if m:
            raw = m.group(1).strip("/")
            prefix = (raw + "/") if raw else ""

    return DeploymentContext(
        deployment_name=name,
        deployment_namespace=ns,
        include_path_prefix=prefix,
    )


def deployment_cookiecutter_json(ctx: DeploymentContext) -> dict:
    return {
        "deployment_name": ctx.deployment_name,
        "deployment_namespace": "{{cookiecutter.deployment_name}}",
        "__include_path_prefix": "",
        "__deployment_name_upper": "{{cookiecutter.deployment_name.upper()}}",
        "__prompts__": {
            "deployment_name": "Deployment name",
            "deployment_namespace": "Deployment namespace",
        },
    }


def deployment_hook() -> str:
    return (
        "from fprime.util.cookiecutter_wrapper import is_valid_name\n\n"
        'name = "{{ cookiecutter.deployment_name }}"\n\n'
        'if is_valid_name(name) != "valid":\n'
        "    raise ValueError(\n"
        '        f"Unacceptable deployment name: {name}. '
        'Do not use spaces or special characters"\n'
        "    )\n"
    )


def deployment_replacements(ctx: DeploymentContext) -> List[Replacement]:
    reps: List[Replacement] = []
    if ctx.include_path_prefix:
        reps.append(Replacement(ctx.include_path_prefix, "{{cookiecutter.__include_path_prefix}}"))
    reps += [
        Replacement(ctx.deployment_namespace, "{{cookiecutter.deployment_namespace}}"),
        Replacement(ctx.deployment_name.upper(), "{{cookiecutter.__deployment_name_upper}}"),
        Replacement(ctx.deployment_name, "{{cookiecutter.deployment_name}}"),
    ]
    return reps


# ---------------------------------------------------------------------------
# Component
# ---------------------------------------------------------------------------

@dataclass
class ComponentContext:
    component_name: str
    component_namespace: str
    component_kind: str
    component_short_description: str


def detect_component(source: Path) -> ComponentContext:
    name = source.name
    ns = "Components"
    kind = "active"
    desc = "Component for F Prime FSW framework."

    fpp = first_fpp_at_root(source)
    if fpp:
        text = read_text(fpp)
        ns_m = re.search(r'^\s*module\s+([A-Za-z_]\w*)', text, re.MULTILINE)
        if ns_m:
            ns = ns_m.group(1)
        kind_m = re.search(r'\b(active|passive|queued)\s+component\b', text)
        if kind_m:
            kind = kind_m.group(1)
        desc_m = re.search(r'@\s+(.+)', text)
        if desc_m:
            desc = desc_m.group(1).strip()

    return ComponentContext(
        component_name=name,
        component_namespace=ns,
        component_kind=kind,
        component_short_description=desc,
    )


def component_cookiecutter_json(ctx: ComponentContext) -> dict:
    ordered_kinds = [ctx.component_kind] + [
        k for k in ("active", "passive", "queued") if k != ctx.component_kind
    ]
    return {
        "component_name": ctx.component_name,
        "component_short_description": ctx.component_short_description,
        "component_namespace": ctx.component_namespace,
        "component_kind": ordered_kinds,
        "enable_commands": ["yes", "no"],
        "enable_telemetry": ["yes", "no"],
        "enable_events": ["yes", "no"],
        "enable_parameters": ["yes", "no"],
        "__prompts__": {
            "component_name": "Component name",
            "component_short_description": "Component short description",
            "component_namespace": "Component namespace",
            "component_kind": "Select component kind",
            "enable_commands": "Enable Commands?",
            "enable_telemetry": "Enable Telemetry?",
            "enable_events": "Enable Events?",
            "enable_parameters": "Enable Parameters?",
        },
    }


def component_hook() -> str:
    return (
        "from fprime.util.cookiecutter_wrapper import is_valid_name\n\n"
        '# Check if the component name is valid\n'
        'if is_valid_name("{{ cookiecutter.component_name }}") != "valid":\n'
        "    raise ValueError(\n"
        '        "Unacceptable component name. Do not use spaces or special characters"\n'
        "    )\n"
    )


def component_replacements(ctx: ComponentContext) -> List[Replacement]:
    _FPP = frozenset({".fpp"})
    _FPP_MD = frozenset({".fpp", ".md"})
    return [
        # Namespace appears in .fpp module declarations and C++ namespace blocks
        Replacement(ctx.component_namespace, "{{cookiecutter.component_namespace}}"),
        # Description is annotation text — only meaningful in .fpp and docs
        Replacement(ctx.component_short_description, "{{cookiecutter.component_short_description}}", _FPP_MD),
        # Kind keyword is only safe to replace inside .fpp files
        Replacement(ctx.component_kind, "{{cookiecutter.component_kind}}", _FPP),
        # Name appears everywhere (filenames, includes, declarations)
        Replacement(ctx.component_name, "{{cookiecutter.component_name}}"),
    ]


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------

@dataclass
class ModuleContext:
    module_name: str


def detect_module(source: Path) -> ModuleContext:
    return ModuleContext(module_name=source.name)


def module_cookiecutter_json(ctx: ModuleContext) -> dict:
    return {
        "module_name": ctx.module_name,
        "__prompts__": {
            "module_name": "Module name",
        },
    }


def module_hook() -> str:
    return (
        "from fprime.util.cookiecutter_wrapper import is_valid_name\n\n"
        'name = "{{ cookiecutter.module_name }}"\n\n'
        'if is_valid_name(name) != "valid":\n'
        "    raise ValueError(\n"
        '        f"Unacceptable module name: {name}. '
        'Do not use spaces or special characters"\n'
        "    )\n"
    )


def module_replacements(ctx: ModuleContext) -> List[Replacement]:
    return [Replacement(ctx.module_name, "{{cookiecutter.module_name}}")]


# ---------------------------------------------------------------------------
# Subtopology
# ---------------------------------------------------------------------------

@dataclass
class SubtopologyContext:
    subtopology_name: str
    subtopology_desc: str
    base_id: str


def detect_subtopology(source: Path) -> SubtopologyContext:
    name = source.name
    desc = "My Subtopology Description"
    base_id = "0x10800000"

    fpp = first_fpp_at_root(source)
    if fpp:
        text = read_text(fpp)
        desc_m = re.search(r'@\s+(.+)', text)
        if desc_m:
            desc = desc_m.group(1).strip()

    # base_id lives in <name>Config/<name>Config.fpp or similar
    config_dir = source / f"{name}Config"
    if config_dir.is_dir():
        all_text = read_fpp_texts(config_dir, recursive=True)
        bid_m = re.search(r'constant\s+BASE_ID\s*=\s*(0x[0-9A-Fa-f]+)', all_text)
        if bid_m:
            base_id = bid_m.group(1)

    return SubtopologyContext(subtopology_name=name, subtopology_desc=desc, base_id=base_id)


def subtopology_cookiecutter_json(ctx: SubtopologyContext) -> dict:
    return {
        "subtopology_name": ctx.subtopology_name,
        "subtopology_desc": ctx.subtopology_desc,
        "base_id": ctx.base_id,
        "__subtopology_name_upper": "{{cookiecutter.subtopology_name.upper()}}",
        "__prompts__": {
            "subtopology_name": "Subtopology name",
            "subtopology_desc": "Subtopology description",
            "base_id": "Base ID for Subtopology (in hex, e.g., 0x8000)",
        },
    }


def subtopology_hook() -> str:
    return (
        "from fprime.util.cookiecutter_wrapper import is_valid_name\n\n"
        'name = "{{ cookiecutter.subtopology_name }}"\n\n'
        'if is_valid_name(name) != "valid":\n'
        "    raise ValueError(\n"
        '        f"Unacceptable subtopology name: {name}. '
        'Do not use spaces or special characters"\n'
        "    )\n"
    )


def subtopology_replacements(ctx: SubtopologyContext) -> List[Replacement]:
    _FPP = frozenset({".fpp"})
    _FPP_MD = frozenset({".fpp", ".md"})
    return [
        # UPPER form must come before the plain name to avoid double-substitution
        Replacement(ctx.subtopology_name.upper(), "{{cookiecutter.__subtopology_name_upper}}"),
        Replacement(ctx.subtopology_desc, "{{cookiecutter.subtopology_desc}}", _FPP_MD),
        Replacement(ctx.base_id, "{{cookiecutter.base_id}}", _FPP),
        Replacement(ctx.subtopology_name, "{{cookiecutter.subtopology_name}}"),
    ]


# ---------------------------------------------------------------------------
# Core template builder
# ---------------------------------------------------------------------------

def tokenize_path(rel: Path, replacements: List[Replacement]) -> Path:
    """Substitute concrete names in path segments (global replacements only)."""
    parts = list(rel.parts)
    for r in replacements:
        if r.old and r.extensions is None:
            parts = [p.replace(r.old, r.new) for p in parts]
    return Path(*parts)


def tokenize_text(content: str, replacements: List[Replacement], suffix: str) -> str:
    """Apply substitutions to file content, respecting per-replacement extension filters."""
    out = content
    for r in replacements:
        if not r.old:
            continue
        if r.extensions is None or suffix in r.extensions:
            out = out.replace(r.old, r.new)
    return out


def is_excluded(
    path: Path,
    source: Path,
    exclude_dirs: FrozenSet[str],
    exclude_suffixes: FrozenSet[str],
) -> bool:
    """Return True if *path* (absolute) should be skipped."""
    rel = path.relative_to(source)
    # Skip if any path segment is an excluded directory name
    for part in rel.parts[:-1] if path.is_file() else rel.parts:
        if part in exclude_dirs:
            return True
    # Skip by file suffix
    if path.is_file() and path.suffix in exclude_suffixes:
        return True
    return False


def build_template(
    source: Path,
    output: Path,
    force: bool,
    artifact_type: Optional[str],
    include_all: bool = False,
) -> None:
    if not source.exists() or not source.is_dir():
        fail(f"Source directory not found: {source}")

    exclude_dirs = frozenset() if include_all else DEFAULT_EXCLUDE_DIRS
    exclude_suffixes = frozenset() if include_all else DEFAULT_EXCLUDE_SUFFIXES

    detected = artifact_type or detect_type(source)
    print(f"[INFO] Artifact type  : {detected}")

    if detected == "deployment":
        ctx = detect_deployment(source)
        cc_json = deployment_cookiecutter_json(ctx)
        replacements = deployment_replacements(ctx)
        root_token = "{{cookiecutter.deployment_name}}"
        hook = deployment_hook()
    elif detected == "component":
        ctx = detect_component(source)
        cc_json = component_cookiecutter_json(ctx)
        replacements = component_replacements(ctx)
        root_token = "{{cookiecutter.component_name}}"
        hook = component_hook()
    elif detected == "subtopology":
        ctx = detect_subtopology(source)
        cc_json = subtopology_cookiecutter_json(ctx)
        replacements = subtopology_replacements(ctx)
        root_token = "{{cookiecutter.subtopology_name}}"
        hook = subtopology_hook()
    else:  # module
        ctx = detect_module(source)
        cc_json = module_cookiecutter_json(ctx)
        replacements = module_replacements(ctx)
        root_token = "{{cookiecutter.module_name}}"
        hook = module_hook()

    if output.exists():
        if not force:
            fail(f"Output already exists: {output}  (use --force to overwrite)")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    # Metadata
    write_text(output / "cookiecutter.json", json.dumps(cc_json, indent=4) + "\n")
    write_text(output / "hooks" / "pre_gen_project.py", hook)

    # Tokenized source tree
    root_dir = output / root_token
    root_dir.mkdir(parents=True, exist_ok=True)

    for src_path in sorted(source.rglob("*")):
        if is_excluded(src_path, source, exclude_dirs, exclude_suffixes):
            continue
        rel = src_path.relative_to(source)
        dst = root_dir / tokenize_path(rel, replacements)

        if src_path.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue

        try:
            content = read_text(src_path)
        except UnicodeDecodeError:
            # Binary file — copy as-is
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst)
            continue

        write_text(dst, tokenize_text(content, replacements, src_path.suffix))

    # Summary
    print(f"[INFO] Source          : {source}")
    print(f"[INFO] Output          : {output}")
    print("[INFO] Substitutions   :")
    for r in replacements:
        if r.old:
            scope = f"  [{', '.join(sorted(r.extensions))}]" if r.extensions else "  [all files + paths]"
            print(f"[INFO]   '{r.old}'  →  '{r.new}'{scope}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a cookiecutter template from an existing F Prime artifact.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source", required=True, type=Path,
        help="Path to the existing artifact directory",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Destination directory for the generated cookiecutter template",
    )
    parser.add_argument(
        "--type", dest="artifact_type",
        choices=ARTIFACT_TYPES,
        help="Force artifact type (auto-detected if omitted)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite output directory if it already exists",
    )
    parser.add_argument(
        "--include-all", action="store_true",
        help="Include all files; skip default exclusion of logs/, *.bin, etc.",
    )
    args = parser.parse_args()
    build_template(args.source.resolve(), args.output.resolve(), args.force, args.artifact_type, args.include_all)
