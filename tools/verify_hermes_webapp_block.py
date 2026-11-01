"""Static + behavioural check for the Hermes Desktop webapp bootstrap blocks.

The template's bootstrap, watchdog, and tile front are Terraform heredocs: a
syntax error only surfaces after a template push and a full workspace build,
and a front proxy that compiles can still fail in the browser (the entry page
ships a CSP that allowlists inline scripts by sha256, so an unnamed injected
<script> is silently refused and the sign-in card reappears). This checks all
three blocks without a deploy:

- bash -n on the rendered bootstrap and watchdog,
- node --check on the rendered front JS,
- a unit run of the front's injection functions, including that the hash the
  front adds to script-src matches what an independent sha256 of the injected
  script text produces (this is the check the browser makes).

Run: python3 tools/verify_hermes_webapp_block.py
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile

TF = (
    pathlib.Path(__file__).resolve().parents[1]
    / "templates/coder/default-ubuntu-vscode-hermes-desktop/main.tf"
)


def _extract(lines: list[str], start_marker: str, delimiter: str) -> str:
    start = next(i for i, line in enumerate(lines) if start_marker in line)
    body = []
    for line in lines[start + 1:]:
        if line.strip() == delimiter:
            break
        body.append(line)
    return "\n".join(body)


def main() -> int:
    lines = TF.read_text().splitlines()
    # Terraform renders `$${` as `${` before bash sees the script; without the
    # same unescape every `${VAR}` reads as a PID plus a brace.
    bootstrap = _extract(
        lines, 'cat >"$HERMES_BOOTSTRAP_SCRIPT" <<\'SH\'', "SH"
    ).replace("$${", "${")
    watchdog = (
        _extract(lines, 'cat >"$HERMES_WATCHDOG_SCRIPT" <<WDSH', "WDSH")
        .replace("\\$", "$")
        .replace('\\"', '"')
        .replace("$${", "${")
    )
    front_js = _extract(
        lines, 'cat >"$HERMES_WEBAPP_FRONT_FILE" <<\'MJS\'', "MJS"
    ).replace("$${", "${")

    ok = True
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        for name, text in (("bootstrap", bootstrap), ("watchdog", watchdog)):
            path = tmp / f"{name}.sh"
            path.write_text(text)
            res = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            print(f"[syntax] {name}: {'ok' if res.returncode == 0 else res.stderr.strip()}")
            ok = ok and res.returncode == 0

        spawn_markers = ('PROXY_PORT="$HERMES_WEBAPP_PORT"', 'nohup env HERMES_HOME="$HERMES_HOME"')
        for expected in spawn_markers:
            if expected not in bootstrap:
                print(f"[bootstrap] FAIL: missing {expected!r}")
                ok = False
        if "$HERMES_HOME/webapp/trust-proxy" not in bootstrap:
            print("[bootstrap] FAIL: no trust-proxy marker write")
            ok = False

        front = tmp / "front.mjs"
        front.write_text(front_js)
        check = subprocess.run(["node", "--check", str(front)], capture_output=True, text=True)
        print(f"[front js] node --check: {'ok' if check.returncode == 0 else check.stderr.strip()}")
        ok = ok and check.returncode == 0

        # Unit-run the injection functions: keep everything up to the server,
        # append assertions. The prefix carries the imports and TOKEN_FILE.
        prefix = front_js[: front_js.index("const server = http.createServer")]
        token = "a" * 42 + "Z"
        token_file = tmp / "session-token"
        token_file.write_text(token + "\n")
        unit = prefix + """
import assert from "node:assert";
const clean = ['<!doctype html><html>', '<head><meta charset="utf-8">',
  '<title>Hermes</title>', '</head><body></body></html>'].join('');
const csp = ["default-src 'none'", "script-src 'self' blob: 'sha256-aaa='",
  "script-src-attr 'none'", "style-src 'self'"].join('; ');
const headers = { "content-security-policy": csp };
const out = injectSignIn(clean, headers);
assert.equal(out.includes("sessionStorage.setItem"), true, "grant not injected");
assert.equal(out.includes("<head><script>"), true, "grant not injected at head start");
assert.equal(out.includes(clean.slice(0, 20)), true, "original page not preserved");
const inner = out.match(/<script>([\\s\\S]*?)<\\/script>/)[1];
const expected = "sha256-" + crypto.createHash("sha256").update(inner, "utf8").digest("base64");
const sent = headers["content-security-policy"];
assert.equal(sent.includes(`'${expected}'`), true, `CSP missing injected hash ${expected}`);
assert.equal(sent.includes("script-src-attr 'none'"), true, "script-src-attr damaged");
const hashed = /script-src[^;]*sha256-[A-Za-z0-9+/=]+/.test(sent);
assert.equal(hashed, true, "no hash in script-src");
const again = injectSignIn(out, { "content-security-policy": sent });
assert.equal(again, out, "double injection");
const noTok = injectSignIn(clean, { "content-security-policy": csp });
assert.equal(noTok.includes("sessionStorage.setItem"), true);
console.log(JSON.stringify({ unit: "ok", hash: expected }));
"""
        # the prefix's TOKEN_FILE const is env-driven; point it at the fixture
        res = subprocess.run(
            ["node", "--input-type=module", "-e", unit],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "HERMES_WEBAPP_TOKEN_FILE": str(token_file)},
        )
        unit_ok = res.returncode == 0 and re.search(r'"unit":\s*"ok"', res.stdout) is not None
        detail = res.stdout.strip() or res.stderr.strip().splitlines()[-1:]
        print(f"[front unit] {'ok' if unit_ok else 'FAIL'}: {detail}")
        ok = ok and unit_ok

    print("VERDICT:", "ok" if ok else "problems found")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
