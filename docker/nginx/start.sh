#!/bin/sh
set -e

# The base image's docker-entrypoint.sh only runs /docker-entrypoint.d/*
# (envsubst-on-templates, etc.) when the container's command literally
# starts with "nginx" - ours starts with this script instead, so it needs
# to run those init scripts itself before starting nginx.
if [ -d /docker-entrypoint.d ]; then
  for f in /docker-entrypoint.d/*.sh; do
    [ -x "$f" ] && "$f"
  done
fi

/certs-watch.sh &
exec nginx -g 'daemon off;'
