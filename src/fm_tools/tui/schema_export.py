"""Build-time export of repo-owned parser controls; never runs workflow handlers.

Run with the development workspace on sys.path and the owners' parser dependencies
available. The shipped snapshot keeps opening FM independent of those packages.
"""

from __future__ import annotations
from dataclasses import asdict
import hashlib
from importlib import import_module
import json
from pathlib import Path
import sys
from .workflows import parser_actions

PROVIDERS = (
    (("data-annotate", "run"), "fm_data_annotate.run_cli", "build_parser"),
    (("data-annotate", "review"), "fm_data_annotate.review_cli", "build_parser"),
    (("data-annotate", "verify"), "fm_data_annotate.verify_cli", "build_parser"),
    (("data-annotate", "benchmark"), "fm_data_annotate.benchmark_cli", "_parser"),
    (("data-pack", "prepare"), "fm_data_package.prepare_cli", "build_parser"),
    (("data-pack", "build"), "fm_data_package.cli", "build_parser"),
    (("data-pack", "verify"), "fm_data_package.verify_cli", "build_parser"),
    (("data-process",), "fm_data_dataset.cli", "build_parser"),
    (("data-showcase",), "fm_data_dataset.showcase_publish_cli", "parser"),
    (("data-archive",), "fm_data_archive.archive_cli", "build_parser"),
    (("policy",), "fm_policy.cli", "build_parser"),
)


def export(root: Path, destination: Path):
    from fm_tools.cli.registry import REPOS

    data = next(repo for repo in REPOS if repo.name == "fm-data").checkout(root)
    policy = next(repo for repo in REPOS if repo.name == "fm-policy").checkout(root)
    sys.path[:0] = [str(p) for p in data.glob("fm_data_*") if p.is_dir()] + [
        str(policy / "src")
    ]
    rows, sources = [], []
    for prefix, module, builder in PROVIDERS:
        owner = import_module(module)
        parser = getattr(owner, builder)()
        source = Path(owner.__file__)
        sources.append(
            {
                "module": module,
                "builder": builder,
                "prefix": list(prefix),
                "path": str(source.relative_to(root)),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
        for action in parser_actions(parser, prefix, "data"):
            if prefix[0] == "data-showcase" and "export" in action.argv:
                continue
            row = asdict(action)
            row["fields"] = [
                asdict(field)
                for field in {field.key: field for field in action.fields}.values()
            ]
            rows.append(row)
    destination.write_text(
        json.dumps({"version": 1, "sources": sources, "actions": rows}, indent=2) + "\n"
    )
    return len(rows)
