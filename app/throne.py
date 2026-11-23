"""Encode complete Xray profiles as Throne Custom Xray Config share links.

Throne 1.4.2's Custom::ParseFromJson accepts the wrapper below. In particular,
splitting `outbounds` into ordinary VLESS links would lose balancers/chains.
"""

from __future__ import annotations

import base64
import copy
import json
import re
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load_configs(body: bytes) -> list[dict[str, Any]]:
    try:
        text = body.decode("utf-8-sig").strip()
        if not text.startswith(("{", "[")):
            compact = re.sub(r"\s+", "", text)
            text = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True).decode("utf-8-sig")
        payload = json.loads(text)
    except (ValueError, UnicodeError):
        raise ValueError("Throne mode requires a full Xray JSON object or array; check the upstream User-Agent") from None

    configs = payload if isinstance(payload, list) else [payload]
    for index, config in enumerate(configs, 1):
        if not isinstance(config, dict) or not isinstance(config.get("outbounds"), list) or not config["outbounds"]:
            raise ValueError(f"Xray profile {index} must have a non-empty outbounds array")
        if any(not isinstance(o, dict) or not isinstance(o.get("protocol"), str) for o in config["outbounds"]):
            raise ValueError(f"Xray profile {index} contains an invalid Xray outbound")
    return configs


def _service_placeholder(config: dict[str, Any]) -> bool:
    credentials = []
    for outbound in config["outbounds"]:
        protocol = outbound["protocol"]
        if protocol in {"freedom", "blackhole", "dns", "loopback"}:
            continue
        if protocol not in {"vless", "vmess"}:
            return False
        settings = outbound.get("settings", {})
        if not isinstance(settings, dict) or not isinstance(settings.get("vnext"), list):
            return False
        for server in settings["vnext"]:
            if not isinstance(server, dict) or not isinstance(server.get("users"), list) or not server["users"]:
                return False
            for user in server["users"]:
                credential = user.get("id") if isinstance(user, dict) else None
                if not isinstance(credential, str):
                    return False
                credentials.append(credential.replace("-", ""))
    return bool(credentials) and all(len(c) == 32 and set(c) == {"0"} for c in credentials)


def _bridge_config(config: dict[str, Any], index: int) -> dict[str, Any]:
    result = copy.deepcopy(config)
    # Throne owns the listener and injects its authenticated SOCKS bridge.
    inbounds = result.pop("inbounds", [])
    if not isinstance(inbounds, list) or any(not isinstance(i, dict) for i in inbounds):
        raise ValueError(f"Xray profile {index} has invalid inbounds")
    old_tags = {i["tag"] for i in inbounds if isinstance(i.get("tag"), str)}
    user_tags = {i["tag"] for i in inbounds if i.get("protocol") in {"socks", "http", "mixed"} and isinstance(i.get("tag"), str)}
    routing = result.get("routing", {})
    if not isinstance(routing, dict) or not isinstance(routing.get("rules", []), list):
        raise ValueError(f"Xray profile {index} has invalid routing rules")
    for rule in routing.get("rules", []):
        if not isinstance(rule, dict):
            raise ValueError(f"Xray profile {index} has an invalid routing rule")
        tags = rule.get("inboundTag", [])
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise ValueError(f"Xray profile {index} has invalid inboundTag routing")
        referenced = old_tags.intersection(tags)
        if not referenced:
            continue
        # Do not silently broaden rules which distinguish HTTP from SOCKS,
        # or convert a special DNS/transparent inbound to general user traffic.
        unnamed_user = any(i.get("protocol") in {"socks", "http", "mixed"} and not i.get("tag") for i in inbounds)
        if referenced != user_tags or unnamed_user:
            raise ValueError(f"Xray profile {index} routes individual inbounds and cannot use one Throne bridge")
        rule["inboundTag"] = list(dict.fromkeys("throne-bridge" if t in user_tags else t for t in tags))
    return result


def convert_xray_subscription(body: bytes) -> tuple[str, int]:
    """Return a raw URI list and count of filtered informational profiles.

    Names and credentials stay in each full profile. Exact profiles can be
    deduplicated, but shared addresses alone must not collapse different routes.
    No partial output is returned if any profile is unsupported.
    """
    links = []
    filtered = 0
    for index, config in enumerate(_load_configs(body), 1):
        if _service_placeholder(config):
            filtered += 1
            continue
        full = _bridge_config(config, index)
        name = next((config[k].strip() for k in ("remarks", "name", "tag") if isinstance(config.get(k), str) and config[k].strip()), f"Xray {index}")
        wrapper = {"type": "custom", "subtype": "xrayfullconfig", "name": name, "config": _json(full)}
        encoded = base64.urlsafe_b64encode(_json(wrapper).encode("utf-8")).decode("ascii").rstrip("=")
        links.append("json://#" + encoded)
    return "".join(link + "\n" for link in links), filtered
