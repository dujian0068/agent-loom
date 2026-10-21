#!/usr/bin/env bash
# Activate an already issued certificate on oracle-a2. Existing services stay intact.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

readonly domain=agentloom.bmilk.cn
readonly site=/etc/nginx/sites-available/agentloom-platform.conf
readonly enabled=/etc/nginx/sites-enabled/agentloom-platform.conf
readonly hook=/etc/letsencrypt/renewal-hooks/deploy/agentloom-platform-nginx.sh

if [[ $EUID -ne 0 ]]; then
    printf 'Run this script with sudo on oracle-a2.\n' >&2
    exit 1
fi
for executable in nginx systemctl curl python3 openssl; do
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
# Verify the existing certificate before changing any Nginx file. This script
# deliberately does not request or replace certificates.
# BEGIN CERTIFICATE VERIFICATION
openssl verify -purpose sslserver -verify_hostname "$domain" -CApath /etc/ssl/certs \
    -untrusted "/etc/letsencrypt/live/$domain/chain.pem" \
    "/etc/letsencrypt/live/$domain/cert.pem"
# END CERTIFICATE VERIFICATION
openssl x509 -in "/etc/letsencrypt/live/$domain/cert.pem" -noout -subject -enddate
printf 'Reusing the existing certificate; updating only the new domain Nginx site.\n'
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
chmod 644 "$site"
if [[ ! -L "$enabled" ]]; then
    ln -s "$site" "$enabled"
fi
nginx -t
systemctl reload nginx

printf 'Waiting for Nginx to serve the new certificate; TLS verification remains enabled.\n'
# Reload is asynchronous. Until new workers accept connections, the old default
# host can still return its certificate. Retry TLS errors without relaxing trust.
response_file=$(mktemp "$backup/https-health.XXXXXXXX")
# BEGIN HTTPS READINESS CHECK
if ! curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 5 \
    --retry 10 --retry-delay 1 --retry-max-time 30 --retry-all-errors \
    --resolve "$domain:443:127.0.0.1" --output "$response_file" "https://$domain/api/v1/health"; then
    printf 'HTTPS did not become ready; certificate validation was not bypassed.\n' >&2
    exit 1
fi
# Parse only a successful HTTP response, so a TLS error cannot cause a second,
# misleading JSONDecodeError from an empty pipe.
python3 -c 'import json, sys; result=json.load(open(sys.argv[1])); assert result["status"]=="ok"; print("Origin HTTPS health check passed; certificate verification enabled.")' "$response_file"
# END HTTPS READINESS CHECK

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
