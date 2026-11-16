# PostgreSQL 16 with pgBackRest for continuous WAL archiving and point-in-time recovery.
# Alpine's pgbackrest package pulls in a whole newer PostgreSQL server as a convenience dependency,
# so it is installed in a throwaway stage and only its binary is copied, with its runtime libraries.
FROM postgres:16-alpine@sha256:81e77a4833487b8132fe4b00bedd39dc3f74b8641e387f19312a48f7de218c2e AS pgbackrest
RUN apk add --no-cache --no-scripts pgbackrest

FROM postgres:16-alpine@sha256:81e77a4833487b8132fe4b00bedd39dc3f74b8641e387f19312a48f7de218c2e
RUN apk add --no-cache libbz2 lz4-libs libssh2 libxml2 zstd-libs
COPY --from=pgbackrest /usr/bin/pgbackrest /usr/bin/pgbackrest
RUN pgbackrest version && postgres --version
