"""Browser driver implementations."""

import logging

from ..config import Config
from .base import BrowserDriver
from .webbridge import WebBridgeDriver

logger = logging.getLogger(__name__)

# Registry of available driver backends. Backend name -> driver class.
# This is the authoritative list of what is actually implemented: the CLI
# derives its --backend choices from it, so adding an entry here is all that is
# needed to expose a new backend. Do not advertise a backend anywhere else
# before it appears here.
DRIVER_REGISTRY = {
    "webbridge": WebBridgeDriver,
}


def create_driver(config: Config, suffix: str = "search") -> BrowserDriver:
    """Create a driver instance for the configured backend.

    Falls back to the default 'webbridge' backend for unknown names
    (backward compatible with the pre-registry behavior), but says so. A silent
    fallback lets a caller believe it selected a backend that does not exist —
    for example via PERPLEXITY_DRIVER, which bypasses the CLI's choices check.
    """
    backend = config.driver_backend
    if backend not in DRIVER_REGISTRY:
        logger.warning(
            "Unknown driver backend %r; falling back to 'webbridge'. "
            "Implemented backends: %s",
            backend, ", ".join(sorted(DRIVER_REGISTRY)),
        )
    cls = DRIVER_REGISTRY.get(backend, WebBridgeDriver)
    return cls(
        url=config.webbridge_url,
        session=config.make_session(suffix),
    )


__all__ = ["BrowserDriver", "WebBridgeDriver", "DRIVER_REGISTRY", "create_driver"]