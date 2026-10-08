#!/bin/sh
# Generate the backup encryption key pair on a trusted workstation, never on the server.
# Output: <dir>/backup-recipient.asc (public; copy to the server) and
#         <dir>/backup-identity.asc (private; store in a password manager and an offline copy).
set -eu
umask 077
out=${1:?Usage: scripts/backup_keygen.sh <output-directory> "<Name> <email>"}
uid=${2:?Usage: scripts/backup_keygen.sh <output-directory> "<Name> <email>"}
mkdir -p "$out"
home=$(mktemp -d /tmp/oa-keygen-XXXXXX)
trap 'gpgconf --homedir "$home" --kill gpg-agent 2>/dev/null; rm -rf "$home"' EXIT
gpg --homedir "$home" --batch --quick-gen-key "$uid" ed25519 sign 0
fpr=$(gpg --homedir "$home" --with-colons --list-keys | awk -F: '/^fpr:/ {print $10; exit}')
gpg --homedir "$home" --batch --quick-add-key "$fpr" cv25519 encr 0
gpg --homedir "$home" --armor --export "$fpr" > "$out/backup-recipient.asc"
gpg --homedir "$home" --armor --export-secret-keys "$fpr" > "$out/backup-identity.asc"
echo "Fingerprint: $fpr"
echo "Public key:  $out/backup-recipient.asc  (server: BACKUP_RECIPIENT_FILE)"
echo "Private key: $out/backup-identity.asc  (protect with its passphrase; keep two offline copies)"
