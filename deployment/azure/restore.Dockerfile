# The database restore job (deployment/azure/restore.py): PostgreSQL 16's own tools, matching both
# servers, and Python's standard library; no credentials, no other packages.
FROM postgres:16-alpine@sha256:81e77a4833487b8132fe4b00bedd39dc3f74b8641e387f19312a48f7de218c2e
RUN apk add --no-cache python3
COPY deployment/azure/restore.py /opt/open-alvary/restore.py
USER postgres
ENTRYPOINT ["python3", "/opt/open-alvary/restore.py"]
