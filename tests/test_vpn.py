import unittest

from installer.vpn import PROVIDERS, gluetun_env

UP = ("/bin/sh -c 'wget -O- --retry-connrefused --post-data \"json={\\\"listen_port\\\":{{PORTS}}}\" "
      "http://127.0.0.1:8080/api/v2/app/setPreferences 2>&1'")


class GluetunEnvTest(unittest.TestCase):
    def test_proton_port_forwarding(self):
        env = gluetun_env("protonvpn", {"WIREGUARD_PRIVATE_KEY": "k"}, "Europe/Paris")
        self.assertEqual(env["VPN_SERVICE_PROVIDER"], "protonvpn")
        self.assertEqual(env["VPN_TYPE"], "wireguard")
        self.assertEqual(env["VPN_PORT_FORWARDING"], "on")
        self.assertEqual(env["PORT_FORWARD_ONLY"], "on")
        self.assertEqual(env["VPN_PORT_FORWARDING_UP_COMMAND"], UP)
        self.assertEqual(env["WIREGUARD_PRIVATE_KEY"], "k")
        self.assertEqual(env["TZ"], "Europe/Paris")

    def test_nord_without_port_forwarding(self):
        env = gluetun_env("nordvpn", {"WIREGUARD_PRIVATE_KEY": "k"}, "UTC")
        self.assertFalse([k for k in env if k.startswith("VPN_PORT_FORWARDING")])
        self.assertNotIn("PORT_FORWARD_ONLY", env)

    def test_pia_uses_openvpn(self):
        env = gluetun_env("private internet access", {"OPENVPN_USER": "u", "OPENVPN_PASSWORD": "p"}, "UTC")
        self.assertEqual(env["VPN_TYPE"], "openvpn")
        self.assertTrue(PROVIDERS["private internet access"].port_forwarding)

    def test_missing_field(self):
        with self.assertRaises(ValueError):
            gluetun_env("mullvad", {"WIREGUARD_PRIVATE_KEY": "k"}, "UTC")

    def test_table(self):
        self.assertEqual(set(PROVIDERS), {"protonvpn", "private internet access", "mullvad", "nordvpn",
                                          "surfshark", "windscribe"})


if __name__ == "__main__":
    unittest.main()
