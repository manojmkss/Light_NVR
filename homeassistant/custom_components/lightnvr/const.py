"""Constants for the LightNVR integration."""

from datetime import timedelta

DOMAIN = "lightnvr"

# Config entry keys
CONF_VERIFY_SSL = "verify_ssl"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"

# Options keys
CONF_FAST_POLL_INTERVAL = "fast_poll_interval"
CONF_SLOW_POLL_INTERVAL = "slow_poll_interval"

DEFAULT_PORT = 8443
DEFAULT_VERIFY_SSL = False  # LightNVR ships a self-signed cert by default

# Fast coordinator: cameras list + motion-status. Cheap, single round trip
# regardless of camera count. 10s is already faster than useful given the
# backend's own 3s motion-stop debounce.
DEFAULT_FAST_POLL_SECONDS = 10
MIN_FAST_POLL_SECONDS = 5

# Slow coordinator: system/status (CPU/disk sample) + system/dashboard (7-day
# heatmap aggregation) - meaningfully heavier, and none of these values change
# faster than a minute anyway.
DEFAULT_SLOW_POLL_SECONDS = 60
MIN_SLOW_POLL_SECONDS = 30

DEFAULT_FAST_INTERVAL = timedelta(seconds=DEFAULT_FAST_POLL_SECONDS)
DEFAULT_SLOW_INTERVAL = timedelta(seconds=DEFAULT_SLOW_POLL_SECONDS)

# Refresh the access token this many seconds before its JWT `exp` claim so a
# long-open MJPEG proxy connection doesn't run out from under an expiring
# token mid-stream.
TOKEN_REFRESH_LEEWAY_SECONDS = 60

MANUFACTURER = "LightNVR"
