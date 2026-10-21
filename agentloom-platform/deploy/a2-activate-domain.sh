#!/usr/bin/env bash
# Run on oracle-a2 with sudo. Existing application services and the old host stay intact.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

readonly domain=agentloom.bmilk.cn
readonly site=/etc/nginx/sites-available/agentloom-platform.conf
readonly enabled=/etc/nginx/sites-enabled/agentloom-platform.conf
readonly hook=/etc/letsencrypt/renewal-hooks/deploy/agentloom-platform-nginx.sh
readonly webroot=/var/lib/agentloom-acme

if [[ $EUID -ne 0 ]]; then
    printf 'Run this script with sudo on oracle-a2.\n' >&2
    exit 1
fi
for executable in nginx certbot systemctl curl python3; do
    command -v "$executable" >/dev/null
done
if [[ -L "$site" || -d "$site" || -L "$hook" || -d "$hook" ]]; then
    printf 'Refusing an unexpected symlink or directory at the managed config paths.\n' >&2
    exit 1
fi
if [[ -e "$enabled" || -L "$enabled" ]]; then
    if [[ ! -L "$enabled" || $(readlink -f "$enabled") != "$site" ]]; then
        printf 'The target site already has an unrelated enabled configuration.\n' >&2
        exit 1
    fi
fi
systemctl is-active --quiet nginx
nginx -t
curl --noproxy '*' --fail --silent --show-error --max-time 10 \
    http://127.0.0.1:18766/api/v1/health >/dev/null

backup=$(mktemp -d /var/backups/agentloom-domain.XXXXXXXX)
readonly backup
for name in site enabled hook; do
    path=${!name}
    if [[ -e "$path" || -L "$path" ]]; then
        cp -a -- "$path" "$backup/$name"
    fi
done
completed=0
rollback() {
    result=$?
    trap - EXIT
    if [[ $completed -eq 0 ]]; then
        set +e
        for name in site enabled hook; do
            path=${!name}
            rm -f -- "$path"
            if [[ -e "$backup/$name" || -L "$backup/$name" ]]; then
                cp -a -- "$backup/$name" "$path"
            fi
        done
        if nginx -t; then
            systemctl reload nginx
        fi
        printf 'Domain activation failed; previous Nginx files restored. Backup: %s\n' "$backup" >&2
        [[ $result -ne 0 ]] || result=1
    fi
    exit "$result"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

install -d -o root -g root -m 755 "$webroot"
cat >"$site" <<'NGINX_HTTP'
server {
    listen 80;
    listen [::]:80;
    server_name agentloom.bmilk.cn;
    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/agentloom-acme;
        default_type text/plain;
    }
    location / { return 503; }
}
NGINX_HTTP
chmod 644 "$site"
if [[ ! -L "$enabled" ]]; then
    ln -s "$site" "$enabled"
fi
nginx -t
systemctl reload nginx

# Reuse the existing Certbot account; do not create an account or change TLS policy.
certbot certonly --non-interactive --webroot --webroot-path "$webroot" \
    --domain "$domain" --cert-name "$domain" --keep-until-expiring

cat >"$site" <<'NGINX_HTTPS'
server {
    listen 80;
    listen [::]:80;
    server_name agentloom.bmilk.cn;
    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/agentloom-acme;
        default_type text/plain;
    }
    location / { return 308 https://agentloom.bmilk.cn$request_uri; }
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name agentloom.bmilk.cn;
    ssl_certificate /etc/letsencrypt/live/agentloom.bmilk.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/agentloom.bmilk.cn/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:AgentLoomTLS:10m;
    ssl_session_timeout 1d;
    server_tokens off;
    client_max_body_size 2m;
    location / {
        proxy_pass http://127.0.0.1:8766;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
        proxy_connect_timeout 10s;
    }
}
NGINX_HTTPS
nginx -t
systemctl reload nginx

curl --noproxy '*' --fail --silent --show-error --max-time 15 \
    --retry 3 --retry-delay 1 --retry-connrefused \
    --resolve "$domain:443:127.0.0.1" "https://$domain/api/v1/health" |
    python3 -c 'import json, sys; result=json.load(sys.stdin); assert result["status"]=="ok"; print("Origin HTTPS health check passed; certificate verification enabled.")'

install -d -o root -g root -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat >"$hook" <<'RENEWAL_HOOK'
#!/bin/sh
set -eu
case "${RENEWED_LINEAGE:-}" in
    /etc/letsencrypt/live/agentloom.bmilk.cn)
        /usr/sbin/nginx -t
        /usr/bin/systemctl reload nginx
        ;;
esac
RENEWAL_HOOK
chmod 755 "$hook"
systemctl enable --now certbot.timer
completed=1
printf 'Activated https://%s; existing site preserved. Backup: %s\n' "$domain" "$backup"
