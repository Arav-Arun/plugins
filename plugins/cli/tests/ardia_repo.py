import os
from pathlib import Path

CLI = Path(__file__).resolve().parents[1]


def find_ardia_repo() -> Path | None:
    given = os.environ.get("ARDIA_REPO")
    candidates = [Path(given)] if given else [CLI.parents[2]]
    for candidate in candidates:
        if (candidate / "packages" / "core" / "src").is_dir() and (candidate / "apps" / "browser").is_dir():
            return candidate
    return None


ARDIA_REPO = find_ardia_repo()
