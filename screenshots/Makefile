# dokploy-wizard screenshot studio.
#   make build                 # once, and after studio code changes
#   make login                 # warm the auth states (all services)
#   make shot SHOT=gitea-home  # one shot (raw capture + redacted output)
#   make all                   # every playbook shot, then the demo teardown
#   make redact                # re-run blur/caption from existing raws
#   make demo-status / demo-seed / demo-teardown
#                              # the demo capture prop (see demo_prop.py):
#                              # seed BEFORE a full run (the shots that prove
#                              # multi-user surfaces need the demo rows);
#                              # `all` tears it down at the end so the box
#                              # never keeps demo as a permanent resident.
# URLs and credentials come from the operator env (never committed);
# redaction patterns from screenshots/redact.env (gitignored).
STUDIO_IMAGE := dokploy-wizard-screenshot-studio:local
# Where the operator install env lives (default: repo root, where the wizard
# keeps .install.env; worktrees may point it one directory up).
ENV_DIR      ?= $(abspath ..)
ENV_FILE     ?= .install.env
REDACT_ENV   ?= redact.env
ROOT         := $(abspath ..)
# The image carries the dependencies at /studio; the host scripts are mounted
# over the image copies so studio edits never need a rebuild.
RUN          := docker run --rm -v $(ROOT):/repo -v $(ENV_DIR):/host \
	-v $(ROOT)/screenshots/studio/capture.mjs:/studio/capture.mjs \
	-v $(ROOT)/screenshots/studio/redact.mjs:/studio/redact.mjs \
	-v $(ROOT)/screenshots/studio/playbook.cjs:/studio/playbook.cjs \
	$(STUDIO_IMAGE)
ARGS         := --env-file /host/$(notdir $(ENV_FILE)) --redact-env /repo/screenshots/$(REDACT_ENV) --out /repo/screenshots/raw

.PHONY: build all shot redact demo-status demo-seed demo-teardown clean

build:
	docker build -t $(STUDIO_IMAGE) studio

shot:
	$(RUN) capture.mjs $(ARGS) --shot $(SHOT)
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots --shot $(SHOT)

# A full run owns the demo prop at the END: the box must not keep demo as a
# permanent resident. Seeding is a documented PRE-step (`make demo-seed`,
# then one forced coder pass for the identity pre-flight — see README),
# because the boot lane needs minutes the capture lane cannot wait on.
all:
	$(RUN) capture.mjs $(ARGS) --all
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots
	$(MAKE) demo-teardown

redact:
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots

demo-status:
	uv run $(ROOT)/screenshots/demo_prop.py status --env-file $(ENV_DIR)/$(notdir $(ENV_FILE))

demo-seed:
	uv run $(ROOT)/screenshots/demo_prop.py seed --env-file $(ENV_DIR)/$(notdir $(ENV_FILE))

demo-teardown:
	uv run $(ROOT)/screenshots/demo_prop.py teardown --env-file $(ENV_DIR)/$(notdir $(ENV_FILE))

clean:
	rm -rf raw/.states raw/*.meta.json raw/*.png
