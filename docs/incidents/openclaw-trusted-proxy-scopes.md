# OpenClaw Trusted-Proxy Scope Incident

## Summary

OpenClaw webchat sessions behind Cloudflare Access could connect successfully while still failing to load the agent list with errors such as `missing scope: operator.read` or `missing scope: operator.write`.

This was not a missing-agent seed problem. The live runtime still had `main`, `nexa`, and `telly`, but the control UI connection was authenticated without retaining operator scopes.

## Symptoms

- Agents tab shows no agents.
- OpenClaw logs contain repeated `agents.list`, `node.list`, or `config.get` failures with `missing scope: operator.read`.
- Telegram and other server-side channels may still work, which makes the bug look like a UI-only issue.

## Root Cause

For `gateway.auth.mode = trusted-proxy`, the OpenClaw control UI can still enforce device identity. In that mode, trusted-proxy operator sessions may have their unbound scopes cleared during the webchat handshake.

On our installs this produced a session that was allowed to connect but lacked the `operator.read` / `operator.write` scopes needed by the control UI.

## Wizard Fix

Dokploy Wizard now seeds the OpenClaw gateway control UI with:

```json
{
  "gateway": {
    "controlUi": {
      "dangerouslyDisableDeviceAuth": true
    }
  }
}
```

This is applied only when OpenClaw is configured to use trusted-proxy auth, which is the intended mode for the Cloudflare Access protected web UI.

## Related Bootstrap Fixes

The same hardening work also keeps two earlier OpenClaw bootstrap fixes in place:

- seed OpenClaw with the real generated LiteLLM virtual key instead of an unresolved placeholder
- keep `DOKPLOY_WIZARD_NEXA_VISIBLE_WORKSPACE_ROOT` aligned with the Nexa contract workspace root so fresh installs do not regress into the old EACCES path issue

## Fresh VPS Expectations

On a fresh VPS, the wizard should now bootstrap OpenClaw with:

- agents: `main`, `nexa`, `telly`
- bindings:
  - `nexa -> nextcloud-talk`
  - `telly -> telegram`
- local model routing through LiteLLM to `local/unsloth-active`
- trusted-proxy control UI sessions that retain operator scopes

## Existing VPS Recovery

Existing boxes may still carry stale seeded OpenClaw state. If source has been updated but the UI still shows missing operator scopes, OpenClaw may need a targeted reseed so the generated `openclaw.json` is rewritten from current source.
