FROM node:20-alpine

# Optional build-time proxy. Declared here so `docker compose build` can pass
# HTTP_PROXY / HTTPS_PROXY through without editing this file. Left unset by
# default, so a normal build ignores them.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

WORKDIR /app

# Install dependencies first for better layer caching.
# The mirror is configured in .npmrc (copied below via `COPY . .`), but we also
# set it explicitly so the install step never depends on layer ordering.
COPY package.json package-lock.json* ./
COPY .npmrc* ./
RUN npm config set registry https://registry.npmmirror.com \
    && npm install --no-audit --no-fund

# Copy source and build
COPY . .
RUN npm run build

EXPOSE 5173

CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "5173"]
