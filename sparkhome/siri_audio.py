"""The Siri Remote's microphone: the audio values hcitap.py forwards -> 16 kHz mono PCM -> a WAV for whisper.

While Siri is held, and about a second after (~50 more frames), the remote (gen 1, A1962) sends a notification on ATT
handle 0x0023 (HID report 0xFC) every 20 ms. The value is 101 bytes: [0] 0x10 (audio), [1] the buttons (0x10: Siri
held), [2:4] a level (LE16, about the frame's RMS), [4:6] seq (LE16: 0 at each press, +1 a frame), [6] NN, the Opus
packet's length (67-94 seen; 94 = 101 - 7 is the cap), [7:7+NN] the packet, then stale filler. TOC 0xB8: CELT-only,
wideband, 20 ms, mono, one frame. A 2-byte 10 00 ends the stream (10 10 when Siri was pressed again), not always.
bluetoothd keeps 20 bytes of each value (the remote asks for ATT MTU 23, then sends 104-byte PDUs), so hidraw only ever
has 13 bytes of a packet: whole ones exist only below it, on the HCI monitor channel (hcitap.py). Decoding is libopus
through ctypes at 16 kHz, 320 samples a frame; a lost frame is libopus's concealment (PLC). Stdlib only.
Self-check: python3 -m sparkhome.siri_audio --selftest
"""
import ctypes
import ctypes.util
import sys
import wave

RATE, FRAME = 16000, 320  # what whisper takes; 20 ms
TOC = 0xB8
MAX_FRAMES = 1500  # 30 s (the agent's MAX_REC_S): the most one stream keeps, 150 KB of packets
_lib = None


def parse(v):
    """A value from handle 0x0023 -> (seq, buttons, level, Opus packet); the end marker -> (None, buttons, 0, b"");
    anything else (touch, buttons, the unlock's reply, a value cut short) -> None."""
    if len(v) == 2 and v[0] == 0x10:
        return None, v[1], 0, b""
    if len(v) > 7 and v[0] == 0x10 and v[7] == TOC and 0 < v[6] <= len(v) - 7:
        return v[4] | v[5] << 8, v[1], v[2] | v[3] << 8, bytes(v[7:7 + v[6]])
    return None


class Stream:
    """One press's audio: values in arrival order -> its packets by seq, None where one was lost. It is over (done) at
    the end marker, at a seq that doesn't go up (the next press: seq restarts at 0, and that frame may be the lost one;
    one L2CAP channel never reorders, so it is never a late frame) or past MAX_FRAMES."""

    def __init__(self):
        self.seqs, self.pkts, self.done = [], [], False

    def add(self, v):
        """-> True when v was this stream's (a frame kept, or its end). False: not audio, or this stream is over; a
        frame refused at a restart is the next Stream's first."""
        p = None if self.done else parse(v)
        if p is None:
            return False
        seq, pkt = p[0], p[3]
        if seq is None:  # an end marker before any frame is the last press's
            self.done = bool(self.seqs)
            return self.done
        if self.seqs and (seq <= self.seqs[-1] or seq - self.seqs[0] >= MAX_FRAMES):
            self.done = True
            return False
        self.seqs.append(seq)
        self.pkts.append(pkt)
        return True

    def packets(self):
        """From the first frame kept to the last, in seq order; None for each one missing."""
        out = [None] * (self.seqs[-1] - self.seqs[0] + 1) if self.seqs else []
        for s, p in zip(self.seqs, self.pkts):
            out[s - self.seqs[0]] = p
        return out


