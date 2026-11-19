# dokploy-wizard screenshot studio.
#   make build                 # once, and after studio code changes
#   make login                 # warm the auth states (all services)
#   make shot SHOT=gitea-home  # one shot (raw capture + redacted output)
#   make all                   # every playbook shot
#   make redact                # re-run blur/caption from existing raws
# URLs and credentials come from the operator env (never committed);
# redaction patterns from screenshots/redact.env (gitignored).
STUDIO_IMAGE := dokploy-wizard-screenshot-studio:local
ENV_FILE     ?= ../.install.env
REDACT_ENV   ?= redact.env
ROOT         := $(abspath ..)
RUN          := docker run --rm -v $(ROOT)/..:/repo -w /repo/screenshots/studio $(STUDIO_IMAGE)

.PHONY: build all shot redact clean

build:
	docker build -t $(STUDIO_IMAGE) studio

shot:
	$(RUN) capture.mjs --env-file /repo/$(ENV_FILE) --redact-env /repo/screenshots/$(REDACT_ENV) --shot $(SHOT) --out /repo/screenshots/raw
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots $(if $(SHOT),--shot $(SHOT))

all:
	$(RUN) capture.mjs --env-file /repo/$(ENV_FILE) --redact-env /repo/screenshots/$(REDACT_ENV) --all --out /repo/screenshots/raw
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots

redact:
	$(RUN) redact.mjs --in /repo/screenshots/raw --out /repo/screenshots

clean:
	rm -rf raw/.states raw/*.meta.json raw/*.png
