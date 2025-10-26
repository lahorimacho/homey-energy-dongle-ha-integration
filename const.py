"""Constants for the integration"""
DOMAIN = "dsmr_homey"

CONF_MODE = "mode"            # "websocket" or "tcp" or "serial"
CONF_WS_URL = "ws_url"        # full websocket URL e.g. ws://192.168.1.100:80/
CONF_TCP_HOST = "tcp_host"    # fallback TCP host
CONF_TCP_PORT = "tcp_port"    # fallback TCP port
CONF_SERIAL_PORT = "serial_port"
CONF_SERIAL_BAUDRATE = "baudrate"
CONF_DECRYPTION_KEY = ""
CONF_DISCOVER = "discover"  # Bool: Enable mDNS

DEFAULT_MODE = "websocket"
DEFAULT_WS_URL = "ws://192.168.86.47:80/ws"
DEFAULT_TCP_HOST = "192.168.1.100"
DEFAULT_TCP_PORT = 12000
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"
DEFAULT_BAUDRATE = 115200

# coordinator settings
SCAN_INTERVAL = 10  # seconds - main heartbeat; incoming telegrams are parsed as they arrive
WATCHDOG_TIMEOUT = 120  # seconds without a telegram => mark unavailable
MAX_BACKOFF = 300  # max reconnection backoff seconds