def _opus():
    """libopus with the prototypes used here, loaded on first use. RuntimeError (a message fit for the TV) without it."""
    global _lib
    if _lib is None:
        name = "libopus.so.0" if sys.platform.startswith("linux") else ctypes.util.find_library("opus") or "libopus"
        vp, i, i32, P = ctypes.c_void_p, ctypes.c_int, ctypes.c_int32, ctypes.POINTER
        try:
            lib = ctypes.CDLL(name)
            for fn, res, args in (
                    ("opus_decoder_create", vp, [i32, i, P(i)]), ("opus_decoder_destroy", None, [vp]),
                    ("opus_decode", i, [vp, ctypes.c_char_p, i32, P(ctypes.c_int16), i, i]),
                    ("opus_encoder_create", vp, [i32, i, i, P(i)]), ("opus_encoder_destroy", None, [vp]),
                    ("opus_encode", i32, [vp, P(ctypes.c_int16), i, ctypes.c_char_p, i32]),
                    ("opus_encoder_ctl", i, [vp, i])):  # (variadic: the fixed arguments only)
                f = getattr(lib, fn)
                f.restype, f.argtypes = res, args
        except (OSError, AttributeError) as e:
            raise RuntimeError("The Siri Remote's audio needs libopus (apt install libopus0).") from e  # (the TV shows it)
        _lib = lib
    return _lib


def decode(packets):
    """Opus packets (None: lost) -> 16 kHz mono 16-bit PCM, 320 samples each. A lost packet, or one libopus refuses,
    becomes its concealment (PLC), so the timing stays whole."""
    lib, err = _opus(), ctypes.c_int()
    dec = lib.opus_decoder_create(RATE, 1, ctypes.byref(err))
    if err.value or not dec:
        raise RuntimeError(f"libopus can't make a decoder ({err.value})")
    buf, out = (ctypes.c_int16 * FRAME)(), bytearray()
    try:
        for p in packets:
            n = lib.opus_decode(dec, p, len(p), buf, FRAME, 0) if p else -1
            if n < 0:
                n = lib.opus_decode(dec, None, 0, buf, FRAME, 0)
            out += ctypes.string_at(buf, 2 * n) if n > 0 else bytes(2 * FRAME)
    finally:
        lib.opus_decoder_destroy(dec)
    return bytes(out)


def encode(pcm, bitrate=32000, cap=94):
    """(tests: the selftest, tools/mic_eval.py) 16 kHz mono 16-bit PCM -> Opus packets as the remote makes them: CELT-only
    (RESTRICTED_LOWDELAY) wideband 20 ms frames, TOC 0xB8, VBR around `bitrate` (the remote's NN averages 75-80 bytes:
    30-32 kbps), at most `cap` bytes. The last frame is padded with silence."""
    lib, err = _opus(), ctypes.c_int()
    enc = lib.opus_encoder_create(RATE, 1, 2051, ctypes.byref(err))  # OPUS_APPLICATION_RESTRICTED_LOWDELAY
    if err.value or not enc:
        raise RuntimeError(f"libopus can't make an encoder ({err.value})")
    pcm = bytes(pcm) + bytes(-len(pcm) % (2 * FRAME))
    buf, out = ctypes.create_string_buffer(cap), []
    try:
        lib.opus_encoder_ctl(enc, 4002, ctypes.c_int32(bitrate))  # OPUS_SET_BITRATE_REQUEST (VBR is the default)
        for i in range(0, len(pcm), 2 * FRAME):
            n = lib.opus_encode(enc, (ctypes.c_int16 * FRAME).from_buffer_copy(pcm, i), FRAME, buf, cap)
            if n < 0:
                raise RuntimeError(f"libopus can't encode ({n})")
            out.append(buf.raw[:n])
    finally:
        lib.opus_encoder_destroy(enc)
    return out


def write_wav(path, pcm):
    """16 kHz mono 16-bit PCM -> a WAV file, as arecord leaves one for whisper."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def value(seq, pkt, buttons=0x10, level=0, filler=b"\xee" * 94):
    """(tests) A 101-byte audio value in the remote's layout: header, packet, stale filler."""
    return bytes([0x10, buttons, level & 255, level >> 8 & 255, seq & 255, seq >> 8 & 255, len(pkt)]) + pkt + \
        filler[:94 - len(pkt)]


