"""Process boundary for streaming shared repo update results to the TUI."""

from __future__ import annotations
import json
import sys
from pathlib import Path
from fm_tools.cli.registry import REPOS
from fm_tools.cli.update import update_repo


def main() -> int:
    root = Path(sys.argv[1])
    names = sys.argv[2:]
    repos = [repo for repo in REPOS if repo.name in names]
    if not names or len(repos) != len(set(names)):
        raise ValueError("Select registered repos to update.")
    failed = False
    for index, repo in enumerate(repos):
        print(
            json.dumps(
                {
                    "event": "progress",
                    "name": repo.name,
                    "done": index,
                    "total": len(repos),
                }
            ),
            flush=True,
        )
        row = update_repo(repo, root)
        print(json.dumps({"event": "result", "row": row}), flush=True)
        failed |= not row["ok"]
    return 4 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
