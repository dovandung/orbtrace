"""
ORBTrace on the Great Scott Gadgets Cynthion (r1.x).

Pinout of the FPGA side is taken from the official Cynthion r1.4 platform
(greatscottgadgets/cynthion, cynthion/python/src/gateware/platform/cynthion_r1_4.py).

What is different from ORBTrace mini:
  * No level shifters: all debug/trace I/O is 3.3 V LVCMOS on the two Pmod headers.
    Only connect 3.3 V targets directly, or add an external level shifter
    (``jtms_dir`` is brought out so a direction-controlled shifter can be used for SWDIO).
  * No target power switching (``with_target_power`` is disabled).
  * USB goes through the AUX port PHY (the CONTROL port belongs to Apollo).
  * The USB3343 PHYs on Cynthion have D+/D- swapped, which is fixed with ULPI register 0x39.
  * The bitstream is loaded with Apollo (``apollo configure`` / ``apollo flash-program``),
    not with the ORBTrace DFU bootloader.

Pmod pinout (Pmod pin numbers as printed on the board, 3.3 V, GND on pins 5/11, 3V3 on 6/12):

    PMOD A (trace)                 PMOD B (debug)
    pin 1  C9   TRACEDATA3         pin 1  B4   SWDIO / TMS
    pin 2  B9   (spare)            pin 2  B5   SWCLK / TCK
    pin 3  D11  (spare)            pin 3  B6   SWO   / TDO
    pin 4  C12  (spare)            pin 4  B7   TDI
    pin 7  C8   TRACECLK           pin 7  C5   nRESET (open drain, active low)
    pin 8  D8   TRACEDATA0         pin 8  A5   SWDIO direction (1 = probe drives)
    pin 9  D9   TRACEDATA1         pin 9  A6   GPIO0 (CMSIS-DAP "CAN" signal)
    pin 10 C10  TRACEDATA2         pin 10 A7   (spare)
"""

from migen import *

from litex.build.generic_platform import Pins, Subsignal, IOStandard, Misc
from litex.build.lattice import LatticePlatform

from litespi.opcodes import SpiNorFlashOpCodes as Codes

from ..crg_ecp5 import CRG
from ..flash_modules import W25Q32JV

# IOs ----------------------------------------------------------------------------------------------

_io = [
    # Clk: discrete 60 MHz oscillator.
    ('clk60', 0, Pins('A8'), IOStandard('LVCMOS33')),

    # User LEDs (active low), LED0..LED5.
    ('user_led', 0, Pins('E13'), IOStandard('LVCMOS33')),
    ('user_led', 1, Pins('C13'), IOStandard('LVCMOS33')),
    ('user_led', 2, Pins('B14'), IOStandard('LVCMOS33')),
    ('user_led', 3, Pins('A15'), IOStandard('LVCMOS33')),
    ('user_led', 4, Pins('D12'), IOStandard('LVCMOS33')),
    ('user_led', 5, Pins('C11'), IOStandard('LVCMOS33')),

    # USER button (active low, external pull-up).
    ('btn', 0, Pins('M14'), IOStandard('LVCMOS33'), Misc('PULLMODE=NONE')),

    # Drives PROGRAMN low to reconfigure the FPGA from flash.
    ('programn', 0, Pins('T13'), IOStandard('LVCMOS33'), Misc('PULLMODE=UP')),

    # Debug (PMOD B).
    ('debug', 0,
        Subsignal('jtms',     Pins('pmodb:0')),  # Pmod pin 1:  SWDIO / TMS
        Subsignal('jtck',     Pins('pmodb:1')),  # Pmod pin 2:  SWCLK / TCK
        Subsignal('jtdo',     Pins('pmodb:2')),  # Pmod pin 3:  SWO / TDO
        Subsignal('jtdi',     Pins('pmodb:3')),  # Pmod pin 4:  TDI
        Subsignal('nrst',     Pins('pmodb:4')),  # Pmod pin 7:  nRESET
        Subsignal('jtms_dir', Pins('pmodb:5')),  # Pmod pin 8:  SWDIO direction for an external level shifter
        IOStandard('LVCMOS33'),
        Misc('SLEWRATE=FAST'),
    ),

    # Trace (PMOD A). C8 is a primary clock input, so TRACECLK goes there.
    ('trace', 0,
        Subsignal('clk',  Pins('pmoda:4')),                         # Pmod pin 7
        Subsignal('data', Pins('pmoda:5 pmoda:6 pmoda:7 pmoda:0')), # Pmod pins 8, 9, 10, 1
        IOStandard('LVCMOS33'),
    ),

    # GPIO0 is used by CMSIS-DAP.
    ('gpio', 0,
        Subsignal('data', Pins('pmodb:6')),  # Pmod pin 9
        IOStandard('LVCMOS33'),
    ),

    # SPI flash (Winbond W25Q32JV, 4 MiB). SCK goes through USRMCLK.
    ('spiflash4x', 0,
        Subsignal('cs_n', Pins('N8')),
        Subsignal('dq',   Pins('T8 T7 M7 N7')),
        IOStandard('LVCMOS33'),
    ),

    # USB: AUX port PHY (USB3343). The PHY gets its 60 MHz clock from the FPGA.
    ('ulpi', 0,
        Subsignal('rst_n', Pins('J13')),
        Subsignal('clk_o', Pins('D16')),
        Subsignal('dir',   Pins('E16')),
        Subsignal('nxt',   Pins('F15')),
        Subsignal('stp',   Pins('E15')),
        Subsignal('data',  Pins('F16 G15 G16 H15 J15 J16 K15 K16')),
        IOStandard('LVCMOS33'),
        Misc('SLEWRATE=FAST'),
    ),

    # Pseudo-supply pins: tied to VCCIO / GND on the PCB and driven by the
    # gateware to source/sink extra supply current (same as the official Cynthion gateware).
    ('pseudo_vccio', 0,
        Pins('E6 E7 D10 E10 E11 F12 J12 K12 L12 N13 P13 M11 P11 P12 L4 M4 R5 M5 N5 P4 M6 F5 G5 H5 H4 J4 J5 J3 J1 J2 R6'),
        IOStandard('LVCMOS33'),
    ),
    ('pseudo_gnd', 0,
        Pins('E5 E8 E9 E12 F13 M13 M12 N12 N11 L5 L3 M3 N6 P5 P6 F4 G2 G3 H3 H2'),
        IOStandard('LVCMOS33'),
    ),
]

