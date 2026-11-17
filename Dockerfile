FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY src ./src
# Saved session, past library names and the anime id list (a volume in compose.yaml).
RUN mkdir -p /app/data && chown node:node /app/data
ENV DATA_DIR=/app/data
USER node
EXPOSE 7000
HEALTHCHECK CMD wget -qO /dev/null http://127.0.0.1:${PORT:-7000}/configure || exit 1
# Node 24 runs TypeScript directly (type stripping), so there is no build step.
CMD ["node", "src/addon.ts"]
