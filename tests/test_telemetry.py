"""Tests for lib/sbtelemetry.py (box telemetry): parsers, the collector on a fake /proc + cgroup tree, storage,
roll-ups, retention, the detectors, fleet export/import and plan usage. No root, no network:
    python3 -m unittest tests.test_telemetry -v"""

import json
import os
import pwd
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sbtestenv  # noqa: E402,F401  (first: shared conf/state for the run)
import sbtelemetry as T  # noqa: E402

ME = pwd.getpwuid(os.getuid()).pw_name
UID = os.getuid()
NOW = 1_800_000_000 // 3600 * 3600 + 1800   # half past some hour


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Parsers(unittest.TestCase):
    def test_psi_meminfo_cpu(self):
        p = T.parse_psi("some avg10=1.50 avg60=0.20 avg300=0.00 total=123456\n"
                        "full avg10=0.00 avg60=0.00 avg300=0.00 total=789\n")
        self.assertEqual((p["some"], p["full"], p["some_avg10"]), (123456, 789, 1.5))
        m = T.parse_meminfo("MemTotal:       32000000 kB\nMemAvailable:   16000000 kB\nHugePages_Total: 0\n")
        self.assertEqual(m["MemTotal"], 32000000 * 1024)
        self.assertEqual(m["HugePages_Total"], 0)
        self.assertEqual(T.parse_cpu_line("cpu  10 0 10 70 5 0 0 5 0 0\ncpu0 1 2 3\n"), (100, 70, 5, 5))

    def test_proc_stat_odd_names(self):
        st = "4242 (my (weird) prog) S 1 4242 4242 0 -1 4194560 0 0 0 0 150 50 0 0 20 0 1 0 9000 0 2500 0"
        comm, state, ppid, ticks, start, rss = T.parse_proc_stat(st)
        self.assertEqual((comm, state, ppid, ticks, start), ("my (weird) prog", "S", 1, 200, 9000))
        self.assertEqual(rss, 2500 * T.PAGE)
        self.assertIsNone(T.parse_proc_stat("garbage"))

    def test_mounts_and_diskstats(self):
        m = T.parse_mounts("/dev/mapper/vg-root / ext4 rw 0 0\n/dev/mapper/vg-root /home ext4 rw 0 0\n"
                           "/dev/sda1 /boot/efi vfat rw 0 0\ntmpfs /run tmpfs rw 0 0\n"
                           "/dev/loop3 /snap/x squashfs ro 0 0\n/dev/sdb1 /srv/data xfs rw 0 0\n")
        self.assertEqual(m, [("/", "/dev/mapper/vg-root"), ("/srv/data", "/dev/sdb1")])
        d = T.parse_diskstats("   8       0 sda 100 0 2000 0 50 0 4000 0 0 300 0\n"
                              "   8       1 sda1 1 0 2 0 1 0 4 0 0 3 0\n"
                              " 259       0 nvme0n1 1 0 8 0 1 0 16 0 0 7 0\n")
        self.assertEqual(d, {"sda": (2000, 4000, 300), "nvme0n1": (8, 16, 7)})

    def test_names_never_keep_arguments(self):
        self.assertEqual(T.proc_name("python3", ["python3", "/home/x/secret-merger-plan.py", "--token=abc"]),
                         "python3")
        self.assertEqual(T.proc_name("python3", ["python3", "-m", "pytest", "-x"]), "python3 pytest")
        self.assertEqual(T.proc_name("node", ["node", "/p/node_modules/vite/bin/vite.js", "dev"]), "node vite")
        self.assertEqual(T.proc_name("node-MainThread", ["/home/x/.t3/runtime/versions/1/t3", "serve"]),
                         "node-MainThread t3")
        self.assertEqual(T.proc_name("cc1plus", ["cc1plus", "secret.cpp"]), "cc1plus")
        self.assertEqual(T.agent_of("node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]),
                         "claude")
        self.assertEqual(T.agent_of("node", ["node", "/x/node_modules/@openai/codex/bin/codex.js"]), "codex")
        self.assertEqual(T.agent_of("codex", []), "codex")
        self.assertIsNone(T.agent_of("node", ["node", "server.js"]))


def entry(msg, ts=1_800_000_000.5, **kw):
    e = {"MESSAGE": msg, "__REALTIME_TIMESTAMP": str(int(ts * 1e6))}
    e.update(kw)
    return e


class Journal(unittest.TestCase):
    def test_kernel_oom_with_cgroup(self):
        info = {}
        self.assertIsNone(T.journal_event(entry(
            "oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,mems_allowed=0,"
            f"oom_memcg=/user.slice/user-{UID}.slice,task_memcg=/user.slice/user-{UID}.slice/session-3.scope,"
            f"task=python3,pid=777,uid={UID}", _TRANSPORT="kernel"), info))
        ev = T.journal_event(entry(f"Memory cgroup out of memory: Killed process 777 (python3) total-vm:9000kB, "
                                   f"anon-rss:2097152kB, file-rss:0kB, shmem-rss:0kB, UID:{UID} pgtables:10kB "
                                   "oom_score_adj:0", _TRANSPORT="kernel"), info)
        self.assertEqual((ev["kind"], ev["user"], ev["unit"], ev["value"]), ("oom_kill", ME, "session-3.scope", 2048))
        self.assertEqual(ev["detail"], "python3 (limit)")

    def test_earlyoom_drops_the_command_line(self):
        ev = T.journal_event(entry(f'sending SIGTERM to process 4321 uid {UID} "node": oom_score 600, VmRSS 3017 '
                                   'MiB, cmdline "node build.js --api-key=sk-secret"', SYSLOG_IDENTIFIER="earlyoom"))
        self.assertEqual((ev["kind"], ev["user"], ev["detail"], ev["value"]), ("earlyoom_kill", ME, "node", 3017))
        self.assertNotIn("secret", json.dumps(ev))

    def test_oomd_and_units(self):
        ev = T.journal_event(entry(f"Killed /user.slice/user-{UID}.slice/user@{UID}.service/app.slice/x.scope due "
                                   "to memory pressure for /user.slice being 82% > 80% for > 30s",
                                   SYSLOG_IDENTIFIER="systemd-oomd"))
        self.assertEqual((ev["kind"], ev["user"], ev["unit"]), ("oomd_kill", ME, "x.scope"))
        f = T.journal_event(entry("sharedbox-web.service: Failed with result 'exit-code'.", _UID="0", _COMM="systemd"))
        self.assertEqual((f["kind"], f["unit"], f["detail"], f["user"]), ("unit_failed", "sharedbox-web.service",
                                                                          "exit-code", ""))
        r = T.journal_event(entry("sb-app.service: Scheduled restart job, restart counter is at 7.", _UID=str(UID)))
        self.assertEqual((r["kind"], r["value"], r["user"]), ("unit_restart", 7, ME))
        o = T.journal_event(entry("x.service: A process of this unit has been killed by the OOM killer."))
        self.assertEqual(o["kind"], "unit_oom")
        s = T.journal_event(entry("x.service: Start request repeated too quickly."))
        self.assertEqual((s["kind"], s["detail"]), ("unit_failed", "start-limit"))
        h = T.journal_event(entry("INFO: task jbd2/sda1-8:123 blocked for more than 122 seconds.", _TRANSPORT="kernel"))
        self.assertEqual((h["kind"], h["value"]), ("hung_task", 122))
        d = T.journal_event(entry("I/O error, dev sdb, sector 1234 op 0x0:(READ)", _TRANSPORT="kernel"))
        self.assertEqual((d["kind"], d["detail"]), ("disk_error", "sdb"))
        self.assertIsNone(T.journal_event(entry("Started session-1.scope.", _COMM="systemd")))
        b = T.journal_event(entry(list(b"x.service: Failed with result 'signal'."), _COMM="systemd"))
        self.assertEqual(b["detail"], "signal")

    def test_boot_reason(self):
        self.assertEqual(T.boot_reason("32", None, []), "watchdog")
        self.assertEqual(T.boot_reason("0", {"MESSAGE": "x"}, ["/sys/fs/pstore/dmesg"]), "panic")
        self.assertEqual(T.boot_reason("", {"MESSAGE": "Journal stopped"}, []), "clean")
        self.assertEqual(T.boot_reason("", {"MESSAGE": "eth0 link up", "SYSLOG_IDENTIFIER": "kernel"}, []),
                         "unclean")
        self.assertEqual(T.boot_reason("", None, []), "unknown")


class FakeBox:
    """A /proc, /sys and cgroup tree in a temp dir that tests can advance."""

    def __init__(self, root):
        self.proc, self.sys, self.cg = (os.path.join(root, d) for d in ("proc", "sys", "cg"))
        write(os.path.join(self.proc, "sys/kernel/random/boot_id"), "boot-1\n")
        write(os.path.join(self.proc, "uptime"), "5000.0 100.0\n")
        write(os.path.join(self.proc, "loadavg"), "1.5 1.0 0.5 2/300 999\n")
        write(os.path.join(self.proc, "meminfo"), "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n"
              "AnonPages: 6000000 kB\nCached: 4000000 kB\nBuffers: 100000 kB\nSReclaimable: 200000 kB\n"
              "Shmem: 100000 kB\nSwapTotal: 4194304 kB\nSwapFree: 4194304 kB\n")
        write(os.path.join(self.proc, "mounts"), "")
        write(os.path.join(self.proc, "diskstats"), "")
        write(os.path.join(self.proc, "cpuinfo"), "cpu MHz : 2000.0\ncpu MHz : 3000.0\n")
        self.slice = os.path.join(self.cg, "user.slice", f"user-{UID}.slice")
        self.set(cpu=(1000, 800, 0), psi_mem=0, vm=(0, 0, 0, 0), slice_cpu=0, slice_oom=0, slice_max=0)

    def set(self, cpu, psi_mem, vm, slice_cpu, slice_oom, slice_max):
        total, idle, iowait = cpu
        write(os.path.join(self.proc, "stat"),
              f"cpu  {total - idle - iowait} 0 0 {idle} {iowait} 0 0 0 0 0\nbtime 1700000000\n"
              "procs_running 3\nprocs_blocked 1\n")
        for f in ("cpu", "io"):
            write(os.path.join(self.proc, "pressure", f), "some avg10=0 avg60=0 avg300=0 total=0\n"
                  "full avg10=0 avg60=0 avg300=0 total=0\n")
        write(os.path.join(self.proc, "pressure", "memory"), f"some avg10=0 avg60=0 avg300=0 total={psi_mem}\n"
              f"full avg10=0 avg60=0 avg300=0 total={psi_mem // 2}\n")
        write(os.path.join(self.proc, "vmstat"), "pswpin {}\npswpout {}\npgmajfault {}\noom_kill {}\n".format(*vm))
        write(os.path.join(self.slice, "cpu.stat"), f"usage_usec {slice_cpu}\n")
        write(os.path.join(self.slice, "memory.stat"), f"anon {3 << 30}\nfile {2 << 30}\nshmem {1 << 20}\n"
              f"kernel {100 << 20}\n")
        write(os.path.join(self.slice, "memory.events"), f"low 0\nhigh 0\nmax {slice_max}\noom 0\noom_kill {slice_oom}\n")
        write(os.path.join(self.slice, "memory.pressure"), "some total=0\nfull total=0\n")
        write(os.path.join(self.slice, "pids.current"), "42\n")

    def proc_(self, pid, comm, ppid, ticks, rss_pages, argv):
        write(os.path.join(self.proc, str(pid), "stat"),
              f"{pid} ({comm}) S {ppid} 1 1 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0 100 0 {rss_pages} 0")
        write(os.path.join(self.proc, str(pid), "cmdline"), "\0".join(argv) + "\0")

    def container(self, cid, mem, peak):
        d = os.path.join(self.slice, f"user@{UID}.service", "app.slice", f"docker-{cid}.scope")
        write(os.path.join(d, "memory.current"), str(mem))
        write(os.path.join(d, "memory.peak"), str(peak))
        write(os.path.join(d, "cpu.stat"), "usage_usec 0\n")
        return d


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = FakeBox(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def collector(self, state):
        b = self.box
        return T.Collector("testbox", state, proc=b.proc, sys_dir=b.sys, cgroup=b.cg, journal=False)

    def test_two_samples_make_deltas(self):
        b = self.box
        b.proc_(100, "claude", 1, 1000, 25600, ["/home/x/.local/bin/claude", "--resume"])
        b.proc_(101, "python3", 100, 500, 51200, ["python3", "-m", "pytest"])
        b.proc_(102, "node", 1, 10, 1000, ["node", "/home/x/secret.js"])
        cid = "a" * 64
        b.container(cid, 100 << 20, 300 << 20)
        data, st = self.collector({}).collect(NOW)
        kinds = [e["kind"] for e in data["events"]]
        self.assertEqual(kinds, ["boot"])                       # first ever sample: when the box started
        self.assertEqual(data["samples"][0]["mem_used_mb"], 8192)
        self.assertIsNone(data["samples"][0].get("cpu_pct"))   # no previous sample yet
        self.assertEqual(data["containers"][0]["name"], cid[:12])
        # a minute later: CPU, memory stall, an OOM kill in the slice, a limit hit, the container gone
        b.set(cpu=(1600, 1100, 60), psi_mem=6_000_000, vm=(256, 0, 9, 1), slice_cpu=30_000_000, slice_oom=1,
              slice_max=4)
        b.proc_(100, "claude", 1, 1000 + 60 * T.HZ // 2, 25600, ["claude"])
        b.proc_(101, "python3", 100, 500 + 60 * T.HZ, 51200, ["python3", "-m", "pytest"])
        import shutil
        shutil.rmtree(os.path.dirname(os.path.dirname(b.container(cid, 0, 0))))
        data, st2 = self.collector(st).collect(NOW + 60)
        s = data["samples"][0]
        self.assertEqual(s["interval_s"], 60)
        self.assertEqual(s["cpu_pct"], 40.0)                  # (600 - 300 idle - 60 iowait... ) of 600 ticks
        self.assertEqual(s["iowait_pct"], 10.0)
        self.assertEqual(s["psi_mem"], 10.0)                   # 6 s of 60 s stalled
        self.assertEqual(s["psi_mem_full"], 5.0)
        self.assertEqual(s["oom_kills"], 1)
        self.assertEqual(s["swap_in_mb"], round(256 * T.PAGE / T.MB, 1))
        sl = next(r for r in data["slices"] if r["user"] == ME)
        self.assertEqual((sl["cpu_pct"], sl["oom_kills"], sl["mem_max_events"]), (50.0, 1, 4))
        self.assertEqual(sl["mem_mb"], 3 * 1024 + 1 + 100)    # anon + shmem + kernel, not the file cache
        self.assertEqual(sl["cache_mb"], 2 * 1024 - 1)
        ag = data["agents"]
        self.assertEqual(len(ag), 1)
        self.assertEqual((ag[0]["agent"], ag[0]["pid"], ag[0]["tree_procs"]), ("claude", 100, 2))
        self.assertEqual((ag[0]["cpu_pct"], ag[0]["tree_cpu_pct"]), (50.0, 150.0))
        names = {r["pid"]: r["name"] for r in data["procs"]}
        self.assertEqual(names[101], "python3 pytest")
        self.assertEqual(names[102], "node")                   # a script name is never recorded
        stop = [e for e in data["events"] if e["kind"] == "container_stop"]
        self.assertEqual((stop[0]["value"], stop[0]["user"]), (300, ME))
        # a stall: the next sample comes 10 minutes later
        data, _ = self.collector(st2).collect(NOW + 660)
        gap = [e for e in data["events"] if e["kind"] == "sample_gap"]
        self.assertEqual(gap[0]["value"], 600)
        # a new boot id: a boot event, no gap
        write(os.path.join(b.proc, "sys/kernel/random/boot_id"), "boot-2\n")
        data, _ = self.collector(st2).collect(NOW + 7200)
        self.assertEqual([e["kind"] for e in data["events"] if e["kind"] in ("boot", "sample_gap")], ["boot"])

    def test_for_user_keeps_only_own_rows(self):
        data = {"samples": [{"ts": 1}], "slices": [{"user": "a"}, {"user": "b"}, {"user": ""}],
                "procs": [{"user": "a"}, {"user": "b"}], "events": [{"user": ""}, {"user": "b"}],
                "agents": [{"user": "b"}], "containers": [{"user": ""}], "disks": [{}], "diskio": []}
        out = T.for_user(data, "a")
        self.assertEqual(out["slices"], [{"user": "a"}, {"user": ""}])
        self.assertEqual(out["procs"], [{"user": "a"}])
        self.assertEqual(out["events"], [{"user": ""}])
        self.assertEqual(out["agents"], [])
        self.assertEqual(out["containers"], [])                # system containers' names aren't everyone's
        self.assertEqual(out["samples"], [{"ts": 1}])


def sample(ts, box="b1", **kw):
    r = {"ts": ts, "box": box, "interval_s": 60, "ncpu": 8, "mem_total_mb": 16000, "mem_used_mb": 4000,
         "psi_mem": 0, "psi_mem_full": 0, "psi_io": 0, "psi_io_full": 0, "psi_cpu": 0, "load1": 1,
         "disk_free_gb": 100, "disk_total_gb": 500}
    r.update(kw)
    return r


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.con = T.connect(self.path)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def q(self, sql, *a):
        return self.con.execute(sql, a).fetchall()

    def test_rollup_and_retention(self):
        h0 = NOW // 3600 * 3600 - 3 * 3600
        rows = {"samples": [sample(h0 + i * 60, cpu_pct=float(i), oom_kills=1 if i == 5 else 0) for i in range(120)],
                "slices": [{"ts": h0 + i * 60, "box": "b1", "slice": "user-1.slice", "user": "u", "cpu_pct": 100.0,
                            "mem_mb": 1000 + i} for i in range(120)],
                "agents": [{"ts": h0 + i * 60, "box": "b1", "user": "u", "agent": "claude", "pid": p, "started": h0,
                            "cpu_pct": 50.0, "tree_cpu_pct": 100.0, "rss_mb": 300, "tree_rss_mb": 500}
                           for i in range(60) for p in (1, 2)],
                "procs": [{"ts": h0 + i * 60, "box": "b1", "user": "u", "pid": 9, "name": "python3 pytest",
                           "cpu_pct": 100.0, "rss_mb": 50} for i in range(60)],
                "events": [{"ts": h0 + 10.5, "box": "b1", "kind": "boot", "user": "", "unit": "", "detail": "clean",
                            "value": None}] * 2}
        T.store(self.con, rows)
        self.assertEqual(self.q("SELECT count(*) FROM events")[0][0], 1)   # the same event is kept once
        self.assertEqual(T.rollup(self.con, NOW), 3)
        h = self.q("SELECT ts, n, cpu_pct, cpu_pct_max, oom_kills FROM samples_hourly ORDER BY ts")
        self.assertEqual(h[0], (h0, 60, 29.5, 59.0, 1))
        self.assertEqual(h[1][1:4], (60, 89.5, 119.0))
        self.assertEqual(self.q("SELECT sessions, sessions_max, cpu_s, tree_cpu_s FROM agents_hourly")[0],
                         (2.0, 2, 3600.0, 7200.0))
        self.assertEqual(self.q("SELECT n, cpu_s FROM procs_hourly")[0], (60, 3600.0))
        self.assertEqual(self.q("SELECT mem_mb_max FROM slices_hourly ORDER BY ts")[1][0], 1119)
        self.assertEqual(T.rollup(self.con, NOW), 0)            # nothing new until the next hour ends
        # retention: minute rows older than raw_days go, roll-ups stay
        T.retention(self.con, raw_days=0, keep_days=400, max_mb=1024, now=NOW)
        self.assertEqual(self.q("SELECT count(*) FROM samples")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM samples_hourly")[0][0], 2)
        T.retention(self.con, raw_days=0, keep_days=0, max_mb=1024, now=NOW)
        self.assertEqual(self.q("SELECT count(*) FROM samples_hourly")[0][0], 0)

    def test_size_cap_drops_oldest_minute_days_first(self):
        day = 86400
        T.store(self.con, {"samples": [sample(NOW - d * day - i * 60, temp_max_c=float(i))
                                       for d in range(5) for i in range(1440)]})
        T.store(self.con, {"samples_hourly": [{"ts": NOW - h * 3600, "box": "b1", "n": 60} for h in range(24 * 60)]})
        before = T.db_bytes(self.con)
        T.retention(self.con, raw_days=14, keep_days=400, max_mb=max(1, before // T.MB // 3), now=NOW)
        oldest = self.q("SELECT min(ts) FROM samples")[0][0]
        self.assertGreater(oldest, NOW - 4 * day)                # the oldest days went
        self.assertGreater(self.q("SELECT count(*) FROM samples")[0][0], 0)
        self.assertEqual(self.q("SELECT count(*) FROM samples_hourly")[0][0], 24 * 60)   # roll-ups untouched

    def test_export_import_and_read(self):
        T.store(self.con, {"samples": [sample(NOW - 60), sample(NOW)],
                           "events": [{"ts": NOW, "box": "b1", "kind": "boot", "detail": "clean"}]})
        T.close(self.con)
        self.assertFalse(os.path.exists(self.path + "-wal"))      # folded in for read-only readers
        lines = list(T.read(self.path, lambda c: list(T.export(c, 0))))
        dst = T.connect(os.path.join(self.tmp.name, "fleet.db"))
        n = T.import_lines(dst, lines + [json.dumps({"t": "sqlite_master", "c": ["x"], "r": [[1]]}),
                                         json.dumps({"t": "samples", "c": ["ts", "box; DROP TABLE x"], "r": [[1, 2]]}),
                                         "not json"])
        self.assertEqual(n, {"samples": 2, "events": 1})
        self.assertEqual(T.import_lines(dst, lines), {"samples": 2, "events": 1})   # again: replaced, not doubled
        self.assertEqual(dst.execute("SELECT count(*) FROM samples").fetchone()[0], 2)
        self.assertEqual(dst.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        dst.close()
        cols, rows = T.sql(self.path, "SELECT count(*) AS n FROM samples")
        self.assertEqual((cols, rows), (["n"], [(2,)]))
        with self.assertRaises(sqlite3.Error):
            T.sql(self.path, "DELETE FROM samples")
        self.con = T.connect(self.path)


class Detectors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = T.connect(os.path.join(self.tmp.name, "t.db"))
        self.con.row_factory = None

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def run_(self, key):
        return T.patterns(self.con, 14, "b1", NOW)["boxes"][0]["found"][key]

    def test_oom_after_climb(self):
        t = NOW - 3600
        T.store(self.con, {"samples": [sample(t - 1800 + i * 60) for i in range(31)],
                           "slices": [{"ts": t - 1800 + i * 60, "box": "b1", "slice": "user-1.slice", "user": "sam",
                                       "mem_mb": 2000 + i * 300} for i in range(31)],
                           "events": [{"ts": t, "box": "b1", "kind": "earlyoom_kill", "user": "sam", "unit": "",
                                       "detail": "python3", "value": 9000}]})
        f = self.run_("oom_climb")
        self.assertEqual(len(f), 1)
        self.assertIn("climbed from 2.0 GB to 10.7 GB", f[0]["text"])
        self.assertIn("earlyoom killed sam's python3", f[0]["text"])

    def test_limit_hits_and_crash_loop(self):
        rows, events = [], []
        for day in range(3):
            ts = NOW - (day + 1) * 86400
            rows.append({"ts": ts, "box": "b1", "slice": "user-1.slice", "user": "sam", "mem_mb": 15000,
                         "mem_max_events": 3, "oom_kills": 1})
            events.append({"ts": ts + 5, "box": "b1", "kind": "oom_kill", "user": "sam", "unit": "", "detail": "node"})
        for i in range(8):
            events.append({"ts": NOW - 7200 + i * 120, "box": "b1", "kind": "unit_restart", "user": "sam",
                           "unit": "sb-api.service", "detail": "", "value": i + 1})
        T.store(self.con, {"slices": rows, "events": events, "samples": [sample(NOW - 100)]})
        lh = self.run_("limit_hits")
        self.assertEqual(len(lh), 1)
        self.assertIn("sam ran out of memory on 3 day(s) (3 kills, 9 limit hits", lh[0]["text"])
        self.assertEqual(lh[0]["sev"], "high")
        cl = self.run_("crash_loops")
        self.assertEqual(len(cl), 1)
        self.assertIn("sam/sb-api.service is crash-looping: 8 failures/restarts", cl[0]["text"])

    def test_disk_growth(self):
        T.store(self.con, {"disks_hourly": [{"ts": NOW - h * 3600, "box": "b1", "mount": "/", "dev": "sda",
                                             "total_gb": 500, "free_gb": 50 + h * 0.25} for h in range(72)],
                           "samples": [sample(NOW - 100)]})
        f = self.run_("disk_growth")
        self.assertEqual(len(f), 1)
        self.assertIn("/ fills 6.0 GB/day", f[0]["text"])
        self.assertIn("full in ~8 days", f[0]["text"])
        self.assertEqual(f[0]["sev"], "high")

    def test_hot_agents(self):
        T.store(self.con, {"agents": [{"ts": NOW - 4 * 3600 + i * 60, "box": "b1", "user": "sam", "agent": "codex",
                                       "pid": 5, "started": NOW - 5 * 3600, "cpu_pct": 95.0, "tree_cpu_pct": 100.0,
                                       "rss_mb": 400, "tree_rss_mb": 600} for i in range(180)]
                           + [{"ts": NOW - 3600 + i * 60, "box": "b1", "user": "amy", "agent": "claude", "pid": 6,
                               "started": NOW - 3600, "cpu_pct": 1.0, "tree_cpu_pct": 300.0, "rss_mb": 400,
                               "tree_rss_mb": 900} for i in range(30)]})
        f = self.run_("hot_agents")
        self.assertEqual(len(f), 1)
        self.assertIn("sam's codex (pid 5", f[0]["text"])
        self.assertIn("3.0 h", f[0]["text"])
        self.assertIn("the agent process itself", f[0]["text"])

    def test_near_freeze_then_restart(self):
        t = NOW - 7200
        S = [sample(t + i * 60) for i in range(20)]
        for i in range(10, 16):
            S[i].update(psi_mem=60, psi_mem_full=30, swap_in_mb=200)
        T.store(self.con, {"samples": S,
                           "slices": [{"ts": t + i * 60, "box": "b1", "slice": "user-1.slice", "user": "sam",
                                       "mem_mb": 14000} for i in range(10, 16)],
                           "events": [{"ts": t + 16 * 60 + 300, "box": "b1", "kind": "boot", "user": "", "unit": "",
                                       "detail": "unclean", "value": 240},
                                      {"ts": NOW - 300, "box": "b1", "kind": "sample_gap", "user": "", "unit": "",
                                       "detail": "", "value": 900}]})
        f = self.run_("near_freeze")
        self.assertEqual(len(f), 2)
        self.assertEqual(f[0]["sev"], "high")
        self.assertIn("6 min: memory stall up to 60%", f[0]["text"])
        self.assertIn("biggest users sam 13.7 GB", f[0]["text"])
        self.assertIn("then box started (unclean", f[0]["text"])
        self.assertIn("no samples for 15 min", f[1]["text"])

    def test_summary_renders(self):
        S = [sample(NOW - 3600 + i * 60) for i in range(60)]
        S[30].update(psi_mem=50, psi_mem_full=20, mem_used_mb=15500)
        T.store(self.con, {"samples": S, "procs": [{"ts": S[30]["ts"], "box": "b1", "user": "sam", "pid": 1,
                                                    "name": "python3 pytest", "cpu_pct": 400.0, "rss_mb": 9000}]})
        res = T.summary(self.con, 24, None, NOW)
        text = T.render_summary(res)
        self.assertIn("Worst moments:", text)
        self.assertIn("memory 97% used, memory stall 50%", text)
        self.assertIn("busiest: sam/python3 pytest 400% CPU", text)
        self.assertIn("No telemetry", T.render_summary(T.summary(self.con, 24, "nope", NOW)))


class PlanUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        write(os.path.join(self.home, ".claude", ".credentials.json"),
              json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": (NOW + 3600) * 1000}}))
        os.makedirs(os.path.join(self.home, "me"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_alerts_once_per_threshold(self):
        sent = []
        answers = iter([{"five_hour": {"utilization": 50, "resets_at": "2026-11-28T10:00:00Z"}},
                        {"five_hour": {"utilization": 83, "resets_at": "2026-11-28T10:00:00Z"},
                         "seven_day": {"utilization": 95, "resets_at": "2026-12-01T00:00:00Z"}, "extra": None},
                        {"five_hour": {"utilization": 85, "resets_at": "2026-11-28T10:00:00Z"}},
                        {"five_hour": {"utilization": 91, "resets_at": "2026-11-28T10:00:00Z"}}])
        tokens = []

        def fetch(tok):
            tokens.append(tok)
            return next(answers)
        for i in range(4):
            r = T.poll_usage(self.home, "sam", "b1", notify=lambda t, x: sent.append(t), fetch=fetch,
                             now=NOW + i * 600)
            self.assertTrue(r["ok"])
        self.assertEqual(tokens, ["tok"] * 4)
        self.assertEqual(sent, ["Claude five hour usage at 83%", "Claude seven day usage at 95%",
                                "Claude five hour usage at 91%"])
        skipped = T.poll_usage(self.home, "sam", "b1", fetch=fetch, now=NOW + 1900)
        self.assertTrue(skipped.get("skipped"))              # polled less than 5 minutes ago
        cols, rows = T.sql(T.fleet_path(self.home), "SELECT window, utilization FROM plan_usage ORDER BY ts, window")
        self.assertEqual(rows[-1], ("five_hour", 91.0))

    def test_no_login_or_expired(self):
        os.unlink(os.path.join(self.home, ".claude", ".credentials.json"))
        self.assertFalse(T.poll_usage(self.home, "sam", "b1", fetch=lambda t: {})["ok"])
        write(os.path.join(self.home, ".claude", ".credentials.json"),
              json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": (NOW - 10) * 1000}}))
        r = T.poll_usage(self.home, "sam", "b1", fetch=lambda t: self.fail("must not call"), force=True, now=NOW)
        self.assertIn("expired", r["error"])

    def test_usage_toggle(self):
        self.assertFalse(T.usage_on(self.home))
        T.usage_set(self.home, True)
        self.assertTrue(T.usage_on(self.home))
        T.usage_set(self.home, False)
        self.assertFalse(T.usage_on(self.home))


class RootJob(unittest.TestCase):
    def test_run_root_writes_box_and_account_files(self):
        import sbcommon
        with tempfile.TemporaryDirectory() as d:
            class SB:
                STATE_DIR = d
                read_json, write_json = staticmethod(sbcommon.read_json), staticmethod(sbcommon.write_json)
                boxes = staticmethod(lambda cfg: {})
                tailscale_ip4 = staticmethod(lambda: None)
                owner_account = staticmethod(lambda cfg: None)
                friend_users = staticmethod(lambda: {ME: {}})
                admin_users = staticmethod(lambda cfg: [])

            class FakeCollector:
                def __init__(self, box, state):
                    self.box = box

                def collect(self, now):
                    return ({"samples": [sample(now, box=self.box)],
                             "slices": [{"ts": now, "box": self.box, "slice": "user-1.slice", "user": ME,
                                         "mem_mb": 5},
                                        {"ts": now, "box": self.box, "slice": "user-2.slice", "user": "someone-else",
                                         "mem_mb": 7}]}, {"ts": now})
            stale = os.path.join(d, "telemetry", "users", "gone.db")
            write(stale, "")
            real, T.Collector = T.Collector, FakeCollector
            try:
                with unittest.mock.patch.object(T, "private_gid", lambda user: os.getgid()):
                    T.run_root({"TELEMETRY": "1"}, SB, now=NOW)
                    T.run_root({"TELEMETRY": "1"}, SB, now=NOW + 60)
                self.assertIsNone(T.run_root({"TELEMETRY": "0"}, SB, now=NOW + 120))
            finally:
                T.Collector = real
            full = os.path.join(d, "telemetry", "telemetry.db")
            own = os.path.join(d, "telemetry", "users", f"{ME}.db")
            self.assertFalse(os.path.exists(stale))           # an account that's gone loses its own copy
            self.assertEqual(T.sql(full, "SELECT count(*) FROM slices")[1], [(4,)])
            self.assertEqual(T.sql(own, "SELECT user FROM slices GROUP BY user")[1], [(ME,)])
            self.assertEqual(T.sql(own, "SELECT count(*) FROM samples")[1], [(2,)])
            self.assertEqual(T.readable_db(d, ME), (full, "all"))


class Backfill(unittest.TestCase):
    def test_old_health_files(self):
        with tempfile.TemporaryDirectory() as d:
            h = os.path.join(d, "health")
            write(os.path.join(h, "metrics.jsonl"), "\n".join(json.dumps(r) for r in [
                {"ts": NOW - 120, "cpu": 10, "load1": 1, "mem": 50.0, "swap": 0, "disk_free_b": 100e9,
                 "psi": [1, 2, 3], "users": {"sam": [1_000_000, 2 << 30]}},
                {"ts": NOW - 60, "cpu": 20, "load1": 2, "mem": 60.0, "swap": 1 << 30, "disk_free_b": 99e9,
                 "psi": [1, 2, 3], "users": {"sam": [31_000_000, 3 << 30]}}, "garbage"][:2]) + "\nnot json\n")
            write(os.path.join(h, "metrics-hourly.jsonl"), json.dumps(
                {"ts": NOW - 7200, "n": 60, "cpu": [5, 9], "load1": [1, 2], "mem": [40, 50], "psi": [[1, 2]] * 3,
                 "temp": [40, 50], "disk_free": 20, "users": {"sam": {"cpu_s": 360, "mem_mb": [100, 200]}}}) + "\n")
            con = T.connect(os.path.join(d, "t.db"))
            self.assertEqual(T.backfill(con, "b1", h), (2, 1))
            self.assertEqual(con.execute("SELECT cpu_pct FROM slices WHERE ts=?", (NOW - 60,)).fetchone()[0], 50.0)
            self.assertEqual(con.execute("SELECT swap_used_mb FROM samples WHERE ts=?", (NOW - 60,)).fetchone()[0],
                             1024)
            self.assertEqual(con.execute("SELECT cpu_pct, mem_mb_max FROM slices_hourly").fetchone(), (10.0, 200))
            con.close()


if __name__ == "__main__":
    unittest.main()
