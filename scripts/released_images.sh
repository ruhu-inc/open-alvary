#!/bin/sh
# Builds each image the release scripts ship (deployment/gcp-terraform/release.sh, deployment/azure/release.sh)
# and starts it with nothing but test settings: the three services answer (the public service's /livez, as
# its /healthz reads a database there is none of here), and the job images run their entry points (findings
# 2026-11-16, item 30). The compose smoke test (scripts/container_smoke.sh) runs other images. Never reads
# production settings or databases.
#   scripts/released_images.sh [public workbench render ingestion restore]
set -eu
cd "$(dirname "$0")/.."
tag="open-alvary-released-check"

build() {
  docker build --quiet -f "$2" -t "$tag-$1" . > /dev/null
}

# Starts a service image with settings $2 and waits up to a minute for $3 to answer 200 on its port 8080.
answers() {
  name="$tag-$1-run"
  # shellcheck disable=SC2086
  docker run -d --rm --name "$name" -p 127.0.0.1::8080 $2 "$tag-$1" > /dev/null || return 1
  port=$(docker port "$name" 8080/tcp | head -n 1 | sed 's/.*://')
  i=0
  while [ "$i" -lt 60 ]; do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port$3" || true)" = "200" ]; then
      docker rm -f "$name" > /dev/null
      echo "$1: answers $3"
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  echo "$1: no answer on $3 within a minute" >&2
  docker logs "$name" >&2 || true
  docker rm -f "$name" > /dev/null || true
  return 1
}

check() {
  case "$1" in
    public)
      build public deployment/public-service.Dockerfile || return 1
      answers public "-e PORT=8080" /livez || return 1
      # The jobs that run on this image (deployment/gcp-terraform/env/jobs.tf) find their modules.
      docker run --rm --entrypoint python "$tag-public" -c "import scripts.check_launch, scripts.seed, \
retrieval.worker, retrieval.indexer, changes.worker, changes.admin, accounts.seals, accounts.privacy, \
accounts.operator, accounts.access_admin, api.traffic, operations.cloud_backups, rights.logins" || return 1
      echo "public: its jobs' modules import"
      ;;
    workbench)
      build workbench deployment/workbench.Dockerfile || return 1
      answers workbench "-e ALVARY_REVIEW_DATABASE_URL=sqlite:////tmp/check.db" / || return 1
      ;;
    render)
      build render deployment/render.Dockerfile || return 1
      answers render "" /healthz || return 1
      ;;
    ingestion)
      build ingestion deployment/ingestion-jobs.Dockerfile || return 1
      docker run --rm --entrypoint sh "$tag-ingestion" -c "pip check && python -c 'import ingestion.scan_job, \
ingestion.ng_documents, ingestion.rw_gazette, google.cloud.documentai'" || return 1
      echo "ingestion: its modules and Document AI import"
      ;;
    restore)
      build restore deployment/azure/restore.Dockerfile || return 1
      # Without a command it would restore: asked for something it does not do, it says what it does.
      said=$(docker run --rm -e DATABASE_URL=postgresql://check@127.0.0.1/check "$tag-restore" --help 2>&1 || true)
      case "$said" in
        *"restore.py describe FOLDER"*) ;;
        *) echo "restore: its entry point did not run: $said" >&2; return 1 ;;
      esac
      docker run --rm --entrypoint sh "$tag-restore" -c "pg_restore --version && pg_dump --version" > /dev/null || return 1
      echo "restore: its entry point runs, with pg_dump and pg_restore"
      ;;
    *)
      echo "no such released image: $1" >&2
      return 2
      ;;
  esac
}

failed=0
for image in ${*:-public workbench render ingestion restore}; do
  check "$image" || failed=1
done
exit "$failed"
