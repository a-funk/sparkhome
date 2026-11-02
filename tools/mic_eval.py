#!/usr/bin/env python3
"""Siri Remote microphone suite: how well whisper hears each way the remote's audio can reach SparkHome, as word error
rate (WER) per phrase. Modes:

  src      the phrase as piper says it, 16 kHz, no codec: whisper's own ceiling (the control)
  full     (a) src -> Opus as the remote makes it (siri_audio.encode: CELT wideband 20 ms, VBR ~32 kbps, <= 94 bytes) ->
           its 101-byte values (seq, level, NN, stale filler; Siri held through the phrase, then the remote's ~1 s tail
           with Siri up and the end marker) -> HCI monitor frames (each value in four 27-byte ACL fragments, a gamepad's
           traffic between them) -> hcitap's parser -> siri_audio.Stream -> decode -> WAV -> whisper
  cut13    (b) the same values as hidraw has them (bluetoothd keeps 20 bytes: the TOC and 12 more of NN), each packet
           zero-padded to NN (their best decode, night/siri-audio/CAPTURES.md) -> whisper: the hidraw-only path
  lossK    (d) full with K% of the frames lost on the air -> libopus PLC -> whisper
  --snr N  white noise N dB under every press, before all of the above (piper's voice is cleaner than a remote's mic)
  --capture (c) a btmon -w file (btsnoop, datalink 2001: the monitor format) -> the same parser -> a WAV per stream ->
           whisper, scored against --expect: a line per press, i.e. per stream with at least agent.MIC_HELD frames
           with Siri held, as SparkHome sends to whisper (a tap, a re-grip's blip or a wake-up remnant gets none). A
           link up before the capture began has no connect event in it: here its voice frames name it (offline only;
           the live tap trusts no link it can't tie to the remote's address and encryption)
whisper is sparkhome.json's asr.cli (the agent's fallback), one run per mode; the voice is its tts.cmd (piper). Both live
on the Spark: run it there. Every audio file stays under ~/.local/state/sparkhome/mic-suite/ (0700), never in the repo.

  python3 -I tools/mic_eval.py                                     # src, full, cut13, loss5/10/20, built-in phrases
  python3 -I tools/mic_eval.py --modes full,loss10 --phrases FILE  # a phrase a line
  python3 -I tools/mic_eval.py --capture siri.snoop [--expect siri.txt]
  python3 -I tools/mic_eval.py --selftest                          # offline: framing, parser, codec, WER; no TTS/whisper
Stdlib only: libopus through siri_audio, ffmpeg to resample piper's 22 kHz.
"""
import argparse
import array
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sparkhome import agent, hcitap, shell, siri_audio as sa  # noqa: E402

STATE = Path("~/.local/state/sparkhome/mic-suite").expanduser()
PHRASES = ["open youtube", "play the boys on prime video", "turn the volume down", "what's the weather tomorrow",
           "quit galactic racer", "the quick brown fox jumps over the lazy dog", "search netflix for stranger things",
           "go back home"]
MODES = "src,full,cut13,loss5,loss10,loss20"
ADDR = "02:00:00:00:00:21"  # the remote: the peer its LE Enhanced Connection Complete names
HANDLE, PAD = 512, 50  # its LE connection handle on the Spark (pair.snoop); a DualSense's BR/EDR one
TAIL = 50  # frames the remote still sends after Siri is let go (~1 s), before its end marker


def press(pcm):
    """A phrase's 16 kHz PCM -> the press's: in whole frames, then the TAIL the remote sends after Siri is let go."""
    return pcm + bytes(-len(pcm) % (2 * sa.FRAME) + 2 * sa.FRAME * TAIL)


def values(pcm, rng):
    """A press's PCM -> the remote's values for it: a frame per 20 ms, Siri held but for the last TAIL, then the end
    marker 10 00."""
    s = array.array("h", pcm + bytes(-len(pcm) % (2 * sa.FRAME)))
    held, out = len(s) // sa.FRAME - TAIL, []
    for q, p in enumerate(sa.encode(s.tobytes())):
        f = s[q * sa.FRAME:(q + 1) * sa.FRAME]
        out.append(sa.value(q, p, 0x10 if q < held else 0, int(math.sqrt(sum(x * x for x in f) / sa.FRAME)),
                            rng.randbytes(94)))
    return out + [b"\x10\x00"]


