#!/bin/sh
# nginx loads ssl_certificate/ssl_certificate_key once at startup/reload and
# never re-reads them on its own. A custom cert upload or a Let's Encrypt
# renewal (both happen in the backend container, sharing the /certs volume)
# need this to actually take effect without restarting the container.
LAST_MTIME=""
while true; do
  sleep 30
  if [ -f /certs/cert.pem ]; then
    MTIME=$(stat -c %Y /certs/cert.pem 2>/dev/null)
    if [ -n "$LAST_MTIME" ] && [ "$MTIME" != "$LAST_MTIME" ]; then
      echo "cert changed, reloading nginx"
      nginx -s reload
    fi
    LAST_MTIME="$MTIME"
  fi
done
