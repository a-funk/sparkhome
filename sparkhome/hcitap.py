"""Siri Remote microphone tap: the gen-1 remote's complete voice frames, read from the HCI monitor, for SparkHome.

The remote (A1962) sends each 20 ms Opus frame as one ATT notification on handle 0x0023 with a 101-byte value, though it
negotiated an ATT MTU of 23. bluetoothd reads every ATT PDU with read(fd, buf, mtu), so hog and hidraw only ever get the
first 20 bytes (13 of the frame's 67-94 Opus bytes). The HCI monitor channel, which btmon reads, has all of them:
binding it needs CAP_NET_RAW.

The monitor shows every Bluetooth packet on the machine (keyboards, pads, pairing keys), so this keeps only the remote's
voice. ACL RX is reassembled per controller and connection handle only while it can still become an ATT (CID 4) Handle
Value Notification (0x1b) on 0x0023 whose value is a voice frame (101 bytes, [0] 0x10, [7] the Opus TOC 0xB8) or an end
marker (2 bytes, [0] 0x10), and is kept only from the remote's link once it is encrypted: the link whose LE Connection
Complete named --remote, or, for one up before the tap started, the kernel's connection list did (links()). A frame's
shape names nobody (it is public), and an address can be borrowed, but the encryption takes the remote's bond. Every
other link's frames, a link of unknown address included, are dropped as they are parsed, never logged or stored. Voice
value: [1] buttons (0x10 Siri), [2:4] level, [4:6] seq (0 at each press, +1 per frame), [6] Opus packet length NN
(67-94), [7:7+NN] the packet (CELT-only, 16 kHz, mono, 20 ms), then stale filler.

Each kept value goes to every client (at most 4) of the SOCK_SEQPACKET socket /run/user/UID/sparkhome/siri-mic.sock
(0600, in a 0700 directory; peers with this uid only) as one message: the CLOCK_MONOTONIC ns when the PDU completed
(8 bytes, little-endian), then the value. A client that can't take a message at once is dropped; with no client, values
are dropped. Clients never send.

deploy/sparkhome-siri-mic.service runs a root-owned copy (deploy/install.sh root) as the user with CAP_NET_RAW only,
which this gives up once the monitor socket is bound. Linux only; the Tap and its checks run anywhere.
  python3 -m sparkhome.hcitap --remote AA:BB:CC:DD:EE:FF      serve (as the unit does)
  python3 -m sparkhome.hcitap --selftest [FILE.snoop ...]     offline checks; then each btmon -w FILE must yield nothing
"""
import argparse
import logging
import os
import select
import signal
import socket
import struct
import sys
import time

log = logging.getLogger("sparkhome.hcitap")
SOCK = "/run/user/%d/sparkhome/siri-mic.sock"   # % uid
CLIENTS = 4
ATT_NOTIFY = b"\x1b\x23\x00"     # Handle Value Notification on 0x0023: HID report 0xFC (Input), CCCD 0x0024
SIZES = (5, 104)                 # L2CAP payloads kept: ATT_NOTIFY + a 2-byte end marker or a 101-byte voice frame

def voice_start(f):
    """Is f (an ACL start fragment's L2CAP bytes) the first fragment of a voice frame: L2CAP 104 on CID 4, ATT_NOTIFY,
    then a value starting 0x10 with the Opus TOC 0xB8 at [7]?"""
    return len(f) >= 15 and f[:4] == b"\x68\x00\x04\x00" and f[4:7] == ATT_NOTIFY and f[7] == 0x10 and f[14] == 0xB8



