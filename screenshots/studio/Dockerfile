# dokploy-wizard screenshot studio: Playwright Chromium + the capture/redact
# scripts. One image, any box: the URLs, credentials, and redaction patterns
# all arrive at runtime (env file + redact.env + playbook), so nothing here
# bakes an operator's identity in.
FROM mcr.microsoft.com/playwright:v1.55.0-noble

WORKDIR /studio
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY capture.mjs redact.mjs playbook.cjs ./

ENTRYPOINT ["node"]
