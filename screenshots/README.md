# Screenshot studio

Every README gallery image is captured by this studio, from the **live box**,
and redacted **deterministically** — no hand-drawn boxes in an editor, no
unrepeatable manual flow. `make all` against a freshly proven box reproduces
the whole gallery byte-comparable modulo page churn.

## How it works

1. **Capture** (`studio/capture.mjs`, Playwright Chromium): drives the real
   URLs of a running stack (login → navigate → act), then walks the rendered
   DOM and records a **character-precise rectangle for every redaction
   pattern hit** (`Range.getClientRects`) into `raw/<shot>.meta.json`.
   Raw PNGs are kept (gitignored) so redaction can be re-run forever.
2. **Redact** (`studio/redact.mjs`, sharp, no browser): blurs every recorded
   rectangle (with a small pad) and appends the caption bar defined in the
   playbook. Output lands at `screenshots/<area>/<shot>.png` — committed via
   Git LFS (`.gitattributes`).
3. **Fail-closed**: the redaction list is data (`redact.env`), the scan is
   exhaustive, and every final image ships a caption that spells the box's
   domain as `«your-domain»`. A shot is only trusted after the post-blur
   report and a human/vision pass.

What gets blurred and what stays:

| Stays visible | Blurred |
|---|---|
| subdomain labels (`gitea.`, `coder.`, `filex.`) | the root domain itself |
| the `dokploy-wizard` provisioner account | operator login, real email, org |
| wizard-managed key/label patterns | tailnet peer labels, box IP |
| `«your-domain»` captions | any literal root-domain text |

## Playbook

`studio/playbook.cjs` is the shot list: id, output area, login identity,
navigation steps, explicit extra blur selectors, and the exact caption text.
Add a shot = add an entry; nothing else moves.

## Re-shooting on a new box

```bash
# 0. the box is up: `dokploy-wizard-remote proof` just passed
cp screenshots/redact.env.example screenshots/redact.env   # fill YOUR values
chmod 600 screenshots/redact.env
cd screenshots
make build
make all                       # or: make shot SHOT=gitea-profile
```

Point the studio at non-default URLs with `--url=service=https://…` appended
to the capture command, and record a demo reel with `--video` (raw WebMs land
in `raw/video/`, also gitignored until a reel is cut).

## Walkthrough lane (second person)

The strip under `walkthrough/` proves the identity lane for a *new* account.
Create the demo user before capturing, delete after — the wizard's own
Coder UI does both (`coder-users` shot shows them side by side):

```bash
# create: Coder UI → Users → Create User  (login: demo, email: demo@<root>,
#   password: any throwaway, no must-change)
make shot SHOT=walkthrough-login && \
  make shot SHOT=walkthrough-workspace && make shot SHOT=walkthrough-gitea-profile
# then delete the demo user again (convert-to-user off): the gallery shows a
# visitor, the box keeps no stranger.
```

The `~/shared` act (`shared/`) needs the same setup twice: write a file from
the deployer's workspace, join as demo, watch the same bytes appear, restore.
The FileX versions shot then shows two authors on one file — the whole
promise of the pack in one image.
