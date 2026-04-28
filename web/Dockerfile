# Alfred React Dossier UI.
#
# Stage 1: build static assets with Vite.
# Stage 2: serve them via nginx and proxy /sink/* to the FastAPI Python sink,
#          mirroring the Vite dev proxy in vite.config.ts. SINK_URL is
#          substituted into the nginx config at container start.
#
# Build:  docker build -t alfred-web .
# Run:    docker run -p 80:80 -e SINK_URL=https://your-sink.example alfred-web

FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Restrict envsubst to SINK_URL so nginx's own $host / $remote_addr / etc.
# are left intact in the rendered config.
ENV NGINX_ENVSUBST_FILTER=^SINK_URL$

EXPOSE 80