class Tap:
    """Monitor packets in, the remote's voice values out (packet()). No I/O: the selftest and tools replay captures."""

    def __init__(self, remote, links=()):
        """links: the LE links up before the monitor was bound, as links() reads them: (index, handle, peer address
        as HCI carries it, encrypted)."""
        self.remote = bytes.fromhex(remote.replace(":", ""))[::-1]   # as HCI events carry it (little-endian)
        if len(self.remote) != 6:
            raise ValueError(f"not a Bluetooth address: {remote}")
        self.part, self.link, self.enc = {}, {}, set()   # (index, handle) -> the L2CAP frame so far; -> True: the
        for idx, h, addr, enc in links:                   # remote, False: not; the encrypted links
            self.link[idx, h] = addr == self.remote
            if enc:
                self.enc.add((idx, h))
            if addr == self.remote:
                log.info("the remote is connected (hci%d handle %d)%s", idx, h, "" if enc else ", not encrypted yet")

    def packet(self, p):
        """One monitor packet (le16 opcode, controller index, length; the payload) -> the value to forward, or None."""
        if len(p) < 6:
            return None
        op, idx, n = struct.unpack_from("<HHH", p)
        d = p[6:6 + n]
        if op == 5:                                        # HCI_MON_ACL_RX_PKT
            return self._acl(idx, d)
        if op == 3 and len(d) >= 2:                        # HCI_MON_EVENT_PKT: code, length, parameters
            self._event(idx, d[0], d[2:2 + d[1]])
        return None

    def _event(self, idx, code, e):
        if code in (0x08, 0x59) and len(e) >= 4:          # Encryption Change v1, v2: status, handle, enabled
            key = (idx, e[1] | (e[2] & 15) << 8)
            (self.enc.add if e[0] == 0 and e[3] else self.enc.discard)(key)
            return
        if code == 0x3E and len(e) >= 12 and e[0] in (0x01, 0x0A, 0x29) and e[1] == 0:
            # LE Connection Complete / LE Enhanced Connection Complete v1, v2 (same prefix), success: subevent,
            # status, handle, role, peer address type, peer address
            key, remote = (idx, e[2] | (e[3] & 15) << 8), e[6:12] == self.remote
        elif code == 0x03 and len(e) >= 3 and e[0] == 0:   # BR/EDR Connection Complete: never the remote (LE only)
            key, remote = (idx, e[1] | (e[2] & 15) << 8), False
        elif code == 0x05 and len(e) >= 3 and e[0] == 0:   # Disconnection Complete
            key, remote = (idx, e[1] | (e[2] & 15) << 8), None
        else:
            return
        self.part.pop(key, None)
        self.enc.discard(key)
        if self.link.pop(key, None):
            log.info("the remote disconnected (hci%d handle %d)", *key)
        if remote is not None:
            self.link[key] = remote
        if remote:
            log.info("the remote connected (hci%d handle %d)", *key)

    def _acl(self, idx, d):
        if len(d) < 4:
            return None
        h, n = struct.unpack_from("<HH", d)
        key, frag = (idx, h & 0xFFF), d[4:4 + n]
        if not self._ours(key, None if h >> 12 & 3 == 1 else frag):   # nothing of another link is kept, not a fragment
            return None
        buf = self.part.pop(key, None)
        if h >> 12 & 3 != 1:                               # a start: PB 00, 10 or 11, as in l2cap_recv_acldata()
            buf = frag
        elif buf is None:                                  # the rest of something already dropped (or never seen)
            return None
        else:
            buf += frag
        if len(buf) >= 4:
            n, cid = struct.unpack_from("<HH", buf)
            if cid != 4 or n not in SIZES or len(buf) > 4 + n or not ATT_NOTIFY.startswith(buf[4:7]):
                return None                                # not the remote's voice: dropped, and so is its rest
            if len(buf) == 4 + n:
                return self._value(key, buf[7:])
        self.part[key] = buf
        return None

    def _value(self, key, v):
        return None if v[0] != 0x10 or len(v) == 101 and v[7] != 0xB8 else v   # (_acl took only the remote's link)

    def _ours(self, key, start):
        """Is key the remote's encrypted link? For another link's start fragment (start), once per press (a voice
        frame's seq 0, all in its first fragment), say why its voice is dropped (--remote wrong? a lost event?)."""
        if self.link.get(key) and key in self.enc:
            return True
        if start is not None and voice_start(start) and start[11:13] == b"\0\0":
            it = self.link.get(key)
            log.warning("a voice stream on hci%d handle %d dropped: %s", *key, "a link the tap never saw connect"
                        if it is None else "not --remote's link" if it is False else "the link isn't encrypted")
        return False


