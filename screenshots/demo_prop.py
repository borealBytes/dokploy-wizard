"""Demo-prop lifecycle for the screenshot workflow: seed, status, teardown.

The demo person is a CAPTURE prop, not a product fixture: shot lanes
(filex-people, litellm-users, coder-users, gitea rows) prove the stack serves
a second human, so the boot lane seeds demo before a full run — and this tool
removes every trace after it, so the box never keeps "demo" as a permanent
resident. One-ID design means the deployer (DOKPLOY_ADMIN_*) can drive every
surface; the wizard's own state files carry the service tokens.

All API shapes here were measured on the box (2026-11-19/20):
  coder   : POST /api/v2/users/login -> {"session_token"}; header spelling
            Coder-Session-Token; user removal rides the product's own CLI
            lane — docker exec -e CODER_URL=http://127.0.0.1:3000
            -e CODER_SESSION_TOKEN=<t> <server> /opt/coder users delete demo
            (REST DELETE /api/v2/users/{user} exists but refuses
            users with workspaces: HTTP 417; REST workspace stop 404s and
            DELETE 405s on this pin — only the CLI cascades workspaces;
            --yes is "unknown flag" there, no-TTY exec runs unconfirmed)
  gitea   : DELETE /api/v1/admin/users/{username}?purge=true with the
            provisioner token (state file gitea-provisioner.json) — purge is
            REQUIRED for accounts holding repos: without it Gitea answers 422
            "user still has ownership of repositories" (measured). Purge
            cascades wizard-demo with the user.
  filex   : GET/DELETE /api/admin/users[/{id}] with the FileX provisioner
            token (state file filex-provisioner.json)
  litellm : GET /user/list, POST /user/delete {"user_ids": [...]} (measured:
            {"user_id": ...} alone is rejected "body user_ids Field required")
            with the master key (LITELLM_MASTER_KEY on the container env)
Everything rides the loopback edge on the box (https://127.0.0.1 + Host
header, -k): UFW blocks host->overlay, and this is the same door the wizard's
own pack lanes use. The gitea-identities ledger drops the demo claim by
itself: the coder identity sync untracks any claim whose account exists
nowhere (see _identity_sync_actions), so the next forced coder pass converges
the ledger — no state-file editing here.

Usage:
  uv run screenshots/demo_prop.py --env-file <install.env> --host <ip> status
  uv run screenshots/demo_prop.py --env-file <install.env> --host <ip> teardown
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

import paramiko  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parent
STATE_DIR = "/root/dokploy-wizard/state"
DEMO_USER = "demo"
DEMO_EMAIL = "demo@wizard.local.test"
DEMO_PASSWORD = "demo-demo-demo"  # the playbook's loginAs password — keep aligned
HOSTS = {
    "coder": "coder.{root}",
    "gitea": "gitea.{root}",
    "filex": "filex.{root}",
}


def _ssh(host: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username="root", password=password, allow_agent=False,
                   look_for_keys=False, auth_timeout=30)
    return client


def _run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, out, err = client.exec_command(cmd, timeout=timeout)
    text = (out.read().decode() + err.read().decode()).strip()
    return str(text)


def _edge(client: paramiko.SSHClient, host: str, method: str, path: str,
          headers: dict[str, str], body: str | None = None) -> tuple[int, str]:
    hdrs = " ".join(f"-H {shlex.quote(f'{k}: {v}')}" for k, v in headers.items())
    data = f" --data {shlex.quote(body)}" if body is not None else ""
    raw = _run(client, f"curl -sk -m 30 -X {method} -w '\\nHTTP%{{http_code}}' "
                       f"{hdrs}{data} https://127.0.0.1{path}")
    body_part, _, code_part = raw.rpartition("HTTP")
    return int(code_part.strip() or 0), body_part.strip()


def _state_json(client: paramiko.SSHClient, name: str) -> dict[str, object]:
    raw = _run(client, f"cat {STATE_DIR}/{name}")
    return json.loads(raw) if raw.startswith("{") else {}


class DemoProp:
    def __init__(self, client: paramiko.SSHClient, root: str,
                 deployer_email: str, deployer_password: str) -> None:
        self.client = client
        self.root = root
        self.email = deployer_email
        self.password = deployer_password

    # -- coder ----------------------------------------------------------------

    def _coder_token(self) -> str:
        _, payload = self._coder("POST", "/api/v2/users/login",
                                 {"email": self.email, "password": self.password})
        return str(json.loads(payload).get("session_token", ""))

    def _coder(self, method: str, path: str, body: dict[str, object] | None = None,
               token: str | None = None) -> tuple[int, str]:
        headers = {"Host": HOSTS["coder"].format(root=self.root),
                   "Content-Type": "application/json"}
        if token:
            headers["Coder-Session-Token"] = token
        return _edge(self.client, HOSTS["coder"].format(root=self.root), method, path,
                     headers, json.dumps(body) if body is not None else None)

    def coder_users(self, token: str) -> list[dict[str, object]]:
        _, payload = self._coder("GET", "/api/v2/users", token=token)
        rows = json.loads(payload).get("users", [])
        return [r for r in rows if isinstance(r, dict)]

    def coder_workspace_names(self, token: str, owner: str) -> list[str]:
        # Owner-QUALIFIED names: workspace names are per-owner (measured —
        # both persons' workspaces are literally 'showmeseewhycom-workspace'),
        # and an unqualified `coder delete <name>` resolves against the
        # TOKEN OWNER. A first teardown pass ate the deployer's workspace
        # that way (recoverable: the coder pack re-ensures the default
        # workspace on its next pass). Never send a bare name.
        from urllib.parse import quote
        _, payload = self._coder("GET", f"/api/v2/workspaces?q={quote('owner:' + owner)}",
                                 token=token)
        rows = json.loads(payload).get("workspaces", [])
        return [f"{r.get('owner_name')}/{r['name']}" for r in rows
                if isinstance(r, dict) and r.get("name")]

    # -- gitea -----------------------------------------------------------------

    def gitea(self, method: str, path: str) -> tuple[int, str]:
        tok = _state_json(self.client, "gitea-provisioner.json").get("token", "")
        return _edge(self.client, HOSTS["gitea"].format(root=self.root), method, path,
                     {"Host": HOSTS["gitea"].format(root=self.root),
                      "Authorization": f"token {tok}"})

    def gitea_usernames(self) -> list[str]:
        code, payload = self.gitea("GET", "/api/v1/admin/users")
        if code != 200:
            return []
        return [str(r.get("username", "")) for r in json.loads(payload) if isinstance(r, dict)]

    # -- filex -----------------------------------------------------------------

    def filex(self, method: str, path: str, body: str | None = None) -> tuple[int, str]:
        tok = _state_json(self.client, "filex-provisioner.json").get("token", "")
        return _edge(self.client, HOSTS["filex"].format(root=self.root), method, path,
                     {"Host": HOSTS["filex"].format(root=self.root),
                      "Authorization": f"Bearer {tok}"}, body)

    def filex_usernames(self) -> list[str]:
        code, payload = self.filex("GET", "/api/admin/users")
        if code != 200:
            return []
        return [str(r.get("username", "")) for r in json.loads(payload) if isinstance(r, dict)]

    # -- litellm ---------------------------------------------------------------

    def _litellm_key(self) -> str:
        raw = _run(self.client,
                   "docker inspect $(docker ps -qf name=litellm | head -1) "
                   "--format '{{range .Config.Env}}{{println .}}{{end}}' "
                   "| grep '^LITELLM_MASTER_KEY=' | head -1")
        return raw.split("=", 1)[1].strip() if "=" in raw else ""

    def litellm_user_ids(self) -> list[str]:
        key = self._litellm_key()
        raw = _run(self.client, f"curl -s -m 15 -H 'Authorization: Bearer {key}' "
                                "http://127.0.0.1:4000/user/list")
        try:
            rows = json.loads(raw).get("users", [])
        except json.JSONDecodeError:
            return []
        return [str(r.get("user_id", "")) for r in rows if isinstance(r, dict)]

    # -- surfaces --------------------------------------------------------------

    def seed(self) -> None:
        """Re-prime the capture prop: coder user + booting workspace.

        The Gitea account (plus its wizard-demo repo) springs through the
        workspace's own boot lane (owner-basic door, same as any real
        person), and FileX/LiteLLM accounts + the ledger claim land on the
        next forced coder pass (identity pre-flight). Seeding is therefore
        only half this function — the other half is the product converging,
        which is exactly the demo being real, not painted in.
        """
        token = self._coder_token()
        container = self._coder_server_container()
        env = ("-e CODER_URL=http://127.0.0.1:3000 "
               f"-e CODER_SESSION_TOKEN={token}")
        existing = {str(u.get("username")) for u in self.coder_users(token)}
        if DEMO_USER not in existing:
            out = _run(self.client,
                       f"docker exec {env} {container} /opt/coder users create "
                       f"-u {DEMO_USER} -e {DEMO_EMAIL} --login-type password "
                       f"-p {DEMO_PASSWORD} 2>&1; echo EXIT=$?")
            print(f"coder user '{DEMO_USER}':\n{out}")
        else:
            print(f"coder: '{DEMO_USER}' already exists")
        # Measured: users created by the CLI land DORMANT, and a dormant
        # user's agent 401s forever ("User is not active"), so the boot lane
        # never runs. Activate unconditionally — idempotent for a live demo.
        # `activate` prompts ("yes/no") and EOFs on a no-TTY exec; unlike
        # `create`, `--yes` is rejected per-command, so the honest lane
        # answers the prompt on stdin (`docker exec -i`).
        out = _run(self.client,
                   f"echo yes | docker exec -i {env} {container} /opt/coder users activate "
                   f"{DEMO_USER} 2>&1; echo EXIT=$?")
        print(f"coder activate '{DEMO_USER}':\n{out[-300:]}")
        ws = f"{DEMO_USER}/showmeseewhycom-workspace"
        if not self.coder_workspace_names(token, DEMO_USER):
            out = _run(self.client,
                       f"docker exec {env} {container} /opt/coder create {ws} "
                       f"--template ubuntu-vscode-hermes-desktop --yes 2>&1; "
                       f"echo EXIT=$?", timeout=600)
            # Measured: `create` asks "Confirm create? (yes/no)" and EOFs on
            # a no-TTY exec — the GLOBAL --yes (accepted here, unlike on
            # `users delete`) is what makes it run through.
            print(f"coder workspace '{ws}':\n{out[-500:]}")
        else:
            print("coder: demo workspace already listed")

    def status(self) -> dict[str, list[str]]:
        token = self._coder_token()
        users = self.coder_users(token)
        tracked = _state_json(self.client, "gitea-identities.json").get("usernames", [])
        return {
            "coder": [str(u.get("username")) for u in users
                      if str(u.get("status") or "") != "deleted"],
            "gitea": self.gitea_usernames(),
            "filex": self.filex_usernames(),
            "litellm": self.litellm_user_ids(),
            "ledger": [str(u) for u in tracked] if isinstance(tracked, list) else [],
        }

    def _coder_server_container(self) -> str:
        # The compose-task naming on the box: {stack}-{random}-{service}-1.
        # The two workspace containers also carry "coder" in their names,
        # so match the task suffix exactly (measured on the box).
        names = _run(self.client, "docker ps --format '{{.Names}}'").splitlines()
        for name in names:
            if name.endswith("-coder-1"):
                return name
        raise RuntimeError("no running Coder server container")

    def teardown(self) -> None:
        token = self._coder_token()
        demo = next((u for u in self.coder_users(token)
                     if str(u.get("username")) == DEMO_USER
                     and str(u.get("status") or "") != "deleted"), None)
        if demo:
            ws_before = self.coder_workspace_names(token, DEMO_USER)
            container = self._coder_server_container()
            # Measured: this pin's `users delete` does NOT cascade
            # workspaces ("You cannot delete a user that has workspaces"),
            # so each workspace rides the product's own delete lane first.
            for ws in ws_before:
                out = _run(self.client,
                           f"docker exec -e CODER_URL=http://127.0.0.1:3000 "
                           f"-e CODER_SESSION_TOKEN={token} {container} "
                           f"/opt/coder delete {ws} --yes 2>&1; echo EXIT=$?")
                print(f"coder workspace '{ws}':\n{out}")
            out = _run(self.client,
                       f"docker exec -e CODER_URL=http://127.0.0.1:3000 "
                       f"-e CODER_SESSION_TOKEN={token} {container} "
                       f"/opt/coder users delete {DEMO_USER} 2>&1; echo EXIT=$?")
            # Measured: `users delete` rejects --yes ("unknown flag"); with no
            # TTY on docker exec the command runs straight through.
            print(f"coder user '{DEMO_USER}' (after {len(ws_before)} workspace(s)):\n{out}")
            ws_after = self.coder_workspace_names(token, DEMO_USER)
            if ws_after:
                print(f"WARNING: {len(ws_after)} demo workspace(s) still listed")
        else:
            print("coder: no live demo user")

        code, payload = self.gitea("DELETE", f"/api/v1/admin/users/{DEMO_USER}?purge=true")
        print(f"gitea user '{DEMO_USER}' (purged, repos cascade): HTTP {code} {payload[:160]}")

        code, payload = self.filex("GET", "/api/admin/users")
        demo_row = next((r for r in (json.loads(payload) if code == 200 else [])
                         if isinstance(r, dict) and r.get("username") == DEMO_USER), None)
        if demo_row:
            code, payload = self.filex("DELETE", f"/api/admin/users/{demo_row['id']}")
            print(f"filex user '{DEMO_USER}': HTTP {code} {payload[:160]}")
        else:
            print("filex: no demo user")

        key = self._litellm_key()
        raw = _run(self.client, f"curl -s -m 20 -X POST -H 'Authorization: Bearer {key}' "
                                f"-H 'Content-Type: application/json' "
                                f"-d '{{\"user_ids\": [\"{DEMO_USER}\"]}}' http://127.0.0.1:4000/user/delete")
        print(f"litellm user '{DEMO_USER}': {raw[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["status", "seed", "teardown"])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--host", help="defaults to VPS_HOST in the env file")
    args = parser.parse_args()

    values: dict[str, str] = {}
    for line in args.env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    host = args.host or values.get("VPS_HOST", "")
    if not host:
        parser.error("--host required (no VPS_HOST in the env file)")
    client = _ssh(host, values["VPS_ROOT_PASSWORD"])
    try:
        prop = DemoProp(client, values["ROOT_DOMAIN"],
                        values["DOKPLOY_ADMIN_EMAIL"], values["DOKPLOY_ADMIN_PASSWORD"])
        if args.action == "teardown":
            prop.teardown()
        elif args.action == "seed":
            prop.seed()
        print(json.dumps(prop.status(), indent=2))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
