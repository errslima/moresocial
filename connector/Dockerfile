# Moresocial WhatsApp connector: one container per workspace, started by the host provisioner.
FROM node:22.23.3-bookworm-slim
ENV PUPPETEER_SKIP_DOWNLOAD=true NODE_ENV=production
RUN apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates tini util-linux \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY diagnostics.js index.js contacts.js normalize.js protocol.js history.js queue.js server.js ./
USER 1000:1000
ENTRYPOINT ["/usr/bin/tini","--"]
# The lock prevents two processes from opening the same WhatsApp session directory.
CMD ["flock","-n","/session/connector.lock","node","index.js"]
