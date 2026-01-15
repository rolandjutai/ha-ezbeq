"""Constants for the ezbeq Profile Loader integration."""

DOMAIN = "ezbeq"
DEFAULT_PORT = 8080
DEFAULT_NAME = "EzBEQ"
STATE_UNLOADED = "unloaded"

# Sensor data
CURRENT_PROFILE = "current_profile"
DEVICES = "devices"

# Manual-load entity IDs
SENSOR_TMDB_IDS = "sensor.ezbeq_candidate_tmdb_ids"
SENSOR_TITLES = "sensor.ezbeq_candidate_titles"
SENSOR_DETAILS = "sensor.ezbeq_candidate_details"
SENSOR_STATUS = "sensor.ezbeq_candidate_status"
SWITCH_SEARCH_ENABLED = "switch.ezbeq_candidate_search_enabled"

# Select entity ID (native SelectEntity)
SELECT_CANDIDATE = "select.ezbeq_candidate"

# Dispatcher signals
SIGNAL_UPDATE_SELECT = "ezbeq_update_select"
