"""Constants for the Elegant LED Controller integration."""

DOMAIN = "elegant"

# Default host
DEFAULT_HOST = "elegant.lan"

# WebSocket
WS_PORT = 80
WS_PATH = "/data"

# Defaults for zone reset
DEFAULT_COLOR_MODE = 0
DEFAULT_COLOR = "0xFFFFFF"
DEFAULT_COLOR_HUE = 0
DEFAULT_WHITE_TEMPERATURE = 50
DEFAULT_COLOR_SATURATION = 0
DEFAULT_BRIGHTNESS = 100

# Elegant white_temperature range (0-100) mapped to Kelvin
MIN_COLOR_TEMP_KELVIN = 2700
MAX_COLOR_TEMP_KELVIN = 6500

# Elegant hue scale for SENDING commands: 0-255
# Conversion: elegant_hue = ha_hue * 256 / 360
# Push responses from controller use standard 0-360 degrees
ELEGANT_HUE_MAX = 256

# Number of zones
MAX_ZONES = 24

# Ping interval in seconds
PING_INTERVAL = 5

# Reconnect settings
RECONNECT_BASE_DELAY = 1
RECONNECT_MAX_DELAY = 60

# Service names
SERVICE_RESET_ZONE_DEFAULTS = "reset_zone_defaults"

# Attribute names
ATTR_ZONE_INDEX = "zone_index"
ATTR_ZONE_TYPE = "zone_type"

# Time sync options
CONF_TIME_SYNC_ENABLED = "time_sync_enabled"
CONF_TIME_SYNC_THRESHOLD = "time_sync_threshold"
DEFAULT_TIME_SYNC_ENABLED = True
DEFAULT_TIME_SYNC_THRESHOLD = 5  # seconds
MIN_TIME_SYNC_THRESHOLD = 2  # minimum meaningful threshold
MAX_TIME_SYNC_THRESHOLD = 60

# Periodic full state poll (get_config)
CONF_POLL_ENABLED = "poll_enabled"
CONF_POLL_INTERVAL = "poll_interval"
DEFAULT_POLL_ENABLED = True
DEFAULT_POLL_INTERVAL = 300  # seconds (5 minutes)
MIN_POLL_INTERVAL = 60  # 1 minute
MAX_POLL_INTERVAL = 86400  # 24 hours

# Debounce get_config after external change
CONF_DEBOUNCE_ENABLED = "debounce_enabled"
CONF_EXTERNAL_CHANGE_DEBOUNCE = "external_change_debounce"
DEFAULT_DEBOUNCE_ENABLED = True
DEFAULT_EXTERNAL_CHANGE_DEBOUNCE = 0.5  # seconds
MIN_EXTERNAL_CHANGE_DEBOUNCE = 0.1
MAX_EXTERNAL_CHANGE_DEBOUNCE = 10.0
EXTERNAL_CHANGE_DEBOUNCE_STEP = 0.1