def acl_rx(payload, cid=4, handle=0x200, index=0, frag=27):
    """(the selftest, tools) An L2CAP frame arriving, as the monitor shows it: ACL RX packets of at most `frag` bytes
    (27 without LE Data Length Extension, as from the remote). A voice frame is acl_rx(ATT_NOTIFY + value)."""
    f = struct.pack("<HH", len(payload), cid) + payload
    return [struct.pack("<HHHHH", 5, index, 4 + len(c), handle | (0x2000 if i == 0 else 0x1000), len(c)) + c
            for i, c in ((i, f[i:i + frag]) for i in range(0, len(f), frag))]


def btsnoop(path):
    """A btmon -w file (btsnoop, datalink 2001: monitor records) -> (Unix time in ns, monitor packet) per record."""
    with open(path, "rb") as f:
        h = f.read(16)
        if h[:8] != b"btsnoop\0" or h[12:16] != struct.pack(">I", 2001):
            raise ValueError(f"{path}: not a btmon -w capture (btsnoop, datalink 2001)")
        while len(r := f.read(24)) == 24:
            _, n, flags, _, ts = struct.unpack(">IIIIq", r)
            d = f.read(n)                                  # flags = index << 16 | monitor opcode
            yield (ts - 0x00DCDDB30F2F8000) * 1000, struct.pack("<HHH", flags & 0xFFFF, flags >> 16, len(d)) + d


def monitor():
    """A socket on the HCI monitor channel (every controller). CPython 3.12's bind() takes only (dev,) for BTPROTO_HCI
    and leaves the channel 0 (raw), so struct sockaddr_hci {family, dev, channel} goes through ctypes. Needs CAP_NET_RAW
    (else EPERM); EINVAL would mean a wrong address."""
    import ctypes
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    addr = struct.pack("=HHH", socket.AF_BLUETOOTH, 0xFFFF, 2)   # HCI_DEV_NONE, HCI_CHANNEL_MONITOR
    if ctypes.CDLL(None, use_errno=True).bind(s.fileno(), addr, len(addr)):
        err = ctypes.get_errno()
        s.close()
        raise OSError(err, f"binding the HCI monitor channel: {os.strerror(err)}")
    return s


def links():
    """The LE links up now, from the kernel's list (HCIGETDEVLIST, then HCIGETCONNLIST per controller, on an unbound raw
    HCI socket: no capability, as hcitool con reads it) -> Tap(links=...): (index, handle, peer address, encrypted).
    Read after the monitor is bound, so every link is in this list or has its connect on the monitor (or both).
    ponytail: 64 links per controller at most; past that one would count as unknown, its voice dropped."""
    import fcntl
    out = []
    with socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI) as s:
        dl = bytearray(struct.pack("=H", 16) + bytes(2 + 16 * 8))   # dev_num, then 16 {u16 dev_id; u32 opt} from 4
        fcntl.ioctl(s, 0x800448D2, dl)                             # HCIGETDEVLIST
        for i in range(struct.unpack_from("=H", dl)[0]):
            dev = struct.unpack_from("=H", dl, 4 + 8 * i)[0]
            cl = bytearray(struct.pack("=HH", dev, 64) + bytes(64 * 16))   # dev_id, conn_num, 64 hci_conn_info
            fcntl.ioctl(s, 0x800448D4, cl)                         # HCIGETCONNLIST
            for j in range(struct.unpack_from("=H", cl, 2)[0]):
                h, addr, kind, _, state, mode = struct.unpack_from("=H6sBBHI", cl, 4 + 16 * j)
                if kind == 0x80 and state == 1:                    # LE_LINK, BT_CONNECTED
                    out.append((dev, h, addr, bool(mode & 4)))     # HCI_LM_ENCRYPT
    return out


def drop_caps():
    """Give up every capability (capset, all sets empty). The bound monitor socket keeps working; the parsing that
    follows then can't open another raw Bluetooth socket or send HCI commands."""
    import ctypes
    head = ctypes.create_string_buffer(struct.pack("=Ii", 0x20080522, 0))   # _LINUX_CAPABILITY_VERSION_3, this thread
    if ctypes.CDLL(None, use_errno=True).capset(head, bytes(24)):            # 2 x {effective, permitted, inheritable}
        err = ctypes.get_errno()
        raise OSError(err, f"capset: {os.strerror(err)}")


