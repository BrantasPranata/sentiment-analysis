# Frontend SPA (Svelte + Vite) disajikan nginx di port 8080; /api/ diteruskan ke backend.

# ---- Tahap 1: build ----
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# ---- Tahap 2: runtime ----
FROM nginx:1.29-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
