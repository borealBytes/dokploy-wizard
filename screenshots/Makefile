# dokploy-wizard screenshot studio.
#   make build                 # once, and after studio code changes
#   make login                 # warm the auth states (all services)
#   make shot SHOT=gitea-home  # one shot (raw capture + redacted output)
#   make all                   # every playbook shot
#   make redact                # re-run blur/caption from existing raws
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

.PHONY: build all shot redact clean

build:
	docker build -t $(STUDIO_IMAGE) studio

shot:
	$(RUN) capture.mjs $(ARGS) --shot $(SHOT)
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots --shot $(SHOT)

all:
	$(RUN) capture.mjs $(ARGS) --all
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots

redact:
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots

clean:
	rm -rf raw/.states raw/*.meta.json raw/*.png
