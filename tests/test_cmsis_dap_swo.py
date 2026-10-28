"""Simulation tests for the CMSIS-DAP SWO commands (DAP_SWO_*)."""

import struct

from amaranth import *
from amaranth.sim import Simulator, SimulatorContext

from luna.gateware.stream import StreamInterface

from orbtrace.debug import cmsis_dap
from orbtrace.debug.cmsis_dap import CMSIS_DAP, DAP_SWO_BUFFER_SIZE


class StubDBGIF:
    """Just enough of the debug interface for CMSIS_DAP to elaborate."""
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
    def __init__(self, v2=True, has_swo=True):
        self.stream_in  = StreamInterface()   # Responses (DAP -> host)
        self.stream_out = StreamInterface()   # Commands  (host -> DAP)
        self.is_v2      = Signal(init=int(v2))
        self.dap = CMSIS_DAP(self.stream_in, self.stream_out, StubDBGIF(), self.is_v2, has_swo=has_swo)

    def elaborate(self, platform):
        m = Module()
        m.submodules.dap = self.dap
        return m


async def send_command(ctx, top, data):
    """Send one CMSIS-DAP command packet (first on byte 0)."""
    so = top.stream_out
    for i, b in enumerate(data):
        ctx.set(so.payload, b)
        ctx.set(so.first, i == 0)
        ctx.set(so.last, i == len(data) - 1)
        ctx.set(so.valid, 1)
        await ctx.tick().until(so.ready == 1)
    ctx.set(so.valid, 0)
    ctx.set(so.first, 0)
    ctx.set(so.last, 0)


async def read_response(ctx, top, v2=True):
    """Collect a response: until 'last' for v2, or 64 bytes for v1.
    Each sample is taken at a clock edge where valid and ready are both high."""
    si = top.stream_in
    ctx.set(si.ready, 1)
    out = []
    while True:
        payload, last = await ctx.tick().sample(si.payload, si.last).until(si.valid == 1)
        out.append(payload)
        if v2 and last:
            break
        if not v2 and len(out) == 64:
            break
    ctx.set(si.ready, 0)
    return out


async def command(ctx, top, data, v2=True):
    await send_command(ctx, top, data)
    return await read_response(ctx, top, v2)


async def feed_swo(ctx, top, data):
    dap = top.dap
    for b in data:
        ctx.set(dap.swo_in_data, b)
        ctx.set(dap.swo_in_valid, 1)
        await ctx.tick()
    ctx.set(dap.swo_in_valid, 0)


def run(top, bench):
    sim = Simulator(top)
    sim.add_clock(1e-8)
    sim.add_testbench(bench)
    sim.run()


def test_info_capabilities_and_buffer_size():
    top = Top()

    async def bench(ctx):
        r = await command(ctx, top, [0x00, 0xF0])
        assert r[:3] == [0x00, 0x01, 0x03 | 0x04 | 0x08 | 0x40]

        r = await command(ctx, top, [0x00, 0xFD])
        assert r[:2] == [0x00, 0x04]
        assert struct.unpack('<I', bytes(r[2:6]))[0] == DAP_SWO_BUFFER_SIZE

    run(top, bench)


def test_info_without_swo():
    top = Top(has_swo=False)

    async def bench(ctx):
        r = await command(ctx, top, [0x00, 0xF0])
        assert r[:3] == [0x00, 0x01, 0x03]

        r = await command(ctx, top, [0x17, 0x01])     # Transport
        assert r == [0x17, 0xFF]

    run(top, bench)


def test_uart_mode_sets_trace_format_and_baud():
    top = Top()
    dap = top.dap

    async def bench(ctx):
        r = await command(ctx, top, [0x18, 0x01])     # Mode UART
        assert r == [0x18, 0x00]
        assert ctx.get(dap.swo_format) == 0x12

        r = await command(ctx, top, [0x19] + list(struct.pack('<I', 32_000_000)))
        assert r[0] == 0x19
        assert struct.unpack('<I', bytes(r[1:5]))[0] == 32_000_000
        assert ctx.get(dap.swo_baudrate) == 32_000_000

        # Too fast: rejected with 0
        r = await command(ctx, top, [0x19] + list(struct.pack('<I', 100_000_000)))
        assert struct.unpack('<I', bytes(r[1:5]))[0] == 0

        r = await command(ctx, top, [0x18, 0x02])     # Mode Manchester
        assert r == [0x18, 0x00]
        assert ctx.get(dap.swo_format) == 0x10

        r = await command(ctx, top, [0x18, 0x00])     # Mode off
        assert ctx.get(dap.swo_format) == 0x00

        r = await command(ctx, top, [0x18, 0x07])     # Invalid mode
        assert r == [0x18, 0xFF]

    run(top, bench)


