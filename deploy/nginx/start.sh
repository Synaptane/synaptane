#!/bin/sh
# Starts nginx with the Let's Encrypt certificate if it exists, otherwise a temporary
# self-signed certificate; reloads nginx as soon as certbot obtains or renews the certificate.
set -eu
: "${DOMAIN:?Set DOMAIN in .env (proxy mode)}"
live="/etc/letsencrypt/live/$DOMAIN"
fallback=/etc/nginx/selfsigned

if [ ! -f "$fallback/fullchain.pem" ]; then
  mkdir -p "$fallback"
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -subj "/CN=$DOMAIN" \
    -keyout "$fallback/privkey.pem" -out "$fallback/fullchain.pem" 2>/dev/null
fi

render() {
  if [ -f "$live/fullchain.pem" ] && [ -f "$live/privkey.pem" ]; then
    CERT_DIR=$live
  else
    CERT_DIR=$fallback
    echo "Let's Encrypt certificate not available yet: temporary self-signed certificate."
  fi
  export CERT_DIR
  # shellcheck disable=SC2016  # variable names for envsubst, not to be expanded here
  envsubst '$DOMAIN $CERT_DIR' < /etc/synaptane/nginx.conf.template > /etc/nginx/conf.d/default.conf
}

# shellcheck disable=SC2002  # cat | md5sum: empty fingerprint, without error, if the file is missing
fingerprint() { cat "$live/fullchain.pem" 2>/dev/null | md5sum; }

render
nginx -g 'daemon off;' &
pid=$!
trap 'nginx -s quit; wait $pid; exit 0' TERM INT QUIT
last=$(fingerprint)
while kill -0 "$pid" 2>/dev/null; do
  sleep 60 &
  wait $! || true
  now=$(fingerprint)
  if [ "$now" != "$last" ]; then
    render
    nginx -s reload
    echo "Certificate updated: nginx reloaded."
    last=$now
  fi
done
wait "$pid"
