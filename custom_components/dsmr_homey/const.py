"""Constants for the integration"""
DOMAIN = "dsmr_homey"


CONF_WS_URL = "ws_url"

DEFAULT_WS_URL = "ws://192.168.86.47:80/ws"

# How long we may stay without a full telegram before we mark the device unavailable
WATCHDOG_TIMEOUT = 90      # seconds (DSMR sends a telegram ~every 10 s)
MAX_BACKOFF = 300          # seconds