def _event(code, prm):
    """An HCI event as the monitor channel shows it: le16 opcode 3, index 0 (hci0), length; code, length, parameters."""
    return struct.pack("<HHHBB", 3, 0, 2 + len(prm), code, len(prm)) + prm


def monitor(vals, rng):
    """The values as the HCI monitor channel shows them: the remote's LE Enhanced Connection Complete and Encryption
    Change, each value an ATT notification on 0x0023 in 27-byte ACL fragments, a DualSense report (BR/EDR) between them
    now and then, and a Disconnection Complete."""
    out = [_event(0x3E, bytes([0x0A, 0]) + struct.pack("<H", HANDLE) + b"\0\0" +
                  bytes.fromhex(ADDR.replace(":", ""))[::-1] + bytes(12) + struct.pack("<HHHB", 8, 0, 42, 0)),
           _event(0x08, b"\0" + struct.pack("<H", HANDLE) + b"\x01")]
    for v in vals:
        for p in hcitap.acl_rx(hcitap.ATT_NOTIFY + v, handle=HANDLE):
            out.append(p)
            if rng.random() < .3:
                out += hcitap.acl_rx(rng.randbytes(79), 0x45, PAD, frag=1021)
    return out + [_event(0x05, b"\0" + struct.pack("<H", HANDLE) + b"\x13")]


class Capture(hcitap.Tap):
    """(c) The tap over a capture: a link it never saw connect (up before btmon started) is the remote's, encrypted,
    from its first voice frame. Offline only: the live tap drops such a link's frames."""

    def _ours(self, key, start):
        if key not in self.link and start is not None and hcitap.voice_start(start):
            self.link[key] = True
            self.enc.add(key)
        return super()._ours(key, start)


def tap(packets, remote=ADDR, kind=hcitap.Tap):
    """Monitor packets -> the values sparkhome/hcitap.py's Tap forwards (the remote's voice frames and end markers)."""
    t = kind(remote)
    return [v for p in packets if (v := t.packet(p)) is not None]


def streams(vals):
    """Values in arrival order -> a siri_audio.Stream per stream the remote sent (a frame its stream refuses starts the
    next), each with .held: its frames with Siri held."""
    out = []
    for v in vals:
        if not (out and out[-1].add(v)):
            s = sa.Stream()
            if not s.add(v):
                continue
            s.held = 0
            out.append(s)
        out[-1].held += len(v) == 101 and bool(v[1] & 0x10)
    return out


def cut13(vals):
    """The packets a hidraw reader has: value[:20] is all bluetoothd keeps, so the TOC and 12 more bytes of NN, here
    zero-padded to NN."""
    return [p + bytes(v[6] - len(p)) for v in vals if len(v) == 101 for p in (v[7:7 + v[6]][:13],)]


def render(mode, pcm, rng, wav):
    """One phrase's 16 kHz PCM through one mode -> wav, and what happened to its frames."""
    if mode == "src":
        sa.write_wav(wav, pcm)
        return {}
    vals = values(pcm, rng)
    if mode == "cut13":
        pk = cut13(vals)
    else:
        loss = int(mode[4:]) / 100 if mode.startswith("loss") else 0
        got = streams(tap(monitor([v for v in vals if len(v) == 2 or rng.random() >= loss], rng)))
        assert len(got) == 1 and got[0].done, f"{mode}: {len(got)} streams"
        pk = got[0].packets()
    sa.write_wav(wav, sa.decode(pk))
    nn = [v[6] for v in vals if len(v) == 101 and v[1]]  # (with Siri held: the tail's silence makes tiny packets)
    return {"frames": len(pk), "lost": pk.count(None), "nn_held": round(sum(nn) / len(nn), 1)}


def words(text):
    return [w.strip("'") for w in re.sub(r"[^a-z0-9']+", " ", text.lower()).split() if w.strip("'")]


def wer(ref, hyp):
    """-> (word edits, words in ref): Levenshtein over words, case and punctuation ignored."""
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i, a in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, b in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (a != b))
    return d[-1], len(r)