def serve(mon, tap, path, uid):
    """Until `mon` closes: read monitor packets and send every value `tap` keeps to each client of `path`."""
    os.makedirs(os.path.dirname(path), 0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    try:
        os.unlink(path)                                    # a killed run's
    except FileNotFoundError:
        pass
    lst, clients = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET), []
    try:
        lst.bind(path)
        os.chmod(path, 0o600)
        lst.listen(CLIENTS)
        while True:
            ready = select.select([lst, mon, *clients], [], [])[0]
            if lst in ready:                               # before the packet below, which a client that's in gets
                c = lst.accept()[0]
                pid, peer, _ = struct.unpack("iII", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if peer != uid or len(clients) >= CLIENTS:
                    log.warning("client pid %d refused: %s", pid,
                                f"uid {peer}" if peer != uid else f"{CLIENTS} already")
                    c.close()
                else:
                    clients.append(c)
                    log.info("client pid %d connected (%d of %d)", pid, len(clients), CLIENTS)
            for c in [c for c in clients if c in ready]:   # clients never send: this is their EOF
                clients.remove(c)
                c.close()
            if mon in ready:
                p = mon.recv(4096)
                if not p:
                    return
                v = tap.packet(p)
                if v is not None and clients:
                    m = struct.pack("<Q", time.monotonic_ns()) + v
                    for c in clients[:]:
                        try:
                            c.send(m, socket.MSG_DONTWAIT)
                        except OSError as e:               # its queue is full (it fell behind), or it's gone
                            log.warning("client dropped: %s", e)
                            clients.remove(c)
                            c.close()
    finally:
        for c in clients:
            c.close()
        lst.close()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m sparkhome.hcitap", description="Serve the gen-1 Siri Remote's voice "
                                "frames from the HCI monitor to SparkHome (needs CAP_NET_RAW, which it then drops).")
    p.add_argument("--remote", metavar="MAC", help="the remote's address ('bluetoothctl devices Paired')")
    p.add_argument("--selftest", nargs="*", metavar="FILE.snoop",
                   help="offline checks; then each btmon -w FILE given must yield nothing (--remote names its links)")
    a = p.parse_args(argv)
    if a.selftest is not None:
        logging.disable(logging.CRITICAL)                  # the checks provoke the warnings on purpose
        _selftest(a.selftest, a.remote or "00:00:00:00:00:00")
        print("selftest OK")
        return
    try:
        Tap(a.remote or "")
    except ValueError:
        p.error(f"--remote MAC is required: {a.remote!r} is not a Bluetooth address")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        mon = monitor()
        drop_caps()
    except OSError as e:
        sys.exit(f"hcitap: {e} (it needs CAP_NET_RAW: deploy/sparkhome-siri-mic.service)")
    tap = Tap(a.remote, links())                           # (after the bind: no link falls between the two)
    path = SOCK % os.getuid()
    log.info("HCI monitor open, capabilities dropped; the remote is %s; serving %s", a.remote, path)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # (so serve() removes its socket)
    serve(mon, tap, path, os.getuid())
    sys.exit("hcitap: the HCI monitor socket closed")      # (a failure: the unit restarts it)


