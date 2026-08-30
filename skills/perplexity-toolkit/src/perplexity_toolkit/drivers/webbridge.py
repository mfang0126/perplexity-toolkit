"""Kimi WebBridge driver implementation."""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from .base import BrowserDriver


logger = logging.getLogger(__name__)


def _truncate(value, limit: int = 300) -> str:
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "..."


class WebBridgeDriver(BrowserDriver):
    """Browser driver using Kimi WebBridge daemon (localhost:10086)."""

    def __init__(self, url: str = "http://127.0.0.1:10086/command",
                 session: str = "perplexity-search"):
        self.url = url
        self.session = session

    def _send(self, action: str, args: Optional[dict] = None) -> dict:
        payload = {"action": action, "session": self.session}
        if args:
            payload["args"] = args
        data = json.dumps(payload).encode("utf-8")
        logger.debug("WebBridge -> %s action=%s session=%s args=%s",
                     self.url, action, self.session, _truncate(args))
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    logger.error("WebBridge HTTP %s from %s (action=%s)",
                                 resp.status, self.url, action)
                    return {
                        "error": (
                            f"WebBridge HTTP {resp.status} from {self.url}"
                        )
                    }
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("error"):
                    logger.warning("WebBridge action=%s returned error: %s",
                                   action, data["error"])
                else:
                    logger.debug("WebBridge <- action=%s ok", action)
                return data
        except urllib.error.HTTPError as e:
            logger.error("WebBridge HTTP %s from %s (action=%s): %s",
                         e.code, self.url, action, e.reason)
            return {"error": f"WebBridge HTTP {e.code} from {self.url}: {e.reason}"}
        except urllib.error.URLError as e:
            logger.error(
                "WebBridge connection failed (action=%s) — is kimi-webbridge "
                "running on %s? (%s)",
                action, self.url, e.reason,
            )
            return {
                "error": (
                    f"WebBridge connection failed — is kimi-webbridge "
                    f"running on {self.url}? ({e.reason})"
                )
            }
        except TimeoutError as e:
            logger.error(
                "WebBridge request timed out (action=%s) on %s — check the "
                "daemon and increase the request timeout if needed: %s",
                action, self.url, e,
            )
            return {"error": f"WebBridge request timed out: {e}"}
        except Exception as e:
            logger.error("WebBridge unexpected error (action=%s): %s", action, e)
            return {"error": f"WebBridge error: {e}"}

    def navigate(self, url: str, new_tab: bool = True,
                 group_title: str = "") -> dict:
        args = {"url": url, "newTab": new_tab}
        if group_title:
            args["group_title"] = group_title
        result = self._send("navigate", args)
        # If tab was closed/stale, retry with new_tab
        if not result.get("ok") and not new_tab:
            err = result.get("error", {}).get("message", "")
            if "No tab" in err or "Bad Gateway" in err:
                logger.warning("Tab stale, opening new tab")
                args["newTab"] = True
                result = self._send("navigate", args)
        return result

    def snapshot(self) -> dict:
        return self._send("snapshot", {})

    def click(self, selector: str) -> dict:
        return self._send("click", {"selector": selector})

    def fill(self, selector: str, value: str) -> dict:
        return self._send("fill", {"selector": selector, "value": value})

    def evaluate(self, code: str) -> Any:
        resp = self._send("evaluate", {"code": code})
        val = resp.get("data", {}).get("value", "")
        if isinstance(val, str):
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return val
        return val

    def screenshot(self, path: Optional[str] = None) -> dict:
        args = {"format": "png"}
        if path:
            args["path"] = path
        return self._send("screenshot", args)

    def close(self) -> dict:
        return self._send("close_session", {})

    def cdp(self, method: str, params: Optional[dict] = None) -> dict:
        args = {"method": method}
        if params:
            args["params"] = params
        return self._send("cdp", args)
