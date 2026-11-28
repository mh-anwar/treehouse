"""net-guard (sharedbox-netguard): the decision logic (simulated network states -> actions, the reboot rate limit,
no reboot during dpkg, a new boot resets an outage), parsing (wpa blocks, netplan splitting, sysfs links, /proc
package locks), the telemetry hook, the `box tune` checks, and the shipped units/rules. Stdlib only (PyYAML if
present), no root, no network.
    python3 -m unittest tests.test_netguard -v"""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbtelemetry as T  # noqa: E402


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


ng = load("sbnetguard_test", os.path.join(LIB, "sharedbox-netguard"))
tune = load("sbtune_netguard", os.path.join(LIB, "sharedbox-tune"))

CFG = {"on": True, "t1": 120.0, "t2": 300.0, "t3": 1200.0, "reboot_every": 6 * 3600.0}


def obs(path_ok=True, ts="ok", **kw):
    o = {"path_ok": path_ok, "ts": ts, "wifi_expected": False, "wifi_ok": False, "dongle_state": "",
         "pkg_busy": False, "boot_id": "b1"}
    o.update(kw)
    return o


def names(acts):
    return [a for a, _ in acts]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Decide(unittest.TestCase):
    def run_outage(self, seconds, o=None, st=None, start=10_000):
        """Ticks every 30 s from `start` for `seconds` with the network down; returns [(t, actions)], state."""
        st = dict(st or {"boot_id": "b1"})
        out = []
        for t in range(0, seconds + 1, 30):
            acts, st = ng.decide(o or obs(path_ok=False, ts="down"), st, start + t, CFG)
            if acts:
                out.append((t, names(acts)))
        return out, st

    def test_healthy_does_nothing(self):
        acts, st = ng.decide(obs(), {"boot_id": "b1"}, 1000, CFG)
        self.assertEqual(acts, [])
        self.assertIsNone(st["down_since"])

    def test_escalation_timeline(self):
        out, st = self.run_outage(1200)
        self.assertEqual(out, [(120, ["reconfigure"]), (300, ["restart"]), (1200, ["reboot"])])
        self.assertEqual(st["last_reboot"]["ts"], 10_000 + 1200)
        self.assertEqual(st["steps"], ["reconfigure", "restart"])

    def test_restart_repeats_every_15_min_while_down(self):
        cfg = dict(CFG, t3=0)   # never reboot: see the repeats
        st, got = {"boot_id": "b1"}, []
        for t in range(0, 2400, 30):
            acts, st = ng.decide(obs(path_ok=False, ts="down"), st, 10_000 + t, cfg)
            got += [(t, a) for a in names(acts)]
        self.assertEqual(got, [(120, "reconfigure"), (300, "restart"), (1200, "restart"), (2100, "restart")])

    def test_short_blip_does_nothing_and_recovers_quietly(self):
        st = {"boot_id": "b1"}
        acts, st = ng.decide(obs(path_ok=False, ts="down"), st, 1000, CFG)
        self.assertEqual(acts, [])
        acts, st = ng.decide(obs(), st, 1060, CFG)
        self.assertEqual(names(acts), ["recovered"])
        self.assertIsNone(st["down_since"])

    def test_recovered_after_actions_records_the_outage(self):
        _, st = self.run_outage(400)
        acts, st = ng.decide(obs(), st, 10_000 + 430, CFG)
        self.assertEqual(names(acts), ["recovered"])
        self.assertEqual(st["last_outage"]["steps"], ["reconfigure", "restart"])
        self.assertEqual(st["steps"], [])

    def test_no_reboot_during_dpkg_and_said_once(self):
        out, st = self.run_outage(1500, obs(path_ok=False, ts="down", pkg_busy=True))
        flat = [a for _, acts in out for a in acts]
        self.assertNotIn("reboot", flat)
        self.assertEqual(flat.count("reboot_blocked"), 1)
        self.assertIn("apt/dpkg", st["blocked"])
        # dpkg finished: the next tick reboots
        acts, st = ng.decide(obs(path_ok=False, ts="down"), st, 10_000 + 1530, CFG)
        self.assertIn("reboot", names(acts))

    def test_reboot_rate_limit(self):
        st = {"boot_id": "b1", "last_reboot": {"ts": 10_000 - 2 * 3600}}
        out, st = self.run_outage(1500, st=st)
        flat = [a for _, acts in out for a in acts]
        self.assertNotIn("reboot", flat)
        self.assertIn("reboot_blocked", flat)
        self.assertIn("already rebooted", st["blocked"])
        st2 = {"boot_id": "b1", "last_reboot": {"ts": 10_000 - 7 * 3600}}
        out, _ = self.run_outage(1200, st=st2)
        self.assertIn("reboot", out[-1][1])

    def test_no_reboot_when_the_router_answers(self):
        # LAN fine, internet/Tailscale down: rebooting this box can't fix the ISP
        out, st = self.run_outage(1500, obs(path_ok=True, ts="down"))
        flat = [a for _, acts in out for a in acts]
        self.assertEqual(flat.count("reconfigure"), 1)
        self.assertIn("restart", flat)
        self.assertNotIn("reboot", flat)
        self.assertIn("router answers", st["blocked"])

    def test_reboot_off(self):
        out, _ = ng.decide(obs(path_ok=False, ts="down"), {"boot_id": "b1", "down_since": 1}, 99_999,
                           dict(CFG, t3=0))
        self.assertNotIn("reboot", names(out))

    def test_new_boot_starts_a_new_outage(self):
        # down since long before a reboot: the new boot must not reboot again right away
        st = {"boot_id": "old", "down_since": 1000, "steps": ["reconfigure", "restart"], "last_restart": 1300}
        acts, st = ng.decide(obs(path_ok=False, ts="down", boot_id="b2"), st, 5000, CFG)
        self.assertEqual(acts, [])
        self.assertEqual((st["down_since"], st["steps"], st["boot_id"]), (5000, [], "b2"))

    def test_tailscale_logged_out_isnt_an_outage(self):
        acts, st = ng.decide(obs(path_ok=True, ts="n/a"), {"boot_id": "b1"}, 1000, CFG)
        self.assertEqual((acts, st["down_since"]), ([], None))

    def test_dongle_without_interface_gets_usb_reset_with_the_restart(self):
        out, _ = self.run_outage(330, obs(path_ok=False, ts="down", dongle_state="no_netdev"))
        self.assertEqual(out, [(120, ["reconfigure"]), (300, ["restart", "usb_reset"])])

    def test_dongle_gone_from_usb_gets_no_usb_reset(self):
        out, _ = self.run_outage(330, obs(path_ok=False, ts="down", dongle_state="gone"))
        self.assertEqual(out, [(120, ["reconfigure"]), (300, ["restart"])])

    def test_dead_wifi_beside_working_cable_is_fixed_in_the_background(self):
        o = obs(wifi_expected=True, wifi_ok=False)
        st, got = {"boot_id": "b1"}, []
        for t in range(0, 4000, 30):
            acts, st = ng.decide(o, st, 10_000 + t, CFG)
            got += [(t, a) for a in names(acts)]
        self.assertEqual(got, [(300, "wifi_fix"), (2100, "wifi_fix"), (3900, "wifi_fix")])  # then every 30 min
        acts, st = ng.decide(obs(wifi_expected=True, wifi_ok=True), st, 20_000, CFG)
        self.assertIsNone(st["wifi_down_since"])

    def test_threshold_zero_turns_a_step_off(self):
        st, got = {"boot_id": "b1"}, []
        for t in range(0, 600, 30):
            acts, st = ng.decide(obs(path_ok=False, ts="down"), st, 10_000 + t, dict(CFG, t1=0, t3=0))
            got += names(acts)
        self.assertEqual(got, ["restart"])

    def test_config_reads_thresholds(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "c.conf")
            write(conf, "NET_GUARD_RECONFIGURE_MIN=1\nNET_GUARD_REBOOT_MIN=0.5\nNET_GUARD_REBOOT_EVERY_HOURS=x\n")
            old = ng.sb.load_config
            ng.sb.load_config = lambda path=conf: old(conf)
            try:
                c = ng.config()
            finally:
                ng.sb.load_config = old
        self.assertEqual((c["t1"], c["t2"], c["t3"], c["reboot_every"], c["on"]), (60, 300, 30, 6 * 3600, True))


