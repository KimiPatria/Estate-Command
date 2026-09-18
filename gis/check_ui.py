"""Syntax-check the Estate Command frontend.

    python gis/check_ui.py

The page used to be one file with a single inline <script>, and this module
pulled that script out and ran `node --check` over it, because a stray brace
produced a blank page with an error only the browser console saw.

The page is now an ES module tree under `command_static/src`, built by Vite.
`npm run build` runs two gates:

  - `test/undefined.mjs` walks every module's AST and reports any identifier
    that is used but bound nowhere and is not a browser global. Rollup treats
    such a name as a global the browser will supply, so a forgotten import
    builds perfectly and throws the moment the line runs. This catches it.
  - `vite build` itself, which resolves every import and fails on a missing
    export.

Together they are strictly stronger than the old `node --check`, which only
caught a stray brace.

Node and the npm install are optional here. When either is absent the check
skips rather than fails, so this stays safe to call from any build script.

For behaviour, not just syntax, see `command_static/test/smoke.mjs`, which
loads the real page against a running server and walks every panel.
"""

import shutil
import subprocess
import sys
from pathlib import Path

UI = Path(__file__).parent.parent / "command_static"


def check(ui: Path = UI) -> tuple[bool, str]:
    if not ui.exists():
        return False, f"{ui} not found"
    if not (ui / "node_modules").is_dir():
        return True, "node_modules absent; run `npm install` in command_static/ (skipped)"
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return True, "npm not installed; build check skipped"

    r = subprocess.run([npm, "run", "build"], cwd=str(ui),
                       capture_output=True, text=True)
    if r.returncode:
        return False, (r.stderr or r.stdout).strip()[-2000:]
    modules = sum(1 for _ in (ui / "src").rglob("*.js"))
    return True, f"build clean, {modules} modules"


if __name__ == "__main__":
    ok, msg = check()
    print(("OK   " if ok else "FAIL ") + msg)
    sys.exit(0 if ok else 1)
