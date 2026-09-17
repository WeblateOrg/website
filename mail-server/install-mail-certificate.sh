#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Install as a root-owned, non-writable helper on each nginx node.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

if ((EUID != 0 || $# != 0)); then
    echo "This helper must run as root with no arguments." >&2
    exit 2
fi

base=/etc/ssl/md.weblate.org
mkdir -p "$base/releases"
exec 9> "$base/install.lock"
flock -w 30 9

release=$(mktemp -d "$base/releases/cert.XXXXXXXX")
previous=$(readlink "$base/current" || true)
switched=0
complete=0
cleanup() {
    if ((complete == 0)); then
        if ((switched == 1)); then
            if [[ -n $previous ]]; then
                ln -s "$previous" "$release/rollback"
                mv -Tf "$release/rollback" "$base/current"
                # Restore the previous running configuration if reload partially succeeded.
                nginx -t && systemctl reload nginx || true
            else
                rm -f -- "$base/current"
            fi
        fi
        rm -rf -- "$release"
    fi
}
trap cleanup EXIT

# Bound the upload and extract only file contents, never archive paths/links.
head -c 1048577 > "$release/upload.tar"
if (($(stat -c %s "$release/upload.tar") > 1048576)); then
    echo "Certificate upload exceeds 1 MiB." >&2
    exit 1
fi
tar --extract --to-stdout --file "$release/upload.tar" fullchain.pem > "$release/fullchain.pem"
tar --extract --to-stdout --file "$release/upload.tar" privkey.pem > "$release/privkey.pem"
rm -- "$release/upload.tar"

# Verify trust, validity, hostname, and that the private key matches the leaf.
openssl verify -purpose sslserver -verify_hostname md.weblate.org \
    -untrusted "$release/fullchain.pem" "$release/fullchain.pem"
openssl x509 -in "$release/fullchain.pem" -pubkey -noout > "$release/cert.pub"
openssl pkey -in "$release/privkey.pem" -passin pass: -pubout > "$release/key.pub"
cmp "$release/cert.pub" "$release/key.pub"
rm -- "$release/cert.pub" "$release/key.pub"

if cmp -s "$release/fullchain.pem" "$base/current/fullchain.pem" &&
    cmp -s "$release/privkey.pem" "$base/current/privkey.pem"; then
    echo "Certificate unchanged; no reload needed."
    exit 0
fi

# Publish both files together; keep the previous release for rollback.
ln -s "$release" "$release/activate"
mv -Tf "$release/activate" "$base/current"
switched=1
nginx -t
systemctl reload nginx
complete=1
echo "Certificate installed and nginx reloaded."
