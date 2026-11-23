import base64
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


_data = tempfile.TemporaryDirectory(prefix="submux-tests-")
with patch.dict(os.environ, {"DATA_DIR": _data.name, "ADMIN_TOKEN": "test-admin", "UPSTREAM_URL": ""}):
    from app import main


def tearDownModule():
    _data.cleanup()


PROFILE = {
    "remarks": "Sweden",
    "inbounds": [{"tag": "socks", "protocol": "socks", "port": 1080}],
    "outbounds": [
        {"tag": "proxy", "protocol": "vless", "settings": {"vnext": [{
            "address": "one.example", "port": 443,
            "users": [{"id": "11111111-1111-4111-8111-111111111111"}],
        }]}},
        {"tag": "direct", "protocol": "freedom"},
    ],
    "routing": {"rules": [{"type": "field", "network": "tcp,udp", "outboundTag": "proxy"}]},
}
URI_BODY = base64.b64encode(b"vless://user-a@normal.example:443?security=tls#Normal\n")


class SubscriptionApiTests(unittest.TestCase):
    def setUp(self):
        with main.db() as con:
            con.execute("DELETE FROM tokens")
            con.execute("DELETE FROM subscriptions")
        main._SOURCE_LOCKS.clear()
        main._BUNDLE_LOCKS.clear()
        main._PUBLIC_REFRESH_WINDOWS.clear()
        self.responses = {"/xray": json.dumps([PROFILE]).encode(), "/uris": URI_BODY}
        self.requests = []
        real_client = httpx.AsyncClient

        def upstream(request):
            self.requests.append(request)
            return httpx.Response(200, content=self.responses[request.url.path], headers={
                "content-type": "application/json", "subscription-userinfo": "upload=1; download=2; total=0",
            })

        self.transport_patch = patch.object(main.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(upstream), trust_env=False,
        ))
        self.transport_patch.start()
        self.burst_patch = patch.object(main, "BURST_REFRESH_REQUESTS", 1000)
        self.burst_patch.start()
        self.admin = TestClient(main.app, base_url=f"http://testserver:{main.ADMIN_PORT}")
        self.public = TestClient(main.app, base_url=f"http://testserver:{main.PUBLIC_PORT}")
        self.admin.cookies.set(main.COOKIE_NAME, main.ADMIN_COOKIE_VALUE)

    def tearDown(self):
        self.admin.close()
        self.public.close()
        self.transport_patch.stop()
        self.burst_patch.stop()

    def source(self, name="xray", format="throne", **extra):
        payload = {"name": name, "url": f"https://upstream.example/{name}", "format": format,
                   "user_agent": "Happ/3.6.0/Android", "hwid": "test-device", **extra}
        res = self.admin.post("/api/subscriptions", json=payload)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def token(self, ids=None):
        res = self.admin.post("/api/tokens", json={"label": "test", "scope_all": ids is None, "subscription_ids": ids or []})
        self.assertEqual(res.status_code, 200)
        return res.json()["token"]

    def test_mixed_bundle_named_raw_and_cache(self):
        source = self.source()
        self.source("uris", "auto")
        token = self.token()
        first = self.public.get(f"/{token}/")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["x-submux-nodes"], "2")
        self.assertEqual(first.headers["x-submux-cache"], "REFRESH")
        self.assertEqual(len(first.text.splitlines()), 2)
        self.assertTrue(first.text.startswith("json://#"))
        self.assertIn("vless://user-a@normal.example", first.text)
        encoded = first.text.splitlines()[0].partition("#")[2]
        wrapper = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(wrapper["subtype"], "xrayfullconfig")
        self.assertEqual(json.loads(wrapper["config"])["routing"], PROFILE["routing"])
        self.assertTrue(all(r.headers["x-hwid"] == "test-device" for r in self.requests))
        self.assertTrue(all(r.headers["user-agent"] == "Happ/3.6.0/Android" for r in self.requests))
        count = len(self.requests)
        second = self.public.get(f"/{token}/")
        self.assertEqual(second.content, first.content)
        self.assertEqual(second.headers["x-submux-cache"], "HIT")
        self.assertEqual(len(self.requests), count)
        named = self.public.get(f"/{token}/sub/xray")
        self.assertEqual(named.text, first.text.splitlines()[0] + "\n")
        self.assertEqual(named.headers["x-submux-nodes"], "1")
        self.assertEqual(named.headers["subscription-userinfo"], "upload=1; download=2; total=0")
        raw = self.admin.get(f"/api/subscriptions/{source['id']}/raw")
        self.assertEqual(raw.content, self.responses["/xray"])
        self.assertEqual(self.public.get(f"/{token}/sub/uris").content, URI_BODY)
        stats = self.admin.get("/api/subscriptions").json()
        self.assertEqual(next(s for s in stats if s["name"] == "xray")["proxy_count"], 1)

    def test_refresh_changes_profiles_and_invalid_json_keeps_last_good_cache(self):
        source = self.source()
        token = self.token()
        first = self.public.get(f"/{token}/")
        changed = copy.deepcopy(PROFILE)
        changed["outbounds"][0]["settings"]["vnext"][0]["address"] = "new.example"
        self.responses["/xray"] = json.dumps([changed]).encode()
        refreshed = self.admin.post(f"/api/subscriptions/{source['id']}/refresh")
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(refreshed.json()["rebuilt_bundles"], 1)
        second = self.public.get(f"/{token}/")
        self.assertNotEqual(second.content, first.content)
        self.responses["/xray"] = URI_BODY
        stale = self.public.get(f"/{token}/?forceUpdate=1")
        self.assertEqual(stale.status_code, 200, stale.text)
        self.assertEqual(stale.content, second.content)
        self.assertEqual(stale.headers["x-submux-stale"], "xray")
        self.assertEqual(self.public.get(f"/{token}/sub/xray?force=1").content, second.content)
        # RAW remains an inspector even when the selected adapter rejects the body.
        self.assertEqual(self.admin.get(f"/api/subscriptions/{source['id']}/raw").content, URI_BODY)

    def test_invalid_payload_without_cache_is_clear_error(self):
        self.source()
        self.responses["/xray"] = URI_BODY
        token = self.token()
        for path in (f"/{token}/", f"/{token}/sub/xray"):
            response = self.public.get(path)
            self.assertEqual(response.status_code, 502)
            self.assertIn("full Xray JSON", response.text)
            self.assertNotIn("user-a", response.text)

    def test_existing_uri_merge_and_access_scope_are_unchanged(self):
        source = self.source("uris", "auto")
        self.source()
        token = self.token([source["id"]])
        response = self.public.get(f"/{token}/")
        self.assertEqual(response.content, URI_BODY)
        self.assertEqual(self.public.get(f"/{token}/sub/xray").status_code, 404)
        self.assertEqual(self.public.get("/invalid-token/").status_code, 403)
        self.assertEqual(self.public.get("/api/subscriptions").status_code, 404)

    def test_same_address_with_different_routes_is_not_deduplicated(self):
        changed = copy.deepcopy(PROFILE)
        changed["routing"]["rules"][0]["outboundTag"] = "direct"
        self.responses["/xray"] = json.dumps([PROFILE, PROFILE, changed]).encode()
        self.source()
        response = self.public.get(f"/{self.token()}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-submux-nodes"], "2")


if __name__ == "__main__":
    unittest.main()