def _dir(p):
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)
    return p


def noisy(pcm, snr, rng):
    """pcm plus white noise snr dB below its RMS: a real mic's floor (TTS is cleaner than any remote). The RMS is the
    whole press's, its pauses and tail included, so while the phrase is said the SNR is a few dB higher."""
    s = array.array("h", pcm)
    sd = math.sqrt(sum(x * x for x in s) / max(len(s), 1)) / 10 ** (snr / 20)
    return array.array("h", (max(-32768, min(32767, round(x + rng.gauss(0, sd)))) for x in s)).tobytes()


def run(phrases, modes, say, hear, out, seed=1, snr=None):
    """say: text -> 16 kHz PCM; hear: [wav] -> [text]. A WAV per phrase and mode under out (each the phrase and the
    remote's 1 s tail), one hear() a mode; snr: dB of white noise under it all. -> {mode: {"wer", "exact", "rows"}}"""
    rng, res = random.Random(seed), {}
    pcm = {t: press(say(t)) for t in phrases}
    pcm = {t: noisy(p, snr, rng) for t, p in pcm.items()} if snr is not None else pcm
    for m in modes:
        d, rng = _dir(out / m), random.Random(seed)
        rows = [{"phrase": t, "wav": str(d / f"{i:02d}.wav")} for i, t in enumerate(phrases)]
        for r in rows:
            r.update(render(m, pcm[r["phrase"]], rng, r["wav"]))
        for r, h in zip(rows, hear([r["wav"] for r in rows])):
            r["heard"] = h
            r["edits"], r["words"] = wer(r["phrase"], h)
        res[m] = {"wer": round(sum(r["edits"] for r in rows) / sum(r["words"] for r in rows), 4),
                  "exact": sum(not r["edits"] for r in rows), "rows": rows}
        print(f"{m:7} WER {res[m]['wer']:6.1%}  exact {res[m]['exact']}/{len(rows)}", flush=True)
        for r in rows:
            lost = f" lost {r['lost']}/{r['frames']}" if r.get("lost") else ""
            print(f"    {r['edits']}/{r['words']}  {r['phrase']!r} -> {r['heard']!r}{lost}", flush=True)
    return res


def capture(path, expect, hear, out, remote=ADDR):
    """(c) A btmon capture -> a WAV per stream under out -> whisper; expect's lines in order go to the presses (streams
    with at least agent.MIC_HELD frames with Siri held: what SparkHome sends to whisper)."""
    exp = iter(Path(expect).read_text().splitlines() if expect else [])
    rows = []
    for i, s in enumerate(streams(tap((p for _, p in hcitap.btsnoop(path)), remote, Capture))):
        pk = s.packets()
        rows.append({"wav": str(out / f"stream-{i + 1:02d}.wav"), "frames": len(pk), "lost": pk.count(None),
                     "held": s.held, "ended": s.done})
        sa.write_wav(rows[-1]["wav"], sa.decode(pk))
    for i, (r, h) in enumerate(zip(rows, hear([r["wav"] for r in rows]) if rows else [])):
        r["heard"] = h
        if r["held"] >= agent.MIC_HELD and (e := next(exp, None)) is not None:
            r["expected"] = e
            r["edits"], r["words"] = wer(e, h)
        print(f"stream {i + 1}: {r['frames']} frames, {r['held']} with Siri held, {r['lost']} lost -> {h!r}"
              + (f"  {r['edits']}/{r['words']}" if "edits" in r else "" if r["held"] >= agent.MIC_HELD else "  (a tap)"),
              flush=True)
    return rows


def piper(tts, cache):
    """say(): settings.tts.cmd (piper, the text on stdin, as the agent speaks) -> 16 kHz mono PCM; cached by text."""
    def say(text):
        wav = cache / (hashlib.sha256(json.dumps([tts["cmd"], text]).encode()).hexdigest()[:16] + ".wav")
        if not wav.exists():
            part = f"{wav}.part"
            p = subprocess.run([x.replace("{out}", part) for x in tts["cmd"]], input=text.encode(),
                               capture_output=True, timeout=120)
            if p.returncode:
                raise SystemExit(f"tts failed: {p.stderr.decode(errors='replace')[-500:]}")
            os.replace(part, wav)
        return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(wav), "-ar", str(sa.RATE), "-ac", "1",
                               "-f", "s16le", "-"], capture_output=True, check=True, timeout=60).stdout
    return say


