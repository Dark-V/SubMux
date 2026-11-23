import base64
import copy
import json
import unittest

from app.throne import convert_xray_subscription


ZERO_ID = "00000000-0000-0000-0000-000000000000"
REAL_ID = "11111111-2222-4333-8444-555555555555"


def xray_proxy(tag="proxy", credential=REAL_ID):
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": f"{tag}.example.com",
                "port": 443,
                "users": [{"id": credential, "encryption": "none"}],
            }],
        },
        "streamSettings": {
            "network": "grpc",
            "security": "tls",
            "tlsSettings": {"serverName": f"{tag}.example.com", "alpn": ["h2"]},
            "grpcSettings": {"serviceName": "example.Api", "multiMode": False},
        },
    }


def profile(name="🇵🇱 Польша", credential=REAL_ID):
    return {
        "remarks": name,
        "inbounds": [
            {"tag": "socks-in", "protocol": "socks", "listen": "127.0.0.1", "port": 1080},
            {"tag": "http-in", "protocol": "http", "listen": "127.0.0.1", "port": 8080},
        ],
        "outbounds": [
            xray_proxy(credential=credential),
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
    }


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def decode_links(text):
    wrappers = []
    for line in text.splitlines():
        if not line.startswith("json://#"):
            raise AssertionError(f"Unexpected URI scheme: {line.split(':', 1)[0]}")
        payload = line.removeprefix("json://#")
        wrapper = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if wrapper["type"] != "custom" or wrapper["subtype"] != "xrayfullconfig":
            raise AssertionError("Expected a full Xray custom profile")
        wrappers.append((wrapper["name"], json.loads(wrapper["config"])))
    return wrappers


class ThroneConversionTests(unittest.TestCase):
    def test_full_profile_preserves_chains_balancers_and_routing(self):
        config = profile()
        config["outbounds"].insert(1, xray_proxy("relay"))
        config["outbounds"][0]["streamSettings"]["sockopt"] = {"dialerProxy": "relay"}
        config["outbounds"][0]["proxySettings"] = {"tag": "relay", "transportLayer": True}
        config["dns"] = {
            "servers": ["https://dns.example.com/dns-query"],
            "hosts": {"private.example.com": "192.0.2.8"},
            "queryStrategy": "UseIPv4",
        }
        config["routing"] = {
            "domainStrategy": "IPIfNonMatch",
            "balancers": [{
                "tag": "best",
                "selector": ["proxy", "relay"],
                "fallbackTag": "relay",
                "strategy": {"type": "leastLoad", "settings": {"maxRTT": "3s"}},
            }],
            "rules": [
                {"type": "field", "domain": ["full:direct.example.com"], "outboundTag": "direct"},
                {"type": "field", "network": "tcp,udp", "balancerTag": "best"},
            ],
        }
        config["observatory"] = {"subjectSelector": ["proxy"], "probeURL": "https://probe.example.com/"}
        config["burstObservatory"] = {"subjectSelector": ["relay"], "pingConfig": {"interval": "1m"}}
        config["policy"] = {"levels": {"0": {"connIdle": 300}}, "system": {"statsOutboundUplink": True}}
        config["stats"] = {}

        text, filtered = convert_xray_subscription(encode(config))

        expected = copy.deepcopy(config)
        expected.pop("inbounds")
        self.assertEqual(decode_links(text), [("🇵🇱 Польша", expected)])
        self.assertEqual(filtered, 0)
        self.assertTrue(text.endswith("\n"))

    def test_array_retains_order_unicode_names_and_separate_profiles(self):
        first = profile("🇸🇪 Швеция / резерв")
        second = profile("日本 — 東京")
        second["routing"] = {"rules": [{"type": "field", "network": "tcp", "outboundTag": "direct"}]}

        text, filtered = convert_xray_subscription(encode([first, second]))
        decoded = decode_links(text)

        self.assertEqual([name for name, _ in decoded], [first["remarks"], second["remarks"]])
        self.assertEqual(decoded[1][1]["routing"], second["routing"])
        self.assertEqual(len(decoded), 2)
        self.assertEqual(filtered, 0)

    def test_base64_single_and_array_match_plain_json(self):
        for payload in [profile(), [profile(), profile("Second")]]:
            with self.subTest(array=isinstance(payload, list)):
                raw = encode(payload)
                encoded = base64.b64encode(raw).rstrip(b"=")
                wrapped = b"\n".join(encoded[i:i + 61] for i in range(0, len(encoded), 61))
                self.assertEqual(convert_xray_subscription(wrapped), convert_xray_subscription(raw))

    def test_utf8_bom_does_not_change_result(self):
        raw = encode(profile())
        self.assertEqual(convert_xray_subscription(b"\xef\xbb\xbf" + raw), convert_xray_subscription(raw))

    def test_name_uses_name_tag_then_stable_index_fallback(self):
        configs = [profile(" "), profile(""), profile("")]
        configs[0]["name"] = "  Name fallback  "
        configs[1]["tag"] = "tag-fallback"

        text, _ = convert_xray_subscription(encode(configs))

        self.assertEqual([name for name, _ in decode_links(text)], ["Name fallback", "tag-fallback", "Xray 3"])

    def test_invalid_formats_are_rejected(self):
        invalid_bodies = [
            b"{bad-json}",
            b"vless://example-user@proxy.example.com:443#Example\n",
            base64.b64encode(b"vless://example-user@proxy.example.com:443#Example\n"),
            b"\xff\xfe",
            encode({"outbounds": [{"type": "vless", "server": "proxy.example.com", "server_port": 443}]}),
            encode({"outbounds": []}),
            encode({"outbounds": ["vless"]}),
            encode({"outbounds": [{"protocol": 42}]}),
            encode([profile(), "not-a-profile"]),
        ]
        for body in invalid_bodies:
            with self.subTest(body_kind=body[:40]):
                with self.assertRaises(ValueError):
                    convert_xray_subscription(body)

    def test_all_zero_placeholder_is_filtered_without_losing_real_profile(self):
        placeholder = profile("Choose a server below", ZERO_ID)
        placeholder["outbounds"].append({"tag": "dns", "protocol": "dns"})

        text, filtered = convert_xray_subscription(encode([placeholder, profile("Real server")]))

        self.assertEqual(filtered, 1)
        self.assertEqual([name for name, _ in decode_links(text)], ["Real server"])

    def test_only_placeholders_produce_empty_subscription_and_count(self):
        configs = [profile("Notice one", ZERO_ID), profile("Notice two", ZERO_ID)]
        configs[1]["outbounds"][0]["protocol"] = "vmess"

        self.assertEqual(convert_xray_subscription(encode(configs)), ("", 2))

    def test_mixed_zero_and_real_credentials_are_kept_intact(self):
        config = profile("Mixed", ZERO_ID)
        config["outbounds"].insert(1, xray_proxy("real", REAL_ID))

        text, filtered = convert_xray_subscription(encode(config))

        self.assertEqual(filtered, 0)
        self.assertEqual(decode_links(text)[0][1]["outbounds"], config["outbounds"])

    def test_other_proxy_protocol_prevents_placeholder_filtering(self):
        config = profile("Trojan fallback", ZERO_ID)
        config["outbounds"].insert(1, {
            "tag": "trojan",
            "protocol": "trojan",
            "settings": {"servers": [{"address": "trojan.example.com", "port": 443, "password": "example-password"}]},
        })

        text, filtered = convert_xray_subscription(encode(config))

        self.assertEqual(filtered, 0)
        self.assertEqual(len(decode_links(text)), 1)

    def test_invalid_later_config_rejects_whole_subscription(self):
        invalid = profile("Broken second profile")
        invalid["routing"] = {"rules": [{"inboundTag": "socks-in", "outboundTag": "proxy"}]}

        with self.assertRaisesRegex(ValueError, "profile 2"):
            convert_xray_subscription(encode([profile(), invalid]))

    def test_all_user_inbound_tags_map_to_one_bridge(self):
        config = profile()
        config["routing"] = {"rules": [{
            "type": "field",
            "inboundTag": ["http-in", "socks-in", "http-in", "independent-tag"],
            "domain": ["full:service.example.com"],
            "outboundTag": "proxy",
        }]}

        text, _ = convert_xray_subscription(encode(config))
        full = decode_links(text)[0][1]

        self.assertNotIn("inbounds", full)
        self.assertEqual(full["routing"]["rules"], [{
            "type": "field",
            "inboundTag": ["throne-bridge", "independent-tag"],
            "domain": ["full:service.example.com"],
            "outboundTag": "proxy",
        }])

    def test_routes_distinguishing_user_inbounds_are_rejected(self):
        for tags in [["socks-in"], ["http-in"]]:
            with self.subTest(tags=tags):
                config = profile()
                config["routing"] = {"rules": [{"inboundTag": tags, "outboundTag": "direct"}]}
                with self.assertRaisesRegex(ValueError, "routes individual inbounds"):
                    convert_xray_subscription(encode(config))

    def test_tagged_route_cannot_capture_traffic_from_unnamed_user_inbound(self):
        config = profile()
        config["inbounds"][1].pop("tag")
        config["routing"] = {"rules": [{"inboundTag": ["socks-in"], "outboundTag": "direct"}]}

        with self.assertRaisesRegex(ValueError, "routes individual inbounds"):
            convert_xray_subscription(encode(config))

    def test_special_inbound_rule_cannot_be_broadened_to_user_traffic(self):
        config = profile()
        config["inbounds"].append({"tag": "dns-in", "protocol": "dokodemo-door", "port": 5353})
        for tags in [["dns-in"], ["socks-in", "http-in", "dns-in"]]:
            with self.subTest(tags=tags):
                config["routing"] = {"rules": [{"inboundTag": tags, "outboundTag": "direct"}]}
                with self.assertRaisesRegex(ValueError, "routes individual inbounds"):
                    convert_xray_subscription(encode(config))


if __name__ == "__main__":
    unittest.main()