def test_baud_strobe_pulses():
    top = Top()
    dap = top.dap
    pulses = []

    async def bench(ctx):
        await command(ctx, top, [0x19] + list(struct.pack('<I', 2_000_000)))
        await command(ctx, top, [0x18, 0x01])

    async def watch(ctx):
        async for _, _, strobe in ctx.tick().sample(dap.swo_baudrate_strobe):
            if strobe:
                pulses.append(1)

    sim = Simulator(top)
    sim.add_clock(1e-8)
    sim.add_testbench(bench)
    sim.add_process(watch)
    sim.run_until(5e-5)
    # Once for Baudrate, once again when Mode hands the format to the trace core
    assert len(pulses) == 2


def test_control_requires_mode():
    top = Top()

    async def bench(ctx):
        r = await command(ctx, top, [0x1A, 0x01])     # Start with mode off
        assert r == [0x1A, 0xFF]

        await command(ctx, top, [0x18, 0x01])         # UART, but no baud rate yet
        r = await command(ctx, top, [0x1A, 0x01])
        assert r == [0x1A, 0xFF]

        await command(ctx, top, [0x19] + list(struct.pack('<I', 1_000_000)))
        r = await command(ctx, top, [0x1A, 0x01])
        assert r == [0x1A, 0x00]

        r = await command(ctx, top, [0x1B])           # Status: active, empty
        assert r == [0x1B, 0x01, 0, 0, 0, 0]

    run(top, bench)


async def start_capture(ctx, top, transport):
    assert await command(ctx, top, [0x17, transport]) == [0x17, 0x00]
    assert await command(ctx, top, [0x18, 0x01]) == [0x18, 0x00]
    await command(ctx, top, [0x19] + list(struct.pack('<I', 4_000_000)))
    assert await command(ctx, top, [0x1A, 0x01]) == [0x1A, 0x00]


def test_swo_data_polling_v2():
    top = Top()
    data = list(range(1, 101))

    async def bench(ctx):
        await start_capture(ctx, top, 1)
        await feed_swo(ctx, top, data)

        r = await command(ctx, top, [0x1B])
        assert r[1] == 0x01
        assert struct.unpack('<I', bytes(r[2:6]))[0] == len(data)

        # Ask for 40: get 40
        r = await command(ctx, top, [0x1C] + list(struct.pack('<H', 40)))
        assert r[0] == 0x1C and r[1] == 0x01
        assert struct.unpack('<H', bytes(r[2:4]))[0] == 40
        assert r[4:] == data[:40]

        # Ask for 1000: get the remaining 60
        r = await command(ctx, top, [0x1C] + list(struct.pack('<H', 1000)))
        assert struct.unpack('<H', bytes(r[2:4]))[0] == 60
        assert r[4:] == data[40:]

        # Empty: header only
        r = await command(ctx, top, [0x1C] + list(struct.pack('<H', 10)))
        assert r == [0x1C, 0x01, 0, 0]

        # Extended status: status + count + index
        r = await command(ctx, top, [0x1E, 0x07])
        assert len(r) == 1 + 1 + 4 + 4 + 4
        assert r[1] == 0x01
        assert struct.unpack('<I', bytes(r[2:6]))[0] == 0
        assert struct.unpack('<I', bytes(r[6:10]))[0] == len(data)

        # Stop
        assert await command(ctx, top, [0x1A, 0x00]) == [0x1A, 0x00]
        r = await command(ctx, top, [0x1B])
        assert r[1] == 0x00

    run(top, bench)


def test_swo_data_polling_v1_is_padded_and_capped():
    top = Top(v2=False)
    data = list(range(200))

    async def bench(ctx):
        await send_command(ctx, top, [0x17, 0x01]); await read_response(ctx, top, v2=False)
        await send_command(ctx, top, [0x18, 0x01]); await read_response(ctx, top, v2=False)
        await send_command(ctx, top, [0x19] + list(struct.pack('<I', 4_000_000))); await read_response(ctx, top, v2=False)
        await send_command(ctx, top, [0x1A, 0x01]); await read_response(ctx, top, v2=False)
        await feed_swo(ctx, top, data)

        await send_command(ctx, top, [0x1C] + list(struct.pack('<H', 500)))
        r = await read_response(ctx, top, v2=False)
        assert len(r) == 64
        assert struct.unpack('<H', bytes(r[2:4]))[0] == 60   # 64 byte report - 4 byte header
        assert r[4:64] == data[:60]

    run(top, bench)


def test_streaming_transport_rejected_on_v1():
    top = Top(v2=False)

    async def bench(ctx):
        await send_command(ctx, top, [0x17, 0x02])
        r = await read_response(ctx, top, v2=False)
        assert r[:2] == [0x17, 0xFF]

    run(top, bench)