def _selftest():
    import array
    import math
    import os
    import tempfile
    pkt = bytes([TOC]) + bytes(range(1, 80))
    v = value(0x105, pkt, level=0x1234)
    assert len(v) == 101 and parse(v) == (0x105, 0x10, 0x1234, pkt), parse(v)
    assert parse(b"\x10\x00") == (None, 0, 0, b"") and parse(b"\x10\x10") == (None, 0x10, 0, b"")
    assert parse(v[:20]) is None  # what reaches hidraw: bluetoothd's 20 bytes hold no whole packet
    assert parse(bytes.fromhex("0100320c340187acef42611584")) is None and parse(b"\xe0\x20\x60\x02") is None  # touch, unlock
    assert parse(v[:7] + b"\xb9" + v[8:]) is None and parse(v[:6] + b"\x00" + v[7:]) is None  # another TOC; NN 0
    assert parse(v[:6] + b"\x5f" + v[7:]) is None and parse(v[:6] + b"\x5e" + v[7:])[3] == v[7:]  # NN past the value
    # a stream: slotted by seq (a gap is None), over at the end marker; the last press's end marker is no start
    s = Stream()
    assert not s.add(b"\x10\x00") and not s.done and not s.add(b"\x01\x00" + bytes(11))
    assert all(s.add(value(q, bytes([TOC, q]))) for q in (0, 1, 3, 4)) and s.add(b"\x10\x00") and s.done
    assert s.packets() == [b"\xb8\x00", b"\xb8\x01", None, b"\xb8\x03", b"\xb8\x04"] and not s.add(value(5, pkt))
    # the next press restarts seq at 0 (here with no end marker between), or at 1 when that frame was lost: the frame
    # isn't this stream's, it is the next one's first
    for first in (0, 1, 7):
        s = Stream()
        assert s.add(value(5, pkt)) and s.add(value(7, pkt)) and not s.add(value(first, pkt)) and s.done
        assert Stream().add(value(first, pkt))
    s = Stream()  # at most MAX_FRAMES
    assert s.add(value(9, pkt)) and s.add(value(8 + MAX_FRAMES, pkt)) and not s.add(value(9 + MAX_FRAMES, pkt))
    assert s.done and len(s.packets()) == MAX_FRAMES
    assert Stream().packets() == []
    try:
        _opus()
    except RuntimeError as e:
        return print(f"siri_audio: {e}; codec checks skipped")
    # the codec: a voiced-like 1 s signal (two harmonics + a sweep) -> packets like the remote's -> values -> Stream ->
    # PCM that matches the source (at libopus's 2.5 ms delay, 40 samples)
    src = array.array("h", (int(6000 * math.sin(2 * math.pi * 220 * t / RATE) + 3000 * math.sin(2 * math.pi * 660 * t / RATE)
                                + 2000 * math.sin(2 * math.pi * (300 + 2.5 * t / 16) * t / RATE)) for t in range(RATE)))
    pk = encode(src.tobytes())
    assert len(pk) == 50 and all(p[0] == TOC and 2 < len(p) <= 94 for p in pk), [(p[0], len(p)) for p in pk]
    s = Stream()
    assert all(s.add(value(q, p, filler=bytes(range(94)))) for q, p in enumerate(pk))
    out = array.array("h", decode(s.packets()))
    assert len(out) == 50 * FRAME

    def corr(a, b, lag=40):
        x, y = a[1600:-1600], b[1600 + lag:len(b) - 1600 + lag]
        return sum(p * q for p, q in zip(x, y)) / math.sqrt(sum(p * p for p in x) * sum(q * q for q in y))
    assert corr(src, out) > .9, corr(src, out)
    # three frames lost and one libopus refuses (code 3, no frames): concealed, so the length and the rest hold
    pk[20:23], pk[30] = [None] * 3, b"\xbb\x00"
    out2 = array.array("h", decode(pk))
    assert len(out2) == len(out) and max(map(abs, out2[20 * FRAME:23 * FRAME])) > 0
    assert out2[:20 * FRAME] == out[:20 * FRAME] and corr(src[:19 * FRAME], out2[:19 * FRAME]) > .9
    d = tempfile.mkdtemp(prefix="sparkhome-siri-audio-")
    try:
        write_wav(os.path.join(d, "a.wav"), out.tobytes())
        with wave.open(os.path.join(d, "a.wav")) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (RATE, 1, 2, len(out))
            assert w.readframes(len(out)) == out.tobytes()
    finally:
        os.unlink(os.path.join(d, "a.wav"))
        os.rmdir(d)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        print("siri_audio selftest OK")
    else:
        sys.exit(__doc__)
