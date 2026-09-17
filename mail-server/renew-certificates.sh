#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Run as an unprivileged user on the trusted certificate management node.
set -euo pipefail
umask 077

mode=${1:-}
case "$mode" in
run | test | deploy) shift ;;
*)
    echo "Usage: $0 {run|test|deploy} SSH_HOST [SSH_HOST ...]" >&2
    exit 2
    ;;
esac
if ((EUID == 0 || $# == 0)); then
    echo "Run as an unprivileged user and specify each node's unique SSH alias." >&2
    exit 2
fi

state=${CERTBOT_STATE_DIR:-"$HOME/.local/share/mail-server-certbot"}
credentials=${CLOUDFLARE_CREDENTIALS:-"$HOME/.config/certbot/cloudflare.ini"}
email=${CERTBOT_EMAIL:-michal@cihar.com}
domain=md.weblate.org
mkdir -p "$state"/{config,work,logs}
exec 9> "$state/run.lock"
flock -n 9 || exit 0

if [[ $mode != deploy ]]; then
    extra=()
    if [[ $mode == test ]]; then
        extra+=(--dry-run)
    fi
    certbot certonly \
        --config-dir "$state/config" \
        --work-dir "$state/work" \
        --logs-dir "$state/logs" \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$credentials" \
        --dns-cloudflare-propagation-seconds 60 \
        --cert-name "$domain" \
        --domain "$domain" \
        --email "$email" \
        --agree-tos --non-interactive --keep-until-expiring \
        "${extra[@]}"
fi

# Staging certificates must never be deployed.
if [[ $mode == test ]]; then
    exit 0
fi

lineage="$state/config/live/$domain"
bundle=$(mktemp "$state/bundle.XXXXXXXX.tar")
trap 'rm -f -- "$bundle"' EXIT
# Certbot's live directory contains symlinks; send the actual PEM files.
tar --create --dereference --file "$bundle" --directory "$lineage" \
    fullchain.pem privkey.pem

result=0
for host in "$@"; do
    echo "Deploying certificate to $host"
    if ! timeout 120 ssh -T \
        -o BatchMode=yes -o StrictHostKeyChecking=yes \
        -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
        -- "$host" 'sudo -n /srv/website/mail-server/install-mail-certificate.sh' < "$bundle"; then
        echo "Deployment failed on $host; other nodes will still be attempted." >&2
        result=1
    fi
done
exit "$result"