class Parsing(unittest.TestCase):
    WPA = ('ctrl_interface=/run/wpa_supplicant\n\nnetwork={\n  ssid=P"Home"\n  key_mgmt=WPA-PSK\n'
           '  psk="secret pass"\n}\n')

    def test_wpa_blocks_and_conf(self):
        blocks = ng.wpa_blocks(self.WPA + "network={\n  ssid=\"Other\"\n  psk=abc\n}\n")
        self.assertEqual(len(blocks), 2)
        self.assertIn('ssid=P"Home"', blocks[0])
        text = ng.wpa_conf_text(blocks + blocks[:1])   # duplicates from two pinned dongles: kept once
        self.assertEqual(text.count("network={"), 2)
        self.assertIn("ctrl_interface=/run/wpa_supplicant", text)

    def test_split_netplan(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML missing")
        only = "network:\n  version: 2\n  wifis:\n    wlx1:\n      dhcp4: true\n      optional: true\n" \
               "      access-points:\n        \"Home\":\n          password: \"x\"\n"
        self.assertEqual(ng.split_netplan(only), (None, True))
        mixed = "network:\n  version: 2\n  ethernets:\n    eno1:\n      dhcp4: true\n" + only.split("version: 2\n")[1]
        rest, has = ng.split_netplan(mixed)
        self.assertTrue(has)
        doc = yaml.safe_load(rest)
        self.assertEqual(doc["network"]["ethernets"], {"eno1": {"dhcp4": True}})
        self.assertNotIn("wifis", doc["network"])
        self.assertNotIn("password", rest)
        eth = "network:\n  version: 2\n  ethernets:\n    eno1:\n      dhcp4: true\n"
        self.assertEqual(ng.split_netplan(eth), (eth, False))
        with self.assertRaises(ValueError):   # a static Wi-Fi address stays with netplan
            ng.split_netplan(only.replace("dhcp4: true", "addresses: [10.0.0.5/24]"))
        with self.assertRaises(ValueError):
            ng.split_netplan(only.replace("version: 2", "version: 2\n  renderer: NetworkManager"))

    def test_physical_links_and_usb(self):
        with tempfile.TemporaryDirectory() as d:
            usb = os.path.join(d, "devices/pci0000:00/0000:00:14.0/usb3/3-6")
            write(os.path.join(usb, "idVendor"), "0bda\n")
            write(os.path.join(usb, "idProduct"), "b812\n")
            os.makedirs(os.path.join(usb, "3-6:1.0"))
            pci = os.path.join(d, "devices/pci0000:00/0000:00:1f.6")
            os.makedirs(pci)
            net = os.path.join(d, "class/net")
            for name, dev, flags, carrier in (("wlx1", os.path.join(usb, "3-6:1.0"), "0x1003", "1"),
                                              ("eno1", pci, "0x1002", "")):
                os.makedirs(os.path.join(net, name))
                os.symlink(dev, os.path.join(net, name, "device"))
                write(os.path.join(net, name, "flags"), flags)
                write(os.path.join(net, name, "carrier"), carrier)
            os.makedirs(os.path.join(net, "wlx1", "wireless"))
            os.makedirs(os.path.join(net, "tailscale0"))   # virtual: no device link
            links = ng.physical_links(net)
        self.assertEqual(sorted(links), ["eno1", "wlx1"])
        self.assertEqual((links["wlx1"]["kind"], links["wlx1"]["admin_up"], links["wlx1"]["carrier"]),
                         ("wifi", True, True))
        self.assertEqual(links["wlx1"]["usb"]["usb"], "3-6")
        self.assertEqual(links["wlx1"]["usb"]["id"], "0bda:b812")
        self.assertEqual((links["eno1"]["kind"], links["eno1"]["admin_up"], links["eno1"]["usb"]),
                         ("wired", False, None))

    def test_pkg_busy(self):
        with tempfile.TemporaryDirectory() as d:
            write(os.path.join(d, "123/comm"), "bash\n")
            write(os.path.join(d, "locks"), "")
            self.assertFalse(ng.pkg_busy(d))
            write(os.path.join(d, "456/comm"), "dpkg\n")
            self.assertTrue(ng.pkg_busy(d))
            os.remove(os.path.join(d, "456/comm"))
            lock = os.path.join(d, "lock-frontend")
            write(lock, "")
            ino = os.stat(lock).st_ino
            old = ng.PKG_LOCKS
            ng.PKG_LOCKS = (lock,)
            try:
                write(os.path.join(d, "locks"), f"1: POSIX  ADVISORY  WRITE 999 08:02:{ino} 0 EOF\n")
                self.assertTrue(ng.pkg_busy(d))
                write(os.path.join(d, "locks"), f"1: POSIX  ADVISORY  WRITE 999 08:02:{ino + 1} 0 EOF\n")
                self.assertFalse(ng.pkg_busy(d))
            finally:
                ng.PKG_LOCKS = old

    def test_describe(self):
        o = obs(path_ok=False, ts="down", dongle_state="no_netdev", dongle={"usb": "3-6"}, wifi_conf="treehouse")
        o["links"] = {"eno1": {"kind": "wired", "admin_up": True, "carrier": False},
                      "eno2": {"kind": "wired", "admin_up": False, "carrier": False},
                      "enp3": {"kind": "wired", "admin_up": True, "carrier": True, "ipv4": ["10.0.0.5"],
                               "routes": [{"gateway": "10.0.0.1"}], "gw_ok": False}}
        text = ng.describe(o)
        for want in ("eno1: no cable", "eno2: switched off", "gateway 10.0.0.1 not answering",
                     "dongle on USB 3-6 has no network interface", "tailscale: down"):
            self.assertIn(want, text)


class TelemetryHook(unittest.TestCase):
    def entry(self, msg):
        return {"MESSAGE": msg, "__REALTIME_TIMESTAMP": "1700000000000000", "SYSLOG_IDENTIFIER": "sharedbox-netguard"}

    def test_actions_become_events(self):
        ev = T.journal_event(self.entry("net-guard: reboot [down 1230s] no working network for 20 min: eno1: no "
                                        "cable"))
        self.assertEqual((ev["kind"], ev["unit"], ev["value"]), ("net_guard", "reboot", 1230))
        self.assertIn("eno1: no cable", ev["detail"])
        self.assertIn("after 20 min down", T.event_text(ev))
        ev = T.journal_event(self.entry("net-guard: wifi_setup Wi-Fi now works with any USB dongle"))
        self.assertEqual((ev["unit"], ev["value"]), ("wifi_setup", None))
        self.assertIsNone(T.journal_event(self.entry("net-guard: down [down 0s] eno1: no cable")))
        self.assertIn("net-guard: ", T.JOURNAL_PATTERN)


class TuneChecks(unittest.TestCase):
    def status(self, **kw):
        s = {"timer": True, "on": True, "wifi": "treehouse", "pinned": [], "state": {},
             "links": {"eno1": {"kind": "wired", "carrier": True, "ipv4": ["10.0.0.5"], "metric": 100},
                       "wlx1": {"kind": "wifi", "carrier": True, "ipv4": ["10.0.0.6"], "metric": 600}}}
        s.update(kw)
        return s

    def by_name(self, res):
        return {r["name"]: r for r in res}

    def test_all_good(self):
        res = tune.check_netguard(self.status())
        self.assertTrue(all(r["ok"] for r in res), res)
        self.assertIn("cable preferred over Wi-Fi", self.by_name(res))
        lines = "\n".join(tune.network_lines(self.status()))
        self.assertIn("Wi-Fi config: any USB dongle", lines)
        self.assertIn("route metric 600", lines)

    def test_pinned_wifi_and_failed_setup(self):
        st = {"wifi_setup": {"ok": False, "why": "the Wi-Fi didn't come back"}}
        r = self.by_name(tune.check_netguard(self.status(wifi="netplan", pinned=["wlxe84e"], state=st)))
        f = r["Wi-Fi works with any USB dongle"]
        self.assertFalse(f["ok"])
        self.assertIn("wlxe84e", f["now"])
        self.assertIn("didn't come back", f["why"])

    def test_wifi_preferred_is_flagged(self):
        s = self.status()
        s["links"]["eno1"]["metric"] = 700
        self.assertFalse(self.by_name(tune.check_netguard(s))["cable preferred over Wi-Fi"]["ok"])

    def test_recent_reboot_and_last_action_shown(self):
        st = {"last_reboot": {"ts": 1_000_000, "reason": "no working network for 20 min"},
              "last_action": {"ts": 1_000_000, "action": "reboot", "detail": "no working network for 20 min"}}
        r = self.by_name(tune.check_netguard(self.status(state=st), now=1_000_000 + 3600))
        self.assertIn("no working network", r["net-guard rebooted the box"]["now"])
        self.assertEqual(r["net-guard rebooted the box"]["kind"], "owner")
        self.assertIn("last action", "\n".join(tune.network_lines(self.status(state=st))))
        r = self.by_name(tune.check_netguard(self.status(state=st), now=1_000_000 + 8 * 86400))
        self.assertNotIn("net-guard rebooted the box", r)

    def test_not_running(self):
        r = self.by_name(tune.check_netguard(self.status(timer=False)))
        self.assertFalse(r["net-guard (brings the network back by itself)"]["ok"])
        r = self.by_name(tune.check_netguard(self.status(timer=False, on=False)))
        self.assertTrue(r["net-guard (brings the network back by itself)"]["ok"])


class Shipped(unittest.TestCase):
    def read(self, *p):
        with open(os.path.join(ROOT, *p)) as f:
            return f.read()

    def test_udev_rule_any_wifi_interface(self):
        rule = [ln for ln in self.read("etc", "udev-wifi.rules").splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(rule), 1)
        self.assertIn('ENV{DEVTYPE}=="wlan"', rule[0])
        self.assertIn('ACTION!="remove"', rule[0])
        self.assertIn("sharedbox-wifi@$name.service", rule[0])
        self.assertNotIn("wlx", rule[0])

    def test_wifi_network_matches_by_type_with_higher_metric(self):
        text = self.read("etc", "networkd-wifi.network")
        self.assertIn("Name=wl*", text)
        self.assertIn("RequiredForOnline=no", text)
        self.assertEqual(text.count("RouteMetric=600"), 3)
        self.assertNotIn("MACAddress", text.split("[Match]")[1])

    def test_wifi_unit_follows_the_device(self):
        text = self.read("systemd", "sharedbox-wifi@.service")
        self.assertIn("BindsTo=sys-subsystem-net-devices-%i.device", text)
        self.assertIn("Restart=always", text)
        self.assertIn("ConditionPathExists=/etc/sharedbox/wifi/wpa_supplicant.conf", text)

    def test_timer_every_30s(self):
        self.assertIn("OnUnitActiveSec=30s", self.read("systemd", "sharedbox-netguard.timer"))

    def test_install_and_uninstall(self):
        inst = self.read("install.sh")
        self.assertIn("sharedbox-netguard; do", inst)
        self.assertIn("/etc/udev/rules.d/99-sharedbox-wifi.rules", inst)
        self.assertIn("/etc/systemd/network/80-sharedbox-wifi.network", inst)
        self.assertIn("sharedbox-netguard.timer", inst)
        self.assertIn('"$LIB/sharedbox-netguard" wifi-setup', inst)
        self.assertFalse([ln for ln in inst.splitlines() if "netplan apply" in ln.split("#")[0]])
        un = self.read("uninstall.sh")
        self.assertIn("wifi-restore", un)
        self.assertIn("sharedbox-netguard.timer", un)
        self.assertIn("99-sharedbox-wifi.rules", un)

    def test_conf_documents_thresholds(self):
        conf = self.read("etc", "sharedbox.conf")
        for k in ng.DEFAULTS:
            self.assertIn(f"#{k}={ng.DEFAULTS[k]}", conf)


if __name__ == "__main__":
    unittest.main()
