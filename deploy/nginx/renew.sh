#!/bin/sh
# Obtains then renews the Let's Encrypt certificate for $DOMAIN (HTTP-01 challenge served by nginx).
set -u
trap 'exit 0' TERM INT
if [ -n "${LETSENCRYPT_EMAIL:-}" ]; then
  account="--email $LETSENCRYPT_EMAIL"
else
  account="--register-unsafely-without-email"
fi
staging=""
[ -n "${LETSENCRYPT_STAGING:-}" ] && staging="--staging"
sleep 10  # let nginx start
while :; do
  # shellcheck disable=SC2086
  if certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" $account $staging \
       --agree-tos --non-interactive --keep-until-expiring; then
    delay=43200   # 12 h: certbot only renews 30 days before expiry
  else
    echo "Failed: does the domain point to this server, is port 80 open? Retrying in 1 h."
    delay=3600
  fi
  sleep "$delay" &
  wait $!
done
