# PostgreSQL 16 with pgBackRest for continuous WAL archiving and point-in-time recovery.
# Alpine's pgbackrest package pulls in a whole newer PostgreSQL server as a convenience dependency,
# so it is installed in a throwaway stage and only its binary is copied, with its runtime libraries.
FROM postgres:16-alpine AS pgbackrest
RUN apk add --no-cache --no-scripts pgbackrest

FROM postgres:16-alpine
RUN apk add --no-cache libbz2 lz4-libs libssh2 libxml2 zstd-libs
COPY --from=pgbackrest /usr/bin/pgbackrest /usr/bin/pgbackrest
RUN pgbackrest version && postgres --version
