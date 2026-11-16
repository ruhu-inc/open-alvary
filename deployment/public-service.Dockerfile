# The public service on Cloud Run (deployment/gcp/public_service.sh): the API service (api.service) and
# the built website in one image, behind the load balancer (MAC-12, D7). Built from the commit itself
# (git archive); .gcloudignore keeps private material out regardless.
FROM node:22-alpine@sha256:1314fb30ec37592d5845e5a160d522c0e680972aa534549940b8d73bde88481a AS website
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY --from=website /web/dist /app/web/dist
RUN useradd --create-home corpus && chown -R corpus:corpus /app
USER corpus
ENV ALVARY_FRONTEND=/app/web/dist
# Cloud Run sets PORT. Admission reads X-Forwarded-For itself (trusted_proxies), so uvicorn's own
# proxy-header handling stays off.
CMD ["sh", "-c", "exec uvicorn api.service:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log --timeout-keep-alive 5"]
