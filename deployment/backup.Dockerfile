# Version-matched PostgreSQL client tools plus GnuPG; holds no keys or credentials.
FROM postgres:16-alpine@sha256:81e77a4833487b8132fe4b00bedd39dc3f74b8641e387f19312a48f7de218c2e
RUN apk add --no-cache python3 gnupg rclone
WORKDIR /opt/open-alvary
COPY operations ./operations
USER postgres
ENTRYPOINT ["python3", "-m", "operations.backup"]