# Connectors ---------------------------------------------------------------------------------------

# Index n is the n-th signal pin: Pmod pins 1 2 3 4 7 8 9 10.
_connectors = [
    ('pmoda', 'C9 B9 D11 C12 C8 D8 D9 C10'),
    ('pmodb', 'B4 B5 B6 B7 C5 A5 A6 A7'),
]

# Platform -----------------------------------------------------------------------------------------

class Platform(LatticePlatform):
    default_clk_name   = 'clk60'
    default_clk_period = 1e9/60e6

    # USB3343 on Cynthion: swap D+ and D- to match the PCB routing.
    ulpi_extra_registers = {
        0x39: 0b000110,
    }

    def __init__(self, device='25F', speed='8', toolchain='trellis', **kwargs):
        assert device in ['12F', '25F']
        assert speed in ['6', '7', '8']
        LatticePlatform.__init__(self, f'LFE5U-{device}-{speed}BG256C', _io, _connectors, toolchain=toolchain, **kwargs)

    def get_crg(self, sys_clk_freq):
        crg = CRG(self, sys_clk_freq)
        crg.add_usb()
        return crg

    def get_flash_module(self):
        return W25Q32JV(Codes.READ_1_4_4)

    def add_leds(self, soc):
        # Cynthion has six single-colour LEDs instead of RGB LEDs, so the
        # ORBTrace RGB status colours are folded onto separate LEDs:
        #   LED0: status (any colour)
        #   LED1: debug connected        LED2: debug running
        #   LED3: trace clock / overrun  LED4: trace data
        #   LED5: unused
        soc.led_status = Record([('r', 1), ('g', 1), ('b', 1)])
        soc.led_debug  = Record([('r', 1), ('g', 1), ('b', 1)])
        soc.led_trace  = Record([('r', 1), ('g', 1), ('b', 1)])

        leds = [self.request('user_led', i) for i in range(6)]

        status = soc.led_status
        debug  = soc.led_debug
        trace  = soc.led_trace

        on = [
            status.r | status.g | status.b,
            debug.r,
            debug.g & ~debug.r,
            trace.r,
            trace.g,
            Constant(0, 1),
        ]

        for pad, value in zip(leds, on):
            soc.comb += pad.eq(~value) # Active low.

    def add_platform_specific(self, soc):
        vccio = self.request('pseudo_vccio')
        gnd   = self.request('pseudo_gnd')

        soc.comb += [
            vccio.eq(Replicate(1, len(vccio))),
            gnd.eq(0),
        ]

    def do_finalize(self, fragment):
        LatticePlatform.do_finalize(self, fragment)
        self.add_period_constraint(self.lookup_request('clk60', loose=True), 1e9/60e6)
        self.add_period_constraint(self.lookup_request('trace:clk', 0, loose=True), 1e9/120e6)

    @classmethod
    def add_arguments(cls, parser):
        parser.add_argument('--device', choices = ['12F', '25F'], default = '25F', help = 'ECP5 device on your Cynthion (default: 25F)')
        parser.add_argument('--speed', choices = ['6', '7', '8'], default = '8', help = 'ECP5 speed grade (default: 8)')

    @classmethod
    def get_profile(cls, profile):
        return {
            'default': {
                # 72 MHz instead of the usual 75 MHz: the CPU clock domain only just met
                # 75 MHz on the 25F (and missed it on the 12F). Nothing on Cynthion needs more.
                'sys_clk_freq': 72e6,
                'uart_name': 'stream',
                'with_debug': True,
                'with_trace': True,
                'with_target_power': False,
                'ecppack_bootaddr': '0x0',
                'ecppack_freq': 38.8,
                'ecppack_compress': True,
            },
        }[profile]
