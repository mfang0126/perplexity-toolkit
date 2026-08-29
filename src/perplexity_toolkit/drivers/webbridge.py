"""Kimi WebBridge driver implementation."""

import json
import urllib.error
import urllib.request
from typing import Any, Optional

from .base import BrowserDriver


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
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    return {
                        "error": (
                            f"WebBridge HTTP {resp.status} from {self.url}"
                        )
                    }
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": f"WebBridge HTTP {e.code} from {self.url}: {e.reason}"}
        except urllib.error.URLError as e:
            return {
                "error": (
                    f"WebBridge connection failed — is kimi-webbridge "
                    f"running on {self.url}? ({e.reason})"
                )
            }
        except TimeoutError as e:
            return {"error": f"WebBridge request timed out: {e}"}
        except Exception as e:
            return {"error": f"WebBridge error: {e}"}

    def navigate(self, url: str, new_tab: bool = True,
                 group_title: str = "") -> dict:
        args = {"url": url, "newTab": new_tab}
        if group_title:
            args["group_title"] = group_title
        return self._send("navigate", args)

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