def whisper(asr):
    """hear(): settings.asr.cli (whisper-cli) over every WAV at once (one model load), -otxt beside each."""
    def hear(wavs):
        env = {**os.environ, **{k: str(v) for k, v in (asr.get("env") or {}).items()}}
        p = subprocess.run([*asr["cli"], "-otxt", *wavs], env=env, capture_output=True, text=True,
                           timeout=60 + 10 * len(wavs))
        if p.returncode:
            raise SystemExit(f"whisper-cli failed: {p.stderr[-500:]}")
        return [agent._clean(Path(w + ".txt").read_text()) for w in wavs]
    return hear


def _selftest():
    assert wer("open youtube", "Open YouTube.") == (0, 2) and wer("what's the weather tomorrow", "What's the weather "
                                                                  "tomorrow?") == (0, 4)
    assert wer("turn the volume down", "Turn the volume up.") == (1, 4) and wer("go back home", "go home") == (1, 3)
    assert wer("quit galactic racer", "Quit Galactic Racer, quit!") == (1, 3) and wer("a", "") == (1, 1)
    assert words(" 'Play' -- The Boys' ") == ["play", "the", "boys"]
    rng = random.Random(1)
    vals = [sa.value(q, bytes([sa.TOC, q]) * 40, level=q) for q in range(30)] + [b"\x10\x00"]
    mon = monitor(vals, rng)  # four fragments a value, other traffic between, events at both ends
    assert tap(mon) == vals and len(mon) > 4 * 30 + 2 and {struct.unpack_from("<H", f)[0] for f in mon} == {3, 5}
    assert tap(mon, "AA:BB:CC:DD:EE:FF") == []  # (another remote's link: hcitap drops it)
    assert tap(mon[2:]) == [] and tap(mon[2:], kind=Capture) == vals  # no connect: hcitap drops it, a capture's shim not

    def write_snoop(path, frames):  # monitor packets as btmon -w writes them
        path.write_bytes(b"btsnoop\0" + struct.pack(">II", 1, 2001) + b"".join(
            struct.pack(">IIIIq", len(m) - 6, len(m) - 6, struct.unpack_from("<H", m)[0], 0, 0) + m[6:] for m in frames))
    with tempfile.TemporaryDirectory() as t:
        f = Path(t, "c.snoop")  # the same frames in btmon -w's file format: back as they were
        write_snoop(f, mon)
        assert [p for _, p in hcitap.btsnoop(f)] == mon
        # two presses, the second without an end marker before it: two streams
        assert [len(s.packets()) for s in streams(vals + vals[:5] + vals[7:12])] == [30, 12]
        assert cut13(vals[:1]) == [vals[0][7:20] + bytes(80 - 13)] and cut13(vals[30:]) == []
        try:
            sa._opus()
        except RuntimeError as e:
            return print(f"mic_eval: {e}; codec checks skipped")
        tone = array.array("h", (int(9000 * math.sin(2 * math.pi * 330 * i / sa.RATE)) for i in range(5000))).tobytes()
        v = values(press(tone), rng)  # 5000 samples = 15.6 frames: 16 held, the tail, the end marker
        assert len(v) == 16 + TAIL + 1 and v[-1] == b"\x10\x00" and all(len(x) == 101 for x in v[:-1])
        p = [sa.parse(x) for x in v[:-1]]
        assert [x[0] for x in p] == list(range(16 + TAIL)) and [x[1] for x in p] == [0x10] * 16 + [0] * TAIL
        assert 6000 < p[3][2] < 6800 and p[-1][2] == 0  # level: the frame's RMS (9000 / sqrt 2)
        n = array.array("h", noisy(tone, 0, rng))  # 0 dB: the noise has the tone's power
        assert len(n) == 5000 and 1.8 < sum(x * x for x in n) / sum(x * x for x in array.array("h", tone)) < 2.2
        # the whole suite on a tone with a stand-in voice and ears: a WAV per phrase and mode, scored
        out, said = _dir(Path(t, "run")), {"open youtube": tone, "go back home": tone[:3200]}
        heard = iter([["open youtube", "go back home"], ["Open YouTube.", "go home"], ["", "go back home"],
                      ["open youtube", "go back home!"]])
        with contextlib.redirect_stdout(io.StringIO()):  # (the table it prints)
            res = run(list(said), ["src", "full", "cut13", "loss20"], said.get, lambda w: next(heard), out)
        assert [res[m]["wer"] for m in res] == [0, .2, .4, 0] and res["full"]["exact"] == 1, res
        full, lossy = res["full"]["rows"][0], res["loss20"]["rows"][0]
        assert full["frames"] == 16 + TAIL and not full["lost"] and 0 < lossy["lost"] and lossy["frames"] <= full["frames"]
        with wave.open(full["wav"]) as w:
            assert (w.getframerate(), w.getnframes()) == (sa.RATE, (16 + TAIL) * sa.FRAME)
        assert out.stat().st_mode & 0o777 == Path(full["wav"]).parent.stat().st_mode & 0o777 == 0o700
        # (c): a capture (begun with the remote connected: no connect event) of a re-grip's blip (5 frames held, then
        # 10 10) and two presses -> a WAV per stream; the expected lines go to the presses, not to the blip
        write_snoop(f, monitor(v[:5] + [b"\x10\x10"] + v + v[:20], rng)[2:])
        Path(t, "c.txt").write_text("open youtube\ngo home\n")
        with contextlib.redirect_stdout(io.StringIO()):
            rows = capture(f, Path(t, "c.txt"), lambda w: ["Open YouTube"] * len(w), out)
        assert [(r["frames"], r["held"], r["ended"], r.get("edits")) for r in rows] == \
            [(5, 5, True, None), (16 + TAIL, 16, True, 0), (20, 16, False, 2)], rows


