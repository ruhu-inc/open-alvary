# Version-matched PostgreSQL client tools plus GnuPG; holds no keys or credentials.
FROM postgres:16-alpine
RUN apk add --no-cache python3 gnupg rclone
WORKDIR /opt/open-alvary
COPY operations ./operations
USER postgres
ENTRYPOINT ["python3", "-m", "operations.backup"]
