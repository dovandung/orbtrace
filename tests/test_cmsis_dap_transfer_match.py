"""DAP_Transfer value match / match mask, as used by Keil MDK to poll the target
(e.g. waiting for the core to halt after running a flash algorithm)."""

import struct

from amaranth import *
from amaranth.sim import Simulator

from luna.gateware.stream import StreamInterface

from orbtrace.debug.cmsis_dap import CMSIS_DAP

ACK_OK = 1

# Transfer request bits
APnDP      = 1 << 0
RnW        = 1 << 1
A2         = 1 << 2
A3         = 1 << 3
MATCH_VAL  = 1 << 4
MATCH_MASK = 1 << 5


class StubDBGIF:
    def __init__(self):
        self.addr32  = Signal(2)
        self.rnw     = Signal()
        self.apndp   = Signal()
        self.dwrite  = Signal(32)
        self.dread   = Signal(32)
        self.perr    = Signal()
        self.go      = Signal()
        self.done    = Signal(init=1)
        self.ack     = Signal(3)
        self.pinsin  = Signal(16)
        self.pinsout = Signal(8)
        self.command = Signal(5)
        self.dev     = Signal(3)
        self.is_jtag = Signal()


class Top(Elaboratable):
    def __init__(self):
        self.stream_in  = StreamInterface()
        self.stream_out = StreamInterface()
        self.is_v2      = Signal(init=1)
        self.dbg        = StubDBGIF()
        self.dap = CMSIS_DAP(self.stream_in, self.stream_out, self.dbg, self.is_v2)

    def elaborate(self, platform):
        m = Module()
        m.submodules.dap = self.dap
        return m


class Target:
    """Fake SWD target. read_values: per (apndp, addr) list of values returned by successive reads.
    SWD AP reads are posted: an AP read returns the value of the previous AP read."""
    def __init__(self, read_values):
        self.read_values = {k: list(v) for k, v in read_values.items()}
        self.log = []
        self.ap_pending = 0

    def access(self, apndp, rnw, addr, wdata):
        self.log.append(('AP' if apndp else 'DP', 'R' if rnw else 'W', addr, wdata if not rnw else None))
        if not rnw:
            return 0
        if apndp:
            prev = self.ap_pending
            seq = self.read_values.get((1, addr), [0])
            self.ap_pending = seq.pop(0) if len(seq) > 1 else seq[0]
            return prev
        if addr == 3:   # RDBUFF: last AP read result
            return self.ap_pending
        seq = self.read_values.get((0, addr), [0])
        return seq.pop(0) if len(seq) > 1 else seq[0]


def run(read_values, commands):
    top = Top()
    dbg = top.dbg
    target = Target(read_values)
    responses = []

    async def target_proc(ctx):
        while True:
            await ctx.tick().until(dbg.go == 1)
            ctx.set(dbg.done, 0)
            await ctx.tick().repeat(3)
            v = target.access(ctx.get(dbg.apndp), ctx.get(dbg.rnw), ctx.get(dbg.addr32), ctx.get(dbg.dwrite))
            ctx.set(dbg.dread, v)
            ctx.set(dbg.ack, ACK_OK)
            ctx.set(dbg.done, 1)

    async def host(ctx):
        so, si = top.stream_out, top.stream_in
        for cmd in commands:
            for i, b in enumerate(cmd):
                ctx.set(so.payload, b)
                ctx.set(so.first, i == 0)
                ctx.set(so.last, i == len(cmd) - 1)
                ctx.set(so.valid, 1)
                await ctx.tick().until(so.ready == 1)
            ctx.set(so.valid, 0)
            ctx.set(si.ready, 1)
            out = []
            for _ in range(2000):
                _, _, valid, payload, last = await ctx.tick().sample(si.valid, si.payload, si.last)
                if valid:
                    out.append(payload)
                    if last:
                        break
            ctx.set(si.ready, 0)
            responses.append(out)

    sim = Simulator(top)
    sim.add_clock(1e-8)
    sim.add_testbench(target_proc, background=True)
    sim.add_testbench(host)
    sim.run_until(2e-3)
    return responses, target


def transfer(*reqs):
    """reqs: (request, data or None)"""
    body = [0x05, 0x00, len(reqs)]
    for req, data in reqs:
        body.append(req)
        if data is not None:
            body += list(struct.pack('<I', data))
    return body


def test_mask_write_is_not_sent_to_target_and_match_completes():
    # DP CTRL/STAT (addr 1) reads 0, 0, then bit 5 set.
    resp, target = run({(0, 1): [0x00, 0x00, 0x20, 0x20]}, [
        transfer((MATCH_MASK, 0x00000020),
                 (RnW | A2 | MATCH_VAL, 0x00000020)),
    ])
    r = resp[0]
    assert r[0] == 0x05
    assert r[1] == 2                 # both transfers done
    assert r[2] == ACK_OK            # OK, no mismatch flag
    assert len(r) == 3               # match reads return no data
    # Only the polling reads reach the target, no write for the mask
    assert all(e[1] == 'R' for e in target.log), target.log
    assert len(target.log) == 3


def test_mismatch_sets_bit4_after_retries():
    resp, target = run({(0, 1): [0x00]}, [
        [0x04, 0x00] + list(struct.pack('<HH', 4, 3)),   # TransferConfigure: idle, wait retry 4, match retry 3
        transfer((MATCH_MASK, 0xFFFFFFFF),
                 (RnW | A2 | MATCH_VAL, 0x00000001)),
    ])
    r = resp[1]
    assert r[0] == 0x05
    assert r[2] & 0x07 == ACK_OK
    assert r[2] & 0x10              # Value mismatch
    assert len(r) == 3
    assert 1 <= len(target.log) <= 6, target.log


def test_ap_match_then_normal_reads_return_correct_data():
    # AP DRW (addr 3) polled until 0x0003_0000 (e.g. DHCSR S_HALT|S_REGRDY), then a DP read.
    resp, target = run({(1, 3): [0x1, 0x2, 0x30000, 0x30000], (0, 0): [0x2BA01477]}, [
        transfer((MATCH_MASK, 0x00030000),
                 (APnDP | RnW | A2 | A3 | MATCH_VAL, 0x00030000),
                 (RnW, None)),                                  # DP IDCODE read
    ])
    r = resp[0]
    assert r[1] == 3 and r[2] == ACK_OK, r
    # Exactly one data word (the DP read), no leftover posted AP data
    assert len(r) == 3 + 4, r
    assert struct.unpack('<I', bytes(r[3:7]))[0] == 0x2BA01477


def test_plain_reads_still_work():
    resp, target = run({(1, 3): [0x11, 0x22, 0x33], (0, 0): [0x2BA01477]}, [
        transfer((APnDP | RnW | A2 | A3, None),
                 (APnDP | RnW | A2 | A3, None),
                 (RnW, None)),
    ])
    r = resp[0]
    assert r[1] == 3 and r[2] == ACK_OK, r
    words = [struct.unpack('<I', bytes(r[3+4*i:7+4*i]))[0] for i in range(3)]
    assert words == [0x11, 0x22, 0x2BA01477]
