"""Config for loading env variables and setting default values."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import structlog
from dotenv import load_dotenv

logger = structlog.get_logger(__name__)

ENV_FILE_PATH = Path.home() / ".haystack-enterprise" / ".env"

# The deepset platform base URL (without a version suffix).
PLATFORM_URL = "https://api.cloud.deepset.ai"

# The API version path appended to the base URL when building requests.
API_VERSION_PATH = "api/v1"

# Matches a trailing version segment like `/api/v1`, `/v1`, `/v2`, ... (case-insensitive),
# optionally followed by a trailing slash.
_VERSION_SUFFIX_RE = re.compile(r"/(?:api/)?v\d+/?$", re.IGNORECASE)


def normalize_base_url(url: str) -> str:
    """Normalize an API URL to a bare base URL without a version suffix.

    Strips a trailing version segment (`/api/v1`, `/v1`, `/v2`, ...) and any trailing slash,
    so both fresh base URLs and legacy full URLs (or pasted URLs including the version) resolve
    to the same base. The SDK appends the version (:data:`API_VERSION_PATH`) when building requests.

    :param url: The API URL to normalize.
    :return: The base URL without a trailing version segment or slash.
    """
    url = _VERSION_SUFFIX_RE.sub("", url)
    return url.rstrip("/")


def load_environment(show_warnings: bool = True) -> bool:
    """Load environment variables using a cascading fallback model.

    1. Load local .env file in current directory if it exists
    2. Load from global ~/.haystack-enterprise/.env to supplement local .env file
    3. Environment variables can override both local and global .env files

    :param show_warnings: Whether to show warnings about missing files/variables
    :return: True if required environment variables were loaded successfully, False otherwise.
    """
    current_path_env = Path.cwd() / ".env"
    local_loaded = current_path_env.is_file() and load_dotenv(current_path_env)
    global_loaded = ENV_FILE_PATH.is_file() and load_dotenv(ENV_FILE_PATH, override=False)

    if local_loaded:
        logger.info(f"Environment variables successfully loaded from local .env file at {current_path_env}.")
    if global_loaded:
        if local_loaded:
            logger.info(f"Loaded global .env file at {ENV_FILE_PATH} to supplement local .env file.")
        else:
            logger.info(f"Environment variables successfully loaded from global .env file at {ENV_FILE_PATH}.")

    if not (local_loaded or global_loaded) and show_warnings:
        logger.warning(
            "No .env files found. Run `haystack-enterprise login` to create a global configuration file. "
            "You can also create a custom local .env file in your project directory."
        )
        return False

    # Check for required environment variables
    required_vars = ["API_KEY", "API_URL", "DEFAULT_WORKSPACE_NAME"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    if missing_vars and show_warnings:
        logger.warning(
            f"Missing required environment variables: {', '.join(missing_vars)}. "
            "Run `haystack-enterprise login` to set up your configuration or set these variables "
            "manually in your .env file."
        )
        return False

    return True


# Load environment variables silently at import time to support CLI commands that depend on .env files.
# Warnings are only shown later in CommonConfig when users don't provide explicit parameters
# and the config values fall back to global defaults.
load_environment(show_warnings=False)

# connection to Haystack Enterprise Platform
API_URL: str = os.getenv("API_URL", PLATFORM_URL)

API_KEY: str = os.getenv("API_KEY", "")

# configuration to use a selected workspace
DEFAULT_WORKSPACE_NAME: str = os.getenv("DEFAULT_WORKSPACE_NAME", "")

ASYNC_CLIENT_TIMEOUT: int = int(os.getenv("ASYNC_CLIENT_TIMEOUT", "300"))


@dataclass
class CommonConfig:
    """Common config for connecting to the Haystack Enterprise Platform.

    Configuration is loaded in the following order of precedence:
    1. Explicit parameters passed to this class
    2. Environment variables
    3. Local .env file in project root
    4. Global .env file in ~/.haystack-enterprise/ (supplements local .env)
    5. Built-in defaults
    """

    api_key: str = ""
    api_url: str = ""
    safe_mode: bool = False

    def __post_init__(self) -> None:
        """Validate config."""
        # Only try loading from environment if user didn't provide explicit parameters)
        if not self.api_key or not self.api_url:
            load_environment(show_warnings=True)
            if not self.api_key:
                self.api_key = os.getenv("API_KEY", "")
            if not self.api_url:
                self.api_url = os.getenv("API_URL", PLATFORM_URL)

        if not self.api_key:
            raise ValueError(
                "API key is required. Either set the API_KEY environment variable or pass api_key parameter. Go to [API Keys](https://cloud.deepset.ai/settings/api-keys) in Haystack Enterprise Platform to get an API key."
            )

        # Normalize to a bare base URL; the version suffix is appended when building requests.
        self.api_url = normalize_base_url(self.api_url)
