"""Tests for rtsp-play. The player binaries are mocked via PATH so the tests
run on a headless box and never block on a real stream."""

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "rtsp-play"


def _load_module():
    loader = SourceFileLoader("rtsp_play", str(SCRIPT))
    spec = importlib.util.spec_from_loader("rtsp_play", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


rp = _load_module()


# ---------------------------------------------------------------------------
# Argv builders
# ---------------------------------------------------------------------------

class BuildMpvArgvTests(unittest.TestCase):
    def test_default_tcp_with_audio(self):
        argv = rp.build_mpv_argv("rtsp://x/y", "tcp", no_audio=False, verbose=False)
        self.assertEqual(argv[0], "mpv")
        # Low-latency tuning we expect to always be present:
        self.assertIn("--profile=low-latency", argv)
        # Transport is layered on top with -add (not assignment), so the
        # profile's own demuxer-lavf-o defaults are preserved.
        self.assertIn("--demuxer-lavf-o-add=rtsp_transport=tcp", argv)
        self.assertIn("--demuxer-lavf-o-add=fflags=+discardcorrupt", argv)
        self.assertIn("--demuxer-lavf-probesize=32", argv)
        self.assertIn("--demuxer-lavf-analyzeduration=0", argv)
        self.assertIn("--cache=no", argv)
        self.assertIn("--framedrop=decoder+vo", argv)
        self.assertIn("--really-quiet", argv)
        self.assertNotIn("--no-audio", argv)
        # URL must come last, after `--`, so a url starting with `-` would not
        # be parsed as an option.
        self.assertEqual(argv[-2], "--")
        self.assertEqual(argv[-1], "rtsp://x/y")

    def test_no_audio_and_udp(self):
        argv = rp.build_mpv_argv("rtsp://x/y", "udp", no_audio=True, verbose=False)
        self.assertIn("--no-audio", argv)
        self.assertIn("--demuxer-lavf-o-add=rtsp_transport=udp", argv)

    def test_verbose_drops_quiet_flag(self):
        argv = rp.build_mpv_argv("rtsp://x/y", "tcp", no_audio=False, verbose=True)
        self.assertNotIn("--really-quiet", argv)

    def test_does_not_use_assignment_form(self):
        # --demuxer-lavf-o=... (assignment) clobbers the low-latency profile
        # defaults; only the -add form is safe. Guard against regressions.
        argv = rp.build_mpv_argv("rtsp://x/y", "tcp", no_audio=False, verbose=False)
        for tok in argv:
            self.assertFalse(
                tok.startswith("--demuxer-lavf-o="),
                msg=f"unexpected assignment-form option: {tok}",
            )


class BuildFfplayArgvTests(unittest.TestCase):
    def test_default_tcp_with_audio(self):
        argv = rp.build_ffplay_argv("rtsp://x/y", "tcp", no_audio=False, verbose=False)
        self.assertEqual(argv[0], "ffplay")
        self.assertEqual(argv[argv.index("-rtsp_transport") + 1], "tcp")
        self.assertEqual(argv[argv.index("-fflags") + 1], "nobuffer+discardcorrupt")
        self.assertEqual(argv[argv.index("-flags") + 1], "low_delay")
        self.assertEqual(argv[argv.index("-probesize") + 1], "32")
        self.assertEqual(argv[argv.index("-analyzeduration") + 1], "0")
        self.assertIn("-framedrop", argv)
        self.assertIn("-loglevel", argv)
        self.assertIn("quiet", argv)
        self.assertEqual(argv[-1], "rtsp://x/y")

    def test_no_audio_and_udp(self):
        argv = rp.build_ffplay_argv("rtsp://x/y", "udp", no_audio=True, verbose=False)
        self.assertIn("-an", argv)
        self.assertEqual(argv[argv.index("-rtsp_transport") + 1], "udp")

    def test_verbose_drops_loglevel(self):
        argv = rp.build_ffplay_argv("rtsp://x/y", "tcp", no_audio=False, verbose=True)
        self.assertNotIn("-loglevel", argv)


# ---------------------------------------------------------------------------
# Player selection
# ---------------------------------------------------------------------------

class _PathSandbox:
    """Context manager: replace PATH with a tmp dir holding fake players.

    Each fake player is a small shell script that records its argv to a file
    next to the binary, then exits 0.
    """
    def __init__(self, players):
        self.players = players
        self.dir = None
        self.old_path = None

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        for name in self.players:
            log_path = os.path.join(self.dir, f"{name}.log")
            script_path = os.path.join(self.dir, name)
            with open(script_path, "w") as f:
                f.write(
                    "#!/bin/sh\n"
                    f'printf "%s\\n" "$@" > "{log_path}"\n'
                    "exit 0\n"
                )
            os.chmod(script_path, 0o755)
        self.old_path = os.environ.get("PATH")
        os.environ["PATH"] = self.dir
        return self

    def __exit__(self, *exc):
        os.environ["PATH"] = self.old_path or ""
        # leave tmpdir; OS will clean it eventually. Cheaper than recursive rm
        # on the per-test path and irrelevant for our purposes.

    def log(self, name):
        with open(os.path.join(self.dir, f"{name}.log")) as f:
            return f.read().splitlines()


class SelectPlayerTests(unittest.TestCase):
    def test_auto_prefers_mpv(self):
        with _PathSandbox(["mpv", "ffplay"]):
            self.assertEqual(rp.select_player("auto"), "mpv")

    def test_auto_falls_back_to_ffplay(self):
        with _PathSandbox(["ffplay"]):
            self.assertEqual(rp.select_player("auto"), "ffplay")

    def test_auto_neither_present(self):
        with _PathSandbox([]):
            with self.assertRaises(rp._Err) as cm:
                rp.select_player("auto")
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("neither mpv nor ffplay", cm.exception.msg)

    def test_explicit_ffplay_when_only_mpv_present(self):
        with _PathSandbox(["mpv"]):
            with self.assertRaises(rp._Err) as cm:
                rp.select_player("ffplay")
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("ffplay not found", cm.exception.msg)


# ---------------------------------------------------------------------------
# CLI end-to-end (with mocked players)
# ---------------------------------------------------------------------------

def _run(args, env=None, stdin_text=None, timeout=10):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
        env=env, input=stdin_text, timeout=timeout,
    )


