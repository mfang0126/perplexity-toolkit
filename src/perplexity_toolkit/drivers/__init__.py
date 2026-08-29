"""Browser driver implementations."""

from ..config import Config
from .base import BrowserDriver
from .webbridge import WebBridgeDriver

# Registry of available driver backends. Backend name -> driver class.
DRIVER_REGISTRY = {
    "webbridge": WebBridgeDriver,
}


def create_driver(config: Config, suffix: str = "search") -> BrowserDriver:
    """Create a driver instance for the configured backend.

    Falls back to the default 'webbridge' backend for unknown names
    (backward compatible with the pre-registry behavior).
    """
    cls = DRIVER_REGISTRY.get(config.driver_backend, WebBridgeDriver)
    return cls(
        url=config.webbridge_url,
        session=config.make_session(suffix),
    )


__all__ = ["BrowserDriver", "WebBridgeDriver", "DRIVER_REGISTRY", "create_driver"]