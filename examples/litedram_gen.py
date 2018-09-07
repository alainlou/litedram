#!/usr/bin/env python3
import os
import sys
import math
import struct

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.build.generic_platform import *
from litex.build.xilinx import XilinxPlatform

from litedram.core.controller import ControllerSettings
from litex.soc.integration.soc_sdram import *
from litex.soc.integration.builder import *
from litex.soc.cores.uart import *

from litedram.frontend.axi import *
from litedram.frontend.bist import LiteDRAMBISTGenerator
from litedram.frontend.bist import LiteDRAMBISTChecker


def get_common_ios():
    return [
        # clk / rst
        ("clk", 0, Pins("X")),
        ("rst", 0, Pins("X")),

        # serial
        ("serial", 0,
            Subsignal("tx", Pins("X")),
            Subsignal("rx", Pins("X"))
        ),

        # crg status
        ("pll_locked", 0, Pins("X")),

        # init status
        ("init_done", 0, Pins("X")),
        ("init_error", 0, Pins("X")),

        # iodelay clk / rst
        ("clk_iodelay", 0, Pins("X")),
        ("rst_iodelay", 0, Pins("X")),

        # user clk / rst
        ("user_clk", 0, Pins("X")),
        ("user_rst", 0, Pins("X"))
    ]

def get_dram_ios(core_config):
    sdram_module = core_config["sdram_module"]
    return [
        ("ddram", 0,
            Subsignal("a", Pins(
                "X "*log2_int(core_config["sdram_module"].nrows))),
            Subsignal("ba", Pins(
                "X "*log2_int(core_config["sdram_module"].nbanks))),
            Subsignal("ras_n", Pins("X")),
            Subsignal("cas_n", Pins("X")),
            Subsignal("we_n", Pins("X")),
            Subsignal("cs_n", Pins(
                "X "*core_config["sdram_rank_nb"])),
            Subsignal("dm", Pins(
                "X "*2*core_config["sdram_module_nb"])),
            Subsignal("dq", Pins(
                "X "*16*core_config["sdram_module_nb"])),
            Subsignal("dqs_p", Pins(
                "X "*2*core_config["sdram_module_nb"])),
            Subsignal("dqs_n", Pins(
                "X "*2*core_config["sdram_module_nb"])),
            Subsignal("clk_p", Pins("X")),
            Subsignal("clk_n", Pins("X")),
            Subsignal("cke", Pins("X")),
            Subsignal("odt", Pins("X")),
            Subsignal("reset_n", Pins("X"))
        ),
    ]