class CliTests(unittest.TestCase):
    def test_invokes_mpv_by_default(self):
        with _PathSandbox(["mpv", "ffplay"]) as sb:
            env = dict(os.environ)
            env["PATH"] = sb.dir
            r = _run(["rtsp://192.168.1.1/x"], env=env)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            args = sb.log("mpv")
            self.assertIn("--profile=low-latency", args)
            self.assertIn("--demuxer-lavf-o-add=rtsp_transport=tcp", args)
            self.assertEqual(args[-1], "rtsp://192.168.1.1/x")

    def test_force_ffplay(self):
        with _PathSandbox(["mpv", "ffplay"]) as sb:
            env = dict(os.environ); env["PATH"] = sb.dir
            r = _run(["--player", "ffplay", "rtsp://x/y"], env=env)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            args = sb.log("ffplay")
            self.assertIn("-rtsp_transport", args)
            self.assertEqual(args[-1], "rtsp://x/y")

    def test_url_from_stdin(self):
        with _PathSandbox(["mpv"]) as sb:
            env = dict(os.environ); env["PATH"] = sb.dir
            r = _run([], env=env, stdin_text="rtsp://1.1.1.1/a\n")
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertEqual(sb.log("mpv")[-1], "rtsp://1.1.1.1/a")

    def test_no_audio_translated(self):
        with _PathSandbox(["mpv"]) as sb:
            env = dict(os.environ); env["PATH"] = sb.dir
            r = _run(["--no-audio", "rtsp://x/y"], env=env)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("--no-audio", sb.log("mpv"))

        with _PathSandbox(["ffplay"]) as sb:
            env = dict(os.environ); env["PATH"] = sb.dir
            r = _run(["--no-audio", "--player", "ffplay", "rtsp://x/y"], env=env)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("-an", sb.log("ffplay"))

    def test_udp_transport(self):
        with _PathSandbox(["mpv"]) as sb:
            env = dict(os.environ); env["PATH"] = sb.dir
            r = _run(["--transport", "udp", "rtsp://x/y"], env=env)
            self.assertEqual(r.returncode, 0, msg=r.stderr)
            self.assertIn("--demuxer-lavf-o-add=rtsp_transport=udp", sb.log("mpv"))

    def test_no_player_in_path(self):
        with _PathSandbox([]) as sb:
            env = dict(os.environ); env["PATH"] = sb.dir
            r = _run(["rtsp://x/y"], env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn("neither mpv nor ffplay", r.stderr)

    def test_malformed_url(self):
        r = _run(["http://not-rtsp/x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("not an RTSP URL", r.stderr)

    def test_empty_stdin(self):
        r = _run([], stdin_text="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no RTSP URL on stdin", r.stderr)

    def test_player_returncode_propagates(self):
        # Build a player that exits with a non-zero status so we can verify
        # we surface its return code unchanged.
        with tempfile.TemporaryDirectory() as d:
            mpv = os.path.join(d, "mpv")
            with open(mpv, "w") as f:
                f.write("#!/bin/sh\nexit 17\n")
            os.chmod(mpv, 0o755)
            env = dict(os.environ); env["PATH"] = d
            r = _run(["rtsp://x/y"], env=env)
            self.assertEqual(r.returncode, 17)

    def test_version(self):
        r = _run(["-V"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), f"{rp.PROG} {rp.VERSION}")

    def test_help(self):
        r = _run(["-h"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("RTSP", r.stdout)


if __name__ == "__main__":
    unittest.main()
