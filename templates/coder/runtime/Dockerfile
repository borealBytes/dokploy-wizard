ARG TARGETARCH
ARG RUNTIME_MANIFEST_SHA256
ARG SOURCE_DATE_EPOCH=1782864000
FROM codercom/enterprise-base@sha256:4e01f4c2cc588f3ddd1d8f0591574141689fe6eac82b2040e1ffd587a74877f2
ARG TARGETARCH
ARG RUNTIME_MANIFEST_SHA256
ARG SOURCE_DATE_EPOCH
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
LABEL org.opencontainers.image.base.name="codercom/enterprise-base@sha256:4e01f4c2cc588f3ddd1d8f0591574141689fe6eac82b2040e1ffd587a74877f2"
LABEL org.opencontainers.image.created="${SOURCE_DATE_EPOCH}"
LABEL org.opencontainers.image.revision="${RUNTIME_MANIFEST_SHA256}"
COPY src/dokploy_wizard/runtime-manifest.lock.json /opt/dokploy-wizard/runtime/runtime-manifest.lock.json
COPY --chmod=755 templates/coder/runtime/verify-runtime-assets /usr/local/bin/verify-runtime-assets
COPY templates/coder/runtime/assets/${TARGETARCH}/ /opt/dokploy-wizard/runtime/
RUN --network=none test "$TARGETARCH" = amd64 || test "$TARGETARCH" = arm64
RUN --network=none test -n "$RUNTIME_MANIFEST_SHA256" && test "$(sha256sum /opt/dokploy-wizard/runtime/runtime-manifest.lock.json | cut -d ' ' -f 1)" = "$RUNTIME_MANIFEST_SHA256"
RUN --network=none verify-runtime-assets /opt/dokploy-wizard/runtime
RUN --network=none set -eux; \
    dpkg --install /opt/dokploy-wizard/runtime/install/packages/*.deb; \
    install -m 0755 /opt/dokploy-wizard/runtime/install/opencode/bin/opencode /usr/local/bin/opencode; \
    install -m 0755 /opt/dokploy-wizard/runtime/install/zellij/bin/zellij /usr/local/bin/zellij; \
    install -m 0755 /opt/dokploy-wizard/runtime/install/node/bin/node /usr/local/bin/node; \
    ln -s /opt/dokploy-wizard/runtime/install/node/bin/npm /usr/local/bin/npm; \
    ln -s /opt/dokploy-wizard/runtime/install/node/bin/npx /usr/local/bin/npx; \
    install -m 0755 /opt/dokploy-wizard/runtime/install/proxy-node/bin/node /usr/local/bin/dokploy-wizard-proxy-node; \
    ln -s /opt/dokploy-wizard/runtime/install/pi/node_modules/.bin/pi /usr/local/bin/pi; \
    test -x /opt/dokploy-wizard/runtime/install/hermes/tools/python/bin/python3.11; \
    test -x /opt/dokploy-wizard/runtime/install/hermes/tools/uv/uv; \
    test -f /opt/dokploy-wizard/runtime/install/hermes/source/uv.lock; \
    test -f /opt/dokploy-wizard/runtime/install/hermes/classic/bootstrap.py; \
    test -x /opt/dokploy-wizard/runtime/install/kdense/tools/node/bin/node; \
    test -f /opt/dokploy-wizard/runtime/install/kdense/source/start.mjs; \
    test -d /opt/dokploy-wizard/runtime/install/kdense/source/server/node_modules; \
    test -f /opt/dokploy-wizard/runtime/install/kdense/source/web/.next/BUILD_ID; \
    test -d /opt/dokploy-wizard/runtime/install/kdense/skills-source/skills
