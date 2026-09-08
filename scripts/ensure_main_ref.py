"""Make packet diff base ``main`` available without moving a checked-out main."""

from __future__ import annotations
import subprocess
import sys


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], text=True, capture_output=True, check=check)


def has_ref(ref: str) -> bool:
    return run("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0


def main() -> int:
    if not has_ref("refs/remotes/origin/main"):
        run("fetch", "origin", "main:refs/remotes/origin/main")
    head = run("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
    if has_ref("refs/heads/main"):
        if head == "refs/heads/main":
            if run("diff", "--quiet", "main", "refs/remotes/origin/main", check=False).returncode:
                print(
                    "checked-out main differs from origin/main; refusing unsafe update",
                    file=sys.stderr,
                )
                return 1
        else:
            run("update-ref", "refs/heads/main", "refs/remotes/origin/main")
    else:
        run("fetch", "origin", "main:refs/heads/main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