def test_swo_streaming_v2():
    top = Top()
    dap = top.dap
    data = list(range(50))
    got = []

    async def bench(ctx):
        await start_capture(ctx, top, 2)
        await feed_swo(ctx, top, data)

        ctx.set(dap.swoStream.ready, 1)
        while len(got) < len(data):
            payload, last = await ctx.tick().sample(dap.swoStream.payload, dap.swoStream.last) \
                                               .until(dap.swoStream.valid == 1)
            got.append((payload, last))
        ctx.set(dap.swoStream.ready, 0)

        assert [b for b, _ in got] == data
        # Packet ends when the buffer runs dry
        assert got[-1][1] == 1

    run(top, bench)


def test_overrun_and_clear_on_start():
    top = Top()

    async def bench(ctx):
        await start_capture(ctx, top, 1)
        await feed_swo(ctx, top, [0x55] * (DAP_SWO_BUFFER_SIZE + 10))

        r = await command(ctx, top, [0x1B])
        assert r[1] == 0x81   # active + overrun
        assert struct.unpack('<I', bytes(r[2:6]))[0] == DAP_SWO_BUFFER_SIZE

        # Reported once, then cleared (like the reference CMSIS-DAP firmware)
        r = await command(ctx, top, [0x1B])
        assert r[1] == 0x01

        # A new overrun is reported again
        await feed_swo(ctx, top, [0x55] * 4)
        r = await command(ctx, top, [0x1C] + list(struct.pack('<H', 0)))
        assert r[1] == 0x81
        r = await command(ctx, top, [0x1E, 0x01])
        assert r == [0x1E, 0x01]

        # Restart: buffer and flags cleared
        await command(ctx, top, [0x1A, 0x00])
        await command(ctx, top, [0x1A, 0x01])
        r = await command(ctx, top, [0x1B])
        assert r == [0x1B, 0x01, 0, 0, 0, 0]

    run(top, bench)


def test_not_capturing_drops_data():
    top = Top()

    async def bench(ctx):
        await command(ctx, top, [0x17, 0x01])
        await command(ctx, top, [0x18, 0x01])
        await feed_swo(ctx, top, [1, 2, 3])
        r = await command(ctx, top, [0x1B])
        assert r == [0x1B, 0x00, 0, 0, 0, 0]

    run(top, bench)


def test_trace_core_routes_uart_swo_to_cmsis_dap():
    """UART SWO waveform -> trace core SWO receiver -> dap_swo stream (not the Orbuculum output)."""
    from orbtrace.trace import core

    dut = core.TraceCore()
    baud = 10_000_000
    samples_per_bit = 500_000_000 // baud     # 2 samples per swo2x cycle at 250 MHz
    data = [0x55, 0xA3, 0x00, 0xFF, 0x42]
    got = []
    orbuculum = []

    sim = Simulator(dut)
    sim.add_clock(1/75e6,  domain='sync')
    sim.add_clock(1/250e6, domain='swo2x')
    sim.add_clock(1/125e6, domain='swo')
    sim.add_clock(1/100e6, domain='trace')

    @sim.add_testbench
    async def control(ctx):
        ctx.set(dut.dap_swo_format, 0x12)      # UART, owned by CMSIS-DAP
        ctx.set(dut.dap_swo_baudrate, baud)
        ctx.set(dut.dap_swo_baudrate_strobe, 1)
        await ctx.tick('sync')
        ctx.set(dut.dap_swo_baudrate_strobe, 0)

    @sim.add_testbench
    async def line(ctx):
        def bits():
            yield from [1] * 200                 # idle, give the divider time
            for b in data:
                yield 0                          # start
                for i in range(8):
                    yield (b >> i) & 1
                yield 1                          # stop
                yield 1
            yield from [1] * 20

        level_samples = []
        for bit in bits():
            level_samples += [bit] * samples_per_bit
        for a, b in zip(level_samples[0::2], level_samples[1::2]):
            ctx.set(dut.swo, a | (b << 1))
            await ctx.tick('swo2x')

    @sim.add_testbench
    async def sink(ctx):
        ctx.set(dut.dap_swo.ready, 1)
        ctx.set(dut.output.ready, 1)
        while True:
            _, _, valid, payload, ovalid = await ctx.tick('sync').sample(dut.dap_swo.valid, dut.dap_swo.payload, dut.output.valid)
            if valid:
                got.append(payload)
            if ovalid:
                orbuculum.append(1)

    # The last stop bit is only seen once the idle run is long enough to be reported (~66 us).
    sim.run_until(150e-6)
    # The simulated line starts low before going idle, which can add one junk byte first.
    assert got[-len(data):] == data and len(got) <= len(data) + 1, got
    assert orbuculum == []