def get_native_user_port_ios(_id, aw, dw):
    return [
        ("user_port", _id,
            # cmd
            Subsignal("cmd_valid", Pins(1)),
            Subsignal("cmd_ready", Pins(1)),
            Subsignal("cmd_we",    Pins(1)),
            Subsignal("cmd_addr",  Pins(aw)),

            # wdata
            Subsignal("wdata_valid", Pins(1)),
            Subsignal("wdata_ready", Pins(1)),
            Subsignal("wdata_we", Pins(dw//8)),
            Subsignal("wdata_data", Pins(dw)),

            # rdata
            Subsignal("rdata_valid", Pins(1)),
            Subsignal("rdata_ready", Pins(1)),
            Subsignal("rdata_data", Pins(dw))
        ),
    ]

def get_axi_user_port_ios(_id, aw, dw, iw):
    return [
        ("user_port", _id,
            # aw
            Subsignal("aw_valid", Pins(1)),
            Subsignal("aw_ready", Pins(1)),
            Subsignal("aw_addr",  Pins(aw)),
            Subsignal("aw_burst", Pins(2)),
            Subsignal("aw_len", Pins(8)),
            Subsignal("aw_size", Pins(4)),
            Subsignal("aw_id",  Pins(iw)),

            # w
            Subsignal("w_valid", Pins(1)),
            Subsignal("w_ready", Pins(1)),
            Subsignal("w_last", Pins(1)),
            Subsignal("w_strb", Pins(dw//8)),
            Subsignal("w_data", Pins(dw)),

            # b
            Subsignal("b_valid", Pins(1)),
            Subsignal("b_ready", Pins(1)),
            Subsignal("b_resp", Pins(2)),
            Subsignal("b_id",  Pins(iw)),

            # ar
            Subsignal("ar_valid", Pins(1)),
            Subsignal("ar_ready", Pins(1)),
            Subsignal("ar_addr",  Pins(aw)),
            Subsignal("ar_burst", Pins(2)),
            Subsignal("ar_len", Pins(8)),
            Subsignal("ar_size", Pins(4)),
            Subsignal("ar_id",  Pins(iw)),

            # r
            Subsignal("r_valid", Pins(1)),
            Subsignal("r_ready", Pins(1)),
            Subsignal("r_last", Pins(1)),
            Subsignal("r_resp", Pins(2)),
            Subsignal("r_data", Pins(dw)),
            Subsignal("r_id", Pins(iw))
        ),
    ]


class Platform(XilinxPlatform):
    def __init__(self):
        XilinxPlatform.__init__(self, "", io=[], toolchain="vivado")


class LiteDRAMCRG(Module):
    def __init__(self, platform, core_config):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_sys4x = ClockDomain(reset_less=True)
        self.clock_domains.cd_sys4x_dqs = ClockDomain(reset_less=True)
        self.clock_domains.cd_iodelay = ClockDomain()

        clk = platform.request("clk")
        reset = platform.request("rst")

        assert core_config["input_clk_freq"] in [100e6, 200e6]
        assert core_config["iodelay_clk_freq"] in [200e6, 300e6]
        assert core_config["sys_clk_freq"]*4 == core_config["dram_clk_freq"]

        pll_pre_multiplier = 2 if core_config["input_clk_freq"] == 100e6 else 1

        # main pll
        main_pll_multipliers = {
            100e6: 4*pll_pre_multiplier,
            125e6: 5*pll_pre_multiplier,
            150e6: 6*pll_pre_multiplier,
            175e6: 7*pll_pre_multiplier,
            200e6: 8*pll_pre_multiplier,
        }
        main_pll_locked = Signal()
        main_pll_fb = Signal()
        main_pll_sys = Signal()
        main_pll_sys4x = Signal()
        main_pll_sys4x_dqs = Signal()
        self.specials += [
            Instance("PLLE2_BASE",
                     p_STARTUP_WAIT="FALSE", o_LOCKED=main_pll_locked,

                     # VCO @ 0.8 to 1.6GHz
                     p_REF_JITTER1=0.01, p_CLKIN1_PERIOD=1e9/core_config["input_clk_freq"],
                     p_CLKFBOUT_MULT=main_pll_multipliers[core_config["sys_clk_freq"]], p_DIVCLK_DIVIDE=1,
                     i_CLKIN1=clk, i_CLKFBIN=main_pll_fb, o_CLKFBOUT=main_pll_fb,

                     # 100 to 200MHz
                     p_CLKOUT0_DIVIDE=8, p_CLKOUT0_PHASE=0.0,
                     o_CLKOUT0=main_pll_sys,

                     # 400 to 800MHz
                     p_CLKOUT1_DIVIDE=2, p_CLKOUT1_PHASE=0.0,
                     o_CLKOUT1=main_pll_sys4x,

                     # 400 to 800MHz dqs (use for A7DDRPHY)
                     p_CLKOUT2_DIVIDE=2, p_CLKOUT2_PHASE=90.0,
                     o_CLKOUT2=main_pll_sys4x_dqs,
            ),
            Instance("BUFG", i_I=main_pll_sys, o_O=self.cd_sys.clk),
            Instance("BUFG", i_I=main_pll_sys4x, o_O=self.cd_sys4x.clk),
            Instance("BUFG", i_I=main_pll_sys4x_dqs, o_O=self.cd_sys4x_dqs.clk),
            AsyncResetSynchronizer(self.cd_sys, ~main_pll_locked | reset),
        ]
        self.comb += platform.request("pll_locked").eq(main_pll_locked)

        # iodelay_pll
        iodelay_dividers = {
            200e6: 6,
            300e6: 4
        }
        iodelay_pll_locked = Signal()
        iodelay_pll_fb = Signal()
        iodelay_pll_iodelay = Signal()
        self.specials += [
            Instance("PLLE2_BASE",
                     p_STARTUP_WAIT="FALSE", o_LOCKED=iodelay_pll_locked,

                     # VCO @ 1.2GHz
                     p_REF_JITTER1=0.01, p_CLKIN1_PERIOD=1e9/core_config["input_clk_freq"],
                     p_CLKFBOUT_MULT=6*pll_pre_multiplier, p_DIVCLK_DIVIDE=1,
                     i_CLKIN1=clk, i_CLKFBIN=iodelay_pll_fb, o_CLKFBOUT=iodelay_pll_fb,

                     # 200/300MHz
                     p_CLKOUT0_DIVIDE=iodelay_dividers[core_config["iodelay_clk_freq"]], p_CLKOUT0_PHASE=0.0,
                     o_CLKOUT0=iodelay_pll_iodelay
            ),
            Instance("BUFG", i_I=iodelay_pll_iodelay, o_O=self.cd_iodelay.clk),
            AsyncResetSynchronizer(self.cd_iodelay, ~iodelay_pll_locked | reset),
        ]

        reset_counter = Signal(4, reset=15)
        ic_reset = Signal(reset=1)
        self.sync.iodelay += \
            If(reset_counter != 0,
                reset_counter.eq(reset_counter - 1)
            ).Else(
                ic_reset.eq(0)
            )
        self.specials += Instance("IDELAYCTRL", i_REFCLK=ClockSignal("iodelay"), i_RST=ic_reset)


class LiteDRAMCoreControl(Module, AutoCSR):
    def __init__(self):
        self.init_done = CSRStorage()
        self.init_error = CSRStorage()


class LiteDRAMCore(SoCSDRAM):
    csr_map = {
        "ddrctrl":   16,
        "ddrphy":    17
    }
    csr_map.update(SoCSDRAM.csr_map)
    def __init__(self, platform, core_config, **kwargs):
        platform.add_extension(get_common_ios())
        sys_clk_freq = core_config["sys_clk_freq"]
        SoCSDRAM.__init__(self, platform, sys_clk_freq,
            cpu_type=core_config["cpu"],
            l2_size=32*core_config["sdram_module_nb"],
            reserve_nmi_interrupt=False,
            csr_data_width=8 if core_config["cpu"] is not None else 32,
            with_uart=core_config["cpu"] is not None,
            with_timer=core_config["cpu"] is not None,
            csr_expose=True,
            **kwargs)

        # crg
        self.submodules.crg = LiteDRAMCRG(platform, core_config)

        # sdram
        platform.add_extension(get_dram_ios(core_config))
        self.submodules.ddrphy = core_config["sdram_phy"](platform.request("ddram"), sys_clk_freq=sys_clk_freq,
            iodelay_clk_freq=core_config["iodelay_clk_freq"])
        self.ddrphy.settings.add_electrical_settings(
             rtt_nom=core_config["rtt_nom"],
             rtt_wr=core_config["rtt_wr"],
             ron=core_config["ron"])
        sdram_module = core_config["sdram_module"](sys_clk_freq, "1:4")
        controller_settings = controller_settings=ControllerSettings(
            cmd_buffer_depth=core_config["cmd_buffer_depth"],
            read_time=core_config["read_time"],
            write_time=core_config["write_time"])
        self.register_sdram(self.ddrphy,
                            sdram_module.geom_settings,
                            sdram_module.timing_settings,
                            controller_settings=controller_settings)

        # sdram init
        self.submodules.ddrctrl = LiteDRAMCoreControl()
        self.add_constant("DDRPHY_HIGH_SKEW_DISABLE", None)
        self.comb += [
            platform.request("init_done").eq(self.ddrctrl.init_done.storage),
            platform.request("init_error").eq(self.ddrctrl.init_error.storage)
        ]

        # user port
        self.comb += [
            platform.request("user_clk").eq(ClockSignal()),
            platform.request("user_rst").eq(ResetSignal())
        ]
        if core_config["user_ports_type"] == "native":
            for i in range(core_config["user_ports_nb"]):
                user_port = self.sdram.crossbar.get_port()
                platform.add_extension(get_native_user_port_ios(i,
                    user_port.address_width,
                    user_port.data_width))
                _user_port_io = platform.request("user_port", i)
                self.comb += [
                    # cmd
                    user_port.cmd.valid.eq(_user_port_io.cmd_valid),
                    _user_port_io.cmd_ready.eq(user_port.cmd.ready),
                    user_port.cmd.we.eq(_user_port_io.cmd_we),
                    user_port.cmd.addr.eq(_user_port_io.cmd_addr),

                    # wdata
                    user_port.wdata.valid.eq(_user_port_io.wdata_valid),
                    _user_port_io.wdata_ready.eq(user_port.wdata.ready),
                    user_port.wdata.we.eq(_user_port_io.wdata_we),
                    user_port.wdata.data.eq(_user_port_io.wdata_data),

                    # rdata
                    _user_port_io.rdata_valid.eq(user_port.rdata.valid),
                    user_port.rdata.ready.eq(_user_port_io.rdata_ready),
                    _user_port_io.rdata_data.eq(user_port.rdata.data),
                ]
        elif core_config["user_ports_type"] == "axi":
            for i in range(core_config["user_ports_nb"]):
                user_port = self.sdram.crossbar.get_port()
                axi_port = LiteDRAMAXIPort(
                    user_port.data_width,
                    user_port.address_width + log2_int(user_port.data_width//8),
                    core_config["user_ports_id_width"])
                axi2native = LiteDRAMAXI2Native(axi_port, user_port)
                self.submodules += axi2native
                platform.add_extension(get_axi_user_port_ios(i,
                        axi_port.address_width,
                        axi_port.data_width,
                        core_config["user_ports_id_width"]))
                _axi_port_io = platform.request("user_port", i)
                self.comb += [
                    # aw
                    axi_port.aw.valid.eq(_axi_port_io.aw_valid),
                    _axi_port_io.aw_ready.eq(axi_port.aw.ready),
                    axi_port.aw.addr.eq(_axi_port_io.aw_addr),
                    axi_port.aw.burst.eq(_axi_port_io.aw_burst),
                    axi_port.aw.len.eq(_axi_port_io.aw_len),
                    axi_port.aw.size.eq(_axi_port_io.aw_size),
                    axi_port.aw.id.eq(_axi_port_io.aw_id),

                    # w
                    axi_port.w.valid.eq(_axi_port_io.w_valid),
                    _axi_port_io.w_ready.eq(axi_port.w.ready),
                    axi_port.w.last.eq(_axi_port_io.w_last),
                    axi_port.w.strb.eq(_axi_port_io.w_strb),
                    axi_port.w.data.eq(_axi_port_io.w_data),

                    # b
                    _axi_port_io.b_valid.eq(axi_port.b.valid),
                    axi_port.b.ready.eq(_axi_port_io.b_ready),
                    _axi_port_io.b_resp.eq(axi_port.b.resp),
                    _axi_port_io.b_id.eq(axi_port.b.id),

                    # ar
                    axi_port.ar.valid.eq(_axi_port_io.ar_valid),
                    _axi_port_io.ar_ready.eq(axi_port.ar.ready),
                    axi_port.ar.addr.eq(_axi_port_io.ar_addr),
                    axi_port.ar.burst.eq(_axi_port_io.ar_burst),
                    axi_port.ar.len.eq(_axi_port_io.ar_len),
                    axi_port.ar.size.eq(_axi_port_io.ar_size),
                    axi_port.ar.id.eq(_axi_port_io.ar_id),

                    # r
                    _axi_port_io.r_valid.eq(axi_port.r.valid),
                    axi_port.r.ready.eq(_axi_port_io.r_ready),
                    _axi_port_io.r_last.eq(axi_port.r.last),
                    _axi_port_io.r_resp.eq(axi_port.r.resp),
                    _axi_port_io.r_data.eq(axi_port.r.data),
                    _axi_port_io.r_id.eq(axi_port.r.id),
                ]
        else:
            raise ValueError("Unsupported port type: {}".format(core_config["user_ports_type"]))


def main():
    # get config
    if len(sys.argv) < 2:
        print("missing config file")
        exit(1)
    exec(open(sys.argv[1]).read(), globals())

    # generate core
    platform = Platform()
    soc = LiteDRAMCore(platform, core_config, integrated_rom_size=0x6000)
    builder = Builder(soc, output_dir="build", compile_gateware=False)
    vns = builder.build(build_name="litedram_core", regular_comb=False)

    # prepare core (could be improved)
    def replace_in_file(filename, _from, _to):
        # Read in the file
        with open(filename, "r") as file :
            filedata = file.read()

        # Replace the target string
        filedata = filedata.replace(_from, _to)

        # Write the file out again
        with open(filename, 'w') as file:
            file.write(filedata)

    init_filename = "mem.init"
    os.system("mv build/gateware/{} build/gateware/litedram_core.init".format(init_filename))
    replace_in_file("build/gateware/litedram_core.v", init_filename, "litedram_core.init")

if __name__ == "__main__":
    main()