def main(argv=None):
    p = argparse.ArgumentParser(prog="tools/mic_eval.py", description=__doc__.split("\n")[0])
    p.add_argument("--modes", default=MODES, help=f"comma-separated: src, full, cut13, lossK (default {MODES})")
    p.add_argument("--phrases", help="a file of phrases, one a line (default: 8 built-in commands)")
    p.add_argument("--capture", metavar="SNOOP", help="(c) score a btmon -w capture instead")
    p.add_argument("--expect", metavar="TXT", help="with --capture: the words said, a line per press")
    p.add_argument("--remote", default=ADDR, help=f"with --capture: the remote's address (default {ADDR})")
    p.add_argument("--config", help="sparkhome.json for asr and tts (default: as SparkHome finds it)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--snr", type=float, help="add white noise this many dB below each press's RMS (default: none)")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args(argv)
    if a.selftest:
        logging.disable(logging.CRITICAL)  # (hcitap warns about the other remote's link the checks make on purpose)
        _selftest()
        return print("mic_eval selftest OK")
    modes = a.modes.split(",")
    bad = [m for m in modes if m not in ("src", "full", "cut13") and not re.fullmatch(r"loss\d{1,2}", m)]
    if bad:
        p.error(f"unknown modes {bad}")
    settings = shell.expand(shell.load_config(a.config)["settings"])
    if not (settings.get("asr") or {}).get("cli"):
        p.error("the config has no asr.cli (whisper-cli)")
    out = _dir(_dir(STATE) / time.strftime("%Y%m%d-%H%M%S"))
    if a.capture:
        res = {"capture": capture(a.capture, a.expect, whisper(settings["asr"]), out, a.remote)}
    else:
        if not (settings.get("tts") or {}).get("cmd"):
            p.error("the config has no tts.cmd (piper)")
        phrases = Path(a.phrases).read_text().splitlines() if a.phrases else PHRASES
        phrases = [t for t in phrases if t.strip()]
        res = {"seed": a.seed, "snr": a.snr, "modes": run(phrases, modes, piper(settings["tts"], _dir(STATE / "tts")),
                                                          whisper(settings["asr"]), out, a.seed, a.snr)}
    (out / "results.json").write_text(json.dumps(res, indent=1))
    print(f"-> {out / 'results.json'}")


if __name__ == "__main__":
    main()