def _selftest(files=(), remote="00:00:00:00:00:00"):
    import ctypes                                          # (imported before the fork below)
    import random
    import re
    import stat
    import tempfile
    import threading
    R, O = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"       # the remote, another device
    END = b"\x10\x00"
    rng = random.Random(1)

    def evt(code, prm, idx=0):
        return struct.pack("<HHHBB", 3, idx, 2 + len(prm), code, len(prm)) + prm

    def le_conn(h, addr, idx=0, sub=0x0A, status=0):      # 19, 31 or 34 parameter bytes, as the spec has them
        return evt(0x3E, bytes([sub, status]) + struct.pack("<H", h) + b"\0\0"
                   + bytes.fromhex(addr.replace(":", ""))[::-1] + bytes({0x01: 7, 0x0A: 19, 0x29: 22}[sub]), idx)

    def disconn(h):
        return evt(0x05, b"\0" + struct.pack("<H", h) + b"\x13")

    def br_conn(h, addr):                                  # status, handle, address, link type ACL, no encryption
        return evt(0x03, b"\0" + struct.pack("<H", h) + bytes.fromhex(addr.replace(":", ""))[::-1] + b"\x01\0")

    def enc(h, on=1, status=0, code=0x08, idx=0):         # Encryption Change (v2, 0x59: a key size more)
        return evt(code, bytes([status]) + struct.pack("<H", h) + bytes([on]) + (b"\x10" if code == 0x59 else b""), idx)

    def voice(seq, nn=80):
        v = b"\x10\x10" + struct.pack("<HH", 2260, seq) + bytes([nn, 0xB8]) + rng.randbytes(nn - 1)
        return v + rng.randbytes(101 - len(v))             # stale filler

    def notify(v, handle=0x200, idx=0, frag=27, head=ATT_NOTIFY, cid=4):
        return acl_rx(head + v, cid, handle, idx, frag)

    def run(t, pkts):
        return [v for p in pkts if (v := t.packet(p)) is not None]
    # a real frame's first 20 bytes (hidraw lab capture, 2026-10-08: seq 0, NN 91), then filler: 108 bytes of L2CAP
    # in 4 x 27-byte fragments, as on the air (LE without Data Length Extension)
    v0 = bytes.fromhex("1010d40800005bb8547c39a6fa230fe54df33c76") + rng.randbytes(81)
    frames = [voice(i) for i in range(4)]
    assert len(v0) == 101 and [len(p) for p in notify(v0)] == [6 + 4 + 27] * 4
    t = Tap(R)
    assert run(t, [le_conn(0x200, R), enc(0x200), *notify(v0), *[p for v in frames[:3] for p in notify(v)],
                   *notify(END)]) == [v0, *frames[:3], END]
    assert t.link == {(0, 0x200): True} and t.enc == {(0, 0x200)} and not t.part
    # interleaved with another device (its address known, its link encrypted) sending a look-alike on 0x0023 and a link
    # nobody announced sending a 104-byte frame on a dynamic channel: only the remote's frame comes out, whole
    run(t, [le_conn(0x201, O), enc(0x201)])
    mixed = zip(notify(frames[0]), notify(frames[1], 0x201), acl_rx(bytes(100), 0x40, 0x202))
    assert run(t, [p for trio in mixed for p in trio]) == [frames[0]]
    assert run(t, [*notify(END, 0x201), *notify(frames[1], 0x201, frag=251)]) == []
    # from the remote, everything that isn't its voice: other channels, opcodes and handles; what hidraw gets (20
    # bytes); touch and button reports; another kind; no TOC 0xB8; 102 bytes; and its frames as sent (ACL TX)
    junk = [notify(frames[0], cid=6), notify(frames[0], head=b"\x1d\x23\x00"), notify(frames[0], head=b"\x1b\x28\x00"),
            notify(frames[0][:22], head=b"\x0b"), notify(frames[0][:20]), notify(b"\x00\x10"),
            notify(bytes.fromhex("0100320c340187acef42611584")), notify(b"\x11" + frames[0][1:]),
            notify(frames[0][:7] + b"\xb9" + frames[0][8:]), notify(frames[0] + b"\0"),
            [b"\x04" + p[1:] for p in notify(frames[0])]]
    assert run(t, [p for j in junk for p in j]) == [] and not t.part
    # fragment sizes: one fragment (with Data Length Extension), and 3-byte ones that split the L2CAP header; a start
    # flagged PB 00 or 11 is a start too, as in the kernel
    one = notify(frames[2], frag=251)[0]
    pb00_11 = [one[:7] + bytes([one[7] & 0xCF | pb << 4]) + one[8:] for pb in (0, 3)]
    assert run(t, [*notify(frames[0], frag=251), *notify(frames[1], frag=3), *pb00_11]) == [*frames[:3], frames[2]]
    # a new start replaces an unfinished frame; a too-long frame, a rest without its start and malformed packets are
    # dropped; the remote's next frame still comes out
    l2 = struct.pack("<HH", 104, 4) + ATT_NOTIFY + frames[0] + b"xx"
    over = [struct.pack("<HHHHH", 5, 0, 4 + len(c), 0x200 | f, len(c)) + c
            for f, c in ((0x2000, l2[:55]), (0x1000, l2[55:]))]
    bad = [b"", b"\x05\0\0\0", struct.pack("<HHH", 5, 0, 50) + bytes(10), struct.pack("<HHH", 5, 0, 2) + b"\0\x02",
           *[p[:-5] for p in notify(frames[2])], *over, *notify(frames[2])[1:], evt(0x3E, b"\x0a\0"), evt(0x05, b""),
           b"\x03\0\0\0\x01\0\x3e", struct.pack("<HHH", 5, 0, 4) + struct.pack("<HH", 0x200, 9)]
    assert run(t, [*notify(frames[0])[:2], *notify(frames[1]), *bad, *notify(frames[3])]) == [frames[1], frames[3]]
    assert run(t, [*over, *notify(frames[2])[1:]]) == [] and all(len(b) <= 108 for b in t.part.values())   # bounded
    # the remote's link drops mid-frame: the rest after it reconnects doesn't complete that frame; another device that
    # gets its handle is not the remote; the remote back on a new handle is, from its next whole frame
    assert run(t, [*notify(frames[0])[:2], disconn(0x200), le_conn(0x200, R), enc(0x200),
                   *notify(frames[0])[2:]]) == []
    rest = notify(frames[2], 0x203)[1:]
    assert run(t, [disconn(0x200), le_conn(0x200, O), enc(0x200), *notify(frames[1]), *notify(END),
                   le_conn(0x203, R), enc(0x203), *rest]) == []
    assert run(t, [*notify(frames[2], 0x203), *notify(END, 0x203)]) == [frames[2], END]
    assert t.link == {(0, 0x200): False, (0, 0x201): False, (0, 0x203): True}
    # gone, its handle is nobody's: an end marker there (a link whose connect the tap missed) isn't the remote's
    assert run(t, [disconn(0x203), *notify(END, 0x203)]) == [] and (0, 0x203) not in t.link.keys() | t.enc
    # its link isn't encrypted until the Encryption Change (v1 or v2) that turns it on: a borrowed address can't make
    # one; a failed one, or encryption off, drops its frames again; so does a reconnect until its own
    t = Tap(R)
    assert run(t, [le_conn(0x200, R), *notify(frames[0]), enc(0x200, status=0x06), *notify(frames[1]),
                   enc(0x200, code=0x59), *notify(frames[2]), enc(0x200, on=0), *notify(frames[3]), enc(0x200),
                   *notify(END), disconn(0x200), le_conn(0x201, R), *notify(frames[0], 0x201), enc(0x201, idx=1),
                   *notify(frames[1], 0x201), enc(0x201), *notify(frames[2], 0x201)]) == [frames[2], END, frames[2]]
    # a link up before the tap started has no connect on the monitor: unknown, its voice is dropped, however well
    # forged (the signature names nobody), unless the kernel's list (links=) names it the remote's, and encrypted
    forged = b"\x10\x10\xff\x7f\0\0\x50\xb8" + bytes(93)
    t = Tap(R, [(0, 0x41, bytes.fromhex("01eeddccbbaa"), True), (0, 0x42, bytes.fromhex("02eeddccbbaa"), True),
                (0, 0x43, bytes.fromhex("01eeddccbbaa"), False)])
    assert t.link == {(0, 0x41): True, (0, 0x42): False, (0, 0x43): True} and t.enc == {(0, 0x41), (0, 0x42)}
    assert run(t, [*notify(forged, 0x40), *notify(frames[0], 0x40), *notify(END, 0x40), *notify(forged, 0x42),
                   *notify(frames[1], 0x43), *notify(frames[2], 0x41), enc(0x43), *notify(frames[3], 0x43)]) == \
        [frames[2], frames[3]] and (0, 0x40) not in t.link and not t.part
    run(t, notify(forged, 0x42)[:1] + notify(forged, 0x40)[:1])   # another device's start fragment: nothing of it kept
    assert not t.part, t.part
    # LE Connection Complete and Enhanced v2 count too; a failed connect makes no link; a BR/EDR link is never the
    # remote; links on two controllers with the same handle are told apart
    t = Tap(R)
    run(t, [le_conn(0x10, R, sub=0x01), le_conn(0x11, R, sub=0x29), le_conn(0x12, R, status=0x3E), br_conn(0x13, O),
            le_conn(0x200, R), le_conn(0x200, O, idx=1), *(enc(h) for h in (0x11, 0x13)), enc(0x200, idx=1)])
    assert t.link == {(0, 0x10): True, (0, 0x11): True, (0, 0x13): False, (0, 0x200): True, (1, 0x200): False}
    assert run(t, [*notify(frames[0], 0x13), *notify(frames[1], idx=1), *notify(frames[2], 0x11)]) == [frames[2]]
    # random packets, many shaped like the real thing: nothing raises, and only voice-shaped values come out
    t = Tap(R)
    run(t, [le_conn(0x200, R), le_conn(0x201, O), enc(0x200), enc(0x201)])
    shapes = [ATT_NOTIFY + frames[0], ATT_NOTIFY + END, ATT_NOTIFY + frames[0][:20], b"\x1b\x23\x00\x00\x10"]
    for i in range(20000):
        k = rng.random()
        if k < .6:
            f = struct.pack("<HH", rng.choice((5, 104, 104, rng.randrange(300))), rng.choice((4, 4, 4, 6)))
            f += rng.choice(shapes)
            j = rng.randrange(len(f))
            c = bytearray(f[j:j + rng.randrange(1, 60)])
            if rng.random() < .3:
                c[rng.randrange(len(c))] = rng.randrange(256)
            h = rng.choice((0x200, 0x201, 0x40)) | rng.randrange(4) << 12
            p = struct.pack("<HHHHH", 5, rng.randrange(2), 4 + len(c), h, len(c)) + c
        elif k < .8:
            prm = bytes([rng.choice((1, 0x0A, 0))]) + rng.randbytes(rng.randrange(20))
            p = evt(rng.choice((0x3E, 0x05, 0x03, 0x08, 0x59)), prm)
        else:
            p = rng.randbytes(rng.randrange(40))
        v = t.packet(p)
        assert v is None or v[0] == 0x10 and (len(v) == 2 or len(v) == 101 and v[7] == 0xB8), (i, p.hex(), v)
    assert run(t, [le_conn(0x300, R), enc(0x300), *notify(frames[0], 0x300)]) == [frames[0]]
    # btmon -w files: the same packets back, with their time
    with tempfile.TemporaryDirectory() as tmp:
        f = os.path.join(tmp, "t.snoop")
        recs = [le_conn(0x200, R), enc(0x200), *notify(v0), *notify(END)]
        with open(f, "wb") as o:
            o.write(b"btsnoop\0" + struct.pack(">II", 1, 2001))
            for i, p in enumerate(recs):
                op, idx, n = struct.unpack_from("<HHH", p)
                o.write(struct.pack(">IIIIq", n, n, idx << 16 | op, 0, 0x00DCDDB30F2F8000 + 1_000_000 + i) + p[6:])
        got = list(btsnoop(f))
        assert [p for _, p in got] == recs and got[3][0] == 1_000_003_000
        assert run(Tap(R), [p for _, p in got]) == [v0, END]
        with open(f, "r+b") as o:
            o.write(b"btsnoop\0" + struct.pack(">II", 1, 1002))   # an H4 capture, not btmon's
        try:
            next(btsnoop(f))
            raise AssertionError("read a non-monitor btsnoop file")
        except ValueError:
            pass
    if sys.platform.startswith("linux"):
        # capset's call shape: as a user without capabilities it drops nothing, but a wrong header is EINVAL
        pid = os.fork()
        if pid == 0:
            try:
                drop_caps()
                st = open("/proc/self/status").read()
                os._exit(0 if all(f"{k}:\t0000000000000000" in st for k in ("CapPrm", "CapEff", "CapAmb")) else 1)
            finally:
                os._exit(2)
        assert os.waitpid(pid, 0)[1] == 0, "drop_caps"
        # the monitor's bind without CAP_NET_RAW (with it, skipped: a selftest never opens the monitor): EPERM, which
        # also shows the sockaddr_hci is right (a wrong one is EINVAL)
        if not int(re.search(r"CapEff:\s*(\w+)", open("/proc/self/status").read())[1], 16) >> 13 & 1:
            try:
                monitor().close()
                raise AssertionError("bound the HCI monitor without CAP_NET_RAW")
            except PermissionError:
                pass
            except (OSError, AttributeError) as e:         # no Bluetooth in this kernel (or this Python)
                assert getattr(e, "errno", 0) != 22, e
        # links() as a user (no capability): the kernel's list, or no Bluetooth here; a wrong ioctl would be ENOTTY
        try:
            got = links()
            assert all(len(x) == 4 and len(x[2]) == 6 and x[3] in (True, False) for x in got), got
        except (OSError, AttributeError) as e:
            assert getattr(e, "errno", 97) in (93, 97), e   # EPROTONOSUPPORT, EAFNOSUPPORT
        # serve(), fed by a socketpair standing in for the monitor socket
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sparkhome", "siri-mic.sock")
            feed, mon = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            tap = Tap(R)
            th = threading.Thread(target=serve, args=(mon, tap, path, os.getuid()), daemon=True)
            th.start()

            def client(p=path):
                for _ in range(500):                       # until it listens
                    c = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    c.settimeout(5)
                    try:
                        c.connect(p)
                        return c
                    except (FileNotFoundError, ConnectionRefusedError):
                        c.close()
                        time.sleep(.01)
                raise AssertionError(f"nothing listens on {p}")

            def say(*pkts):
                for p in pkts:
                    feed.send(p)
            mark = [0x700]

            def handled():                                 # every packet said so far has been read and parsed
                mark[0] += 1
                say(br_conn(mark[0], O))
                for _ in range(500):
                    if (0, mark[0]) in tap.link:
                        return
                    time.sleep(.01)
                raise AssertionError("serve() stalled")
            say(le_conn(0x200, R), enc(0x200), *notify(frames[0]))
            handled()                                      # no client then: dropped, not kept for later
            a = client()
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
            assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
            say(*notify(END))                              # a connected before this was sent, so a gets it
            assert a.recv(200)[8:] == END
            b = client()
            t0 = time.monotonic_ns()
            say(*notify(frames[1]))
            for c in (a, b):                               # both; b nothing from before it connected
                m = c.recv(200)
                assert m[8:] == frames[1] and t0 <= struct.unpack("<Q", m[:8])[0] <= time.monotonic_ns(), m
            s, c = client(), client()                      # 4 clients: a fifth is refused
            assert client().recv(200) == b""
            for _ in range(2000):                          # s never reads: once its queue is full it is dropped, and
                say(*notify(END))                          # the others get every message meanwhile
                assert [x.recv(200)[8:] for x in (a, b, c)] == [END] * 3
            n = 0
            while s.recv(200):                             # what was queued for s, then the end
                n += 1
            assert 0 < n < 2000, n
            x = client()                                   # its place is free again
            say(*notify(frames[2]))
            assert [y.recv(200)[8:] for y in (a, b, c, x)] == [frames[2]] * 4
            f2, m2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            p2 = os.path.join(tmp, "other", "siri-mic.sock")
            th2 = threading.Thread(target=serve, args=(m2, Tap(R), p2, os.getuid() + 1), daemon=True)
            th2.start()
            assert client(p2).recv(200) == b""             # a peer with another uid is refused
            f2.close()
            feed.close()                                   # the monitor socket closed: serve() ends, socket removed
            for t in (th, th2):
                t.join(5)
                assert not t.is_alive()
            assert not os.path.exists(path) and not os.path.exists(p2) and a.recv(200) == b""
            for y in (a, b, c, s, x, mon, m2):
                y.close()
    for f in files:                                        # real captures without voice: nothing comes out
        t, n = Tap(remote), 0
        for _, p in btsnoop(f):
            assert t.packet(p) is None, f"{f}: kept a value (monitor packet {n})"
            n += 1
        print(f"{f}: {n} monitor packets, nothing kept; links (index, handle): the remote? {t.link}")


if __name__ == "__main__":
    main()
