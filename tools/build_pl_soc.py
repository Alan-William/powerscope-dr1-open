"""Reproduce the NPU coexistence candidate from the user's local vendor archive.

Never configures hardware. Vendor files and generated products stay outside Git.
This project-specific recipe retains the vendor's high placement optimization;
the generic anlogic-fpga-workflow recipe deliberately rejects that custom setting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def timing_clock_requirements(clock_plan="vendor", profile="full"):
    required = {
        "npu": (150, "design_0_PLL_npu_Inst/"),
        "gp0": (75, "design_0_pll_75m_Inst/"),
        "ps_npu_ddr": (150, ".GEN_PL_HP0"),
        "ps_video_ddr": (150, ".GEN_PL_HP1"),
        "ps_npu_control": (150, ".GEN_PL_GP1S"),
        "ps_gp0_control": (75, ".GEN_PL_GP0S"),
    }
    if clock_plan == "shared-axi":
        prefix = "design_0_inst/design_0_pll_75m_Inst/dr1_phy_pll_1f386b1db2c3_Inst/u_DR1_PHY_PLL.clkc"
        required.update(npu=(150, prefix + "[1]"), gp0=(75, prefix + "[0]"))
    elif clock_plan != "vendor":
        raise ValueError(f"Unknown clock plan: {clock_plan}")
    if profile == "headless":
        del required["ps_video_ddr"]  # HP1 is tied idle; no video DMA loads remain.
    elif profile not in ("full", "display"):
        raise ValueError(f"Unknown SoC profile: {profile}")
    if profile == "display":
        required["hdmi_pixel"] = (148.5, "dr1_pll_vtc_Inst/u_DR1_PHY_PLL.clkc[0]")
        # TD rounds the inherited period to ps before deriving the x5 clock.
        required["hdmi_serial"] = (742.942, "design_0_PLL_3_Inst/dr1_phy_pll_58e32fb6dd07_Inst/u_DR1_PHY_PLL.clkc[0]")
    return required


def verify_timing_clocks(report, clock_plan="vendor", profile="full"):
    """Check the actual timing clock table, including PS-side AXI clock models."""
    entries = re.findall(r"^\s*clk\d+:\s+([\d.]+)\s+(\d+)\s+(\S+)", report, re.MULTILINE)
    required = timing_clock_requirements(clock_plan, profile)
    clocks = {}
    for name, (expected, fragment) in required.items():
        matches = [(float(freq), int(fanout), clock) for freq, fanout, clock in entries if fragment in clock]
        if len(matches) != 1 or abs(matches[0][0] - expected) > 0.1 or matches[0][1] <= 0:
            raise ValueError(f"Unverified timing clock {name}: expected {expected} MHz, got {matches}")
        clocks[name] = {"mhz": matches[0][0], "fanout": matches[0][1], "clock": matches[0][2]}
    return clocks


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Vendor layout changed: expected one {old!r}, got {text.count(old)}")
    return text.replace(old, new, 1)


def share_axi_clock_pll(text):
    """Use the vendor's already configured 75/150 MHz, zero-phase PLL outputs.

    C0 already has its feedback BUFG. Add a BUFG to C1, then feed all NPU/video
    150 MHz consumers and their existing reset synchronizers from that clock.
    The now-unused standalone NPU/video PLLs can be removed by synthesis.
    """
    for name in ("PLL_npu", "pll_150m"):
        text = replace_once(text, f"assign w_net__{name}__clk0_out = w_port__{name}__clk0_out ;",
                            f"assign w_net__{name}__clk0_out = powerscope_clk150 ;")
        text = replace_once(text, f"assign w_net__{name}__lock = w_port__{name}__lock ;",
                            f"assign w_net__{name}__lock = w_port__pll_75m__lock ;")
    old = ".clk0_out(w_port__pll_75m__clk0_out)\n                                          , .clk1_out()"
    text = replace_once(text, old, old.replace(".clk1_out()", ".clk1_out(powerscope_clk150_raw)"))
    # Declare before the PLL port connection so TD never creates an implicit net.
    header_end = text.index(");", text.index("module design_0 (")) + 2
    text = text[:header_end] + "\nwire powerscope_clk150_raw, powerscope_clk150;\n" + text[header_end:]
    glue = ("\n// Shared AXI clocks: vendor PLL C0=75 MHz, C1=150 MHz; both phase 0.\n"
            "DR1_LOGIC_BUFG powerscope_clk150_buf(.i(powerscope_clk150_raw),.o(powerscope_clk150));\n")
    return replace_once(text, "endmodule", glue + "endmodule")


def shared_clock_sdc():
    # Do not copy the old NPU-vs-GP0 clock-group cut onto the merged 150 MHz
    # domain: that would newly exclude GP0-vs-video paths. Analyze all shared
    # 75/150 MHz paths. Keep explicit reset and first RX-stage exceptions only;
    # IP-level CDC constraints are loaded separately by the build recipe.
    return """# Shared AXI PLL: derive frequency/phase from real PLL parameters.
derive_clocks
set_false_path -from [get_nets {design_0_inst/design_0_ARM_Processor_System_0_Inst/p2f_rst0_n}]
set_false_path -from [get_ports {I_rs485_rx}] -to [get_regs {design_0_inst/powerscope_core/modbus/receiver/rx_meta}]
# This MIPIIO target is absent in the checked design, which uses an LVDS wrapper.
# If present in another revision, retain the original constraint for that target.
set mipi_target [get_pins -quiet {design_top_wrapper_u/u_mipi_dphy_rx_dr1_mipiio_wrapper/u_DR1_mipiio_wrapper/u_DR1_PHY_MIPIIO.hsrx_byteclk_to_fabric}]
if {[llength $mipi_target]} { create_clock -name clk_mipi -period 6.4 -waveform {0 3.2} $mipi_target }
"""


def headless_design(text, ps_wrapper):
    """Keep the checked NPU/PS cone, replacing the GP0 video matrix with DECERR.

    Retained vendor instances and signal assignments are copied from the exact
    verified archive, not recreated from assumed AXI widths or IRQ numbers.
    """
    keep = {
        "mipi_iic_i2c0_scl_DR1_LOGIC_IOTRIBUF_0",
        "mipi_iic_i2c0_sda_DR1_LOGIC_IOTRIBUF_0",
        "design_0_AXI_Protocol_Converter_0_Inst",
        "design_0_ARM_Processor_System_0_Inst", "design_0_Concat_0_Inst",
        "design_0_pll_75m_Inst", "design_0_rst_n_75m_Inst",
        "design_0_rstn_npu_Inst", "design_0_pwm_0_Inst",
        "design_0_AXI_Matrix_1_Inst", "design_0_AXI_Protocol_Converter_1_Inst",
        "design_0_Video_NPU_0_Inst",
    }
    pattern = r"^    (\w+) (\w+) \(\s*(.*?)\);"
    instances = list(re.finditer(pattern, text, re.MULTILINE | re.DOTALL))
    if len(instances) != 27 or not keep.issubset({m[2] for m in instances}):
        raise ValueError("Vendor instance inventory changed; headless pruning stopped")
    blocks = []
    hp1_ports = re.findall(r"^\s*(input|output)\s+(?:\[(\d+):0\]\s+)?(slave_hp1_axi_\w+)\s*[,;]", ps_wrapper, re.MULTILINE)
    if len(hp1_ports) != 38:
        raise ValueError(f"Unexpected PS HP1 port inventory: {len(hp1_ports)}")
    for m in instances:
        if m[2] not in keep:
            continue
        block = m[0]
        if m[2] == "design_0_ARM_Processor_System_0_Inst":
            for direction, msb, port in hp1_ports:
                if port == "slave_hp1_axi_aclk":
                    continue  # Keep the configured PS interface clock running.
                value = f"{int(msb)+1 if msb else 1}'b0" if direction == "input" else ""
                block, count = re.subn(r"\." + port + r"\([^()]*\)", f".{port}({value})", block)
                if count != 1:
                    raise ValueError(f"Unexpected PS port binding: {port}")
        if m[2] == "design_0_Concat_0_Inst":
            # Original NPU IRQ is In15. In0 is camera, In2 is video DMA.
            for index in (0, 2):
                block = replace_once(block, f".In{index}(w_port__Concat_0__In{index})", f".In{index}(1'b0)")
        blocks.append(block)
    # The router's original m0 nets retain their names, but terminate in DECERR.
    ports = [".clk(w_port__AXI_Protocol_Converter_0__aclk)", ".resetn(w_port__AXI_Protocol_Converter_0__aresetn)"]
    for signal in ("awvalid", "awready", "wvalid", "wready", "bresp", "bvalid", "bready",
                   "arvalid", "arready", "rdata", "rresp", "rvalid", "rready"):
        ports.append(f".s_{signal}(w_port__AXI_Matrix_0__us0_{signal})")
    blocks.append("ps_axi_lite_error powerscope_unmapped(\n" + ",\n".join(ports) + "\n);")
    extra = text[text.index("\nwire [32:0] ps_awaddr;"):text.rindex("endmodule")]
    assignments = dict(re.findall(r"^\s*assign\s+(\w+)\s*=\s*([^;]+);", text, re.MULTILINE))
    assignments.update(npu_ready="w_port__rstn_npu__peripheral_aresetn",
                       gp0_ready="w_port__rst_n_75m__interconnect_aresetn")
    declarations = {m[2]: m[0].strip() for m in re.finditer(r"^\s*wire\s+(\[[^\]]+\]\s*)?(\w+)\s*;", text, re.MULTILINE)}
    # Follow the original generated assign graph backwards from all kept ports.
    symbols = set(declarations) | set(assignments) | {"powerscope_clk150", "powerscope_clk150_raw"}
    def references(expression):
        return set(re.findall(r"\b[A-Za-z_]\w*\b", expression)) & symbols
    needed = references("\n".join(blocks) + extra) | {"pwm_out", "npu_ready", "gp0_ready"}
    while True:
        expanded = needed | set().union(*(references(assignments[n]) for n in needed if n in assignments))
        if expanded == needed:
            break
        needed = expanded
    header = """// Generated headless profile: PS/NPU configuration and NPU connections retained.
module design_0 (
    input wire I_rs485_rx, output wire O_rs485_tx, output wire O_rs485_de,
    inout wire mipi_iic_i2c0_scl_io, inout wire mipi_iic_i2c0_sda_io,
    output wire pwm_out, output wire npu_ready, output wire gp0_ready
);
wire powerscope_clk150_raw, powerscope_clk150;
"""
    # extra carries the ps_* declarations; avoid duplicating them here.
    kept_declarations = [line for name, line in declarations.items() if name in needed and not name.startswith("ps_")]
    kept_assignments = [f"assign {name} = {value.strip()};" for name, value in assignments.items() if name in needed]
    design = header + "\n".join(kept_declarations + kept_assignments + blocks) + extra + "endmodule\n"
    removed_markers = ("w_port__VDMA_0__", "w_port__video_timing_generator_v01_0__", "w_port__AXI4_Stream_to_Video_Out_0__",
                       "w_port__AXI_Matrix_2__", "w_port__fvdma_ctrl_0__", "hp1_w_us1", "w_net__f2p_irq")
    if any(marker in design for marker in removed_markers):
        raise ValueError("A removed camera/video connection is still needed")
    return design, {"retained_vendor_instances": sorted(keep),
                    "removed_vendor_instances": [m[2] for m in instances if m[2] not in keep],
                    "hp1": "Configured PS clock retained; all transaction inputs idle, outputs disconnected",
                    "irq": "Original NPU at Concat In15 preserved; camera In0 and VDMA In2 tied low"}


def headless_top():
    return """// NPU/AXI/RS485 only. Camera held in reset with its clock stopped.
module system_top(
    input wire I_rs485_rx, output wire O_rs485_tx, output wire O_rs485_de,
    inout wire SCL0, inout wire SDA0, output wire [2:0] led,
    output wire O_imax334_rst_n, output wire O_imx334_clk_24m
);
assign O_imax334_rst_n = 1'b0;
assign O_imx334_clk_24m = 1'b0;
design_0 design_0_inst(
    .I_rs485_rx(I_rs485_rx), .O_rs485_tx(O_rs485_tx), .O_rs485_de(O_rs485_de),
    .mipi_iic_i2c0_scl_io(SCL0), .mipi_iic_i2c0_sda_io(SDA0),
    .pwm_out(led[0]), .npu_ready(led[1]), .gp0_ready(led[2])
);
endmodule
"""


def display_design(text, matrix_wrapper):
    """Retain the factory framebuffer/HDMI pipeline alongside the accepted NPU.

    Camera capture is not needed for a PS framebuffer. Its HP1 master is tied
    idle and its former AXI-Lite registers return DECERR. All video register
    addresses, PS configuration, NPU connections and HDMI pins remain original.
    """
    remove = {"design_0_PLL_npu_Inst", "design_0_pll_150m_Inst",
              "design_0_fvdma_ctrl_0_Inst", "design_0_pwm_0_0_Inst", "design_0_pwm_0_1_Inst"}
    pattern = r"^    (\w+) (\w+) \(\s*(.*?)\);"
    instances = list(re.finditer(pattern, text, re.MULTILINE | re.DOTALL))
    if len(instances) != 27 or not remove.issubset({m[2] for m in instances}):
        raise ValueError("Vendor instance inventory changed; display pruning stopped")
    camera_ports = re.findall(r"^\s*(input|output)\s+(?:\[(\d+):0\]\s+)?(us1_\w+)\s*[,;]", matrix_wrapper, re.MULTILINE)
    if len(camera_ports) != 37:
        raise ValueError(f"Unexpected HP1 camera-master port inventory: {len(camera_ports)}")
    blocks = []
    for m in instances:
        if m[2] in remove:
            continue
        block = m[0]
        if m[2] == "design_0_AXI_Matrix_2_Inst":
            for direction, msb, port in camera_ports:
                value = f"{int(msb)+1 if msb else 1}'b0" if direction == "input" else ""
                block, count = re.subn(r"\." + port + r"\([^()]*\)", f".{port}({value})", block)
                if count != 1:
                    raise ValueError(f"Unexpected HP1 camera binding: {port}")
        if m[2] == "design_0_Concat_0_Inst":
            block = replace_once(block, ".In0(w_port__Concat_0__In0)", ".In0(1'b0)")
        blocks.append(block)
    ports = [".clk(w_port__AXI_Matrix_0__aclk)", ".resetn(w_port__AXI_Matrix_0__aresetn)"]
    for signal in ("awvalid", "awready", "wvalid", "wready", "bresp", "bvalid", "bready",
                   "arvalid", "arready", "rdata", "rresp", "rvalid", "rready"):
        ports.append(f".s_{signal}(w_port__AXI_Matrix_0__ds2_{signal})")
    blocks.append("ps_axi_lite_error powerscope_camera_unmapped(\n" + ",\n".join(ports) + "\n);")
    extra = text[text.index("\nwire [32:0] ps_awaddr;"):text.rindex("endmodule")]
    assignments = dict(re.findall(r"^\s*assign\s+(\w+)\s*=\s*([^;]+);", text, re.MULTILINE))
    assignments.update(npu_ready="w_port__rstn_npu__peripheral_aresetn",
                       gp0_ready="w_port__rst_n_75m__interconnect_aresetn")
    # Delete the old camera slave's output aliases: the DECERR slave now drives
    # these matrix pins directly. Otherwise dependency pruning would retain
    # undriven fvdma_ctrl output wires and introduce a second driver.
    response = ("awready", "wready", "bresp", "bvalid", "arready", "rdata", "rresp", "rvalid")
    for signal in response:
        name = f"w_port__AXI_Matrix_0__ds2_{signal}"
        if "fvdma_ctrl_0" not in assignments.get(name, ""):
            raise ValueError(f"Unexpected camera-slave response: {name}")
        del assignments[name]
    declarations = {m[2]: m[0].strip() for m in re.finditer(r"^\s*wire\s+(\[[^\]]+\]\s*)?(\w+)\s*;", text, re.MULTILINE)}
    symbols = set(declarations) | set(assignments) | {"powerscope_clk150", "powerscope_clk150_raw"}
    def references(expression):
        return set(re.findall(r"\b[A-Za-z_]\w*\b", expression)) & symbols
    needed = references("\n".join(blocks) + extra) | {"pwm_out", "npu_ready", "gp0_ready", "O_hdmi_clk_p", "O_hdmi_tx_p"}
    while True:
        expanded = needed | set().union(*(references(assignments[n]) for n in needed if n in assignments))
        if expanded == needed:
            break
        needed = expanded
    header = """// PS framebuffer/HDMI plus NPU/AXI/RS485; camera capture held idle.
module design_0 (
    input wire I_rs485_rx, output wire O_rs485_tx, output wire O_rs485_de,
    inout wire mipi_iic_i2c0_scl_io, inout wire mipi_iic_i2c0_sda_io,
    output wire pwm_out, output wire npu_ready, output wire gp0_ready,
    output wire O_hdmi_clk_p, output wire [2:0] O_hdmi_tx_p
);
wire powerscope_clk150_raw, powerscope_clk150;
"""
    kept_declarations = [line for name, line in declarations.items() if name in needed and not name.startswith("ps_")]
    kept_assignments = [f"assign {name} = {value.strip()};" for name, value in assignments.items() if name in needed]
    design = header + "\n".join(kept_declarations + kept_assignments + blocks) + extra + "endmodule\n"
    if any(marker in design for marker in ("w_port__fvdma_ctrl_0__", "hp1_w_us1", "w_net__f2p_irq")):
        raise ValueError("A removed camera connection is still needed")
    return design, {"retained_vendor_instances": [m[2] for m in instances if m[2] not in remove],
                    "removed_vendor_instances": sorted(remove),
                    "hp1": "VDMA read master retained; camera master inputs idle, outputs disconnected",
                    "irq": "NPU In15 and VDMA In2 retained; camera In0 tied low",
                    "display": "Original VDMA, VTG, async stream converter and HDMI serializer",
                    "camera_registers": "Original matrix ds2 at 0x80300000 returns DECERR"}


def display_top():
    top = headless_top().replace("// NPU/AXI/RS485 only.", "// Framebuffer/HDMI with NPU/AXI/RS485.")
    top = replace_once(top, "module system_top(", "module system_top(\n    output wire O_hdmi_clk_p, output wire [2:0] O_hdmi_tx_p,")
    return replace_once(top, "design_0 design_0_inst(", "design_0 design_0_inst(\n    .O_hdmi_clk_p(O_hdmi_clk_p), .O_hdmi_tx_p(O_hdmi_tx_p),")


def display_cdc_sdc():
    """Constrain the vendor FIFO's real synchronizers, not whole clock domains.

    TD's ip/ph2a/fifo_sdc.tcl specifies a datapath-only maximum of the shorter
    clock period minus 0.3 ns for Gray-pointer to first-stage crossings. The
    routed vendor netlist confirms five Gray bits, then sync_r1, then
    primary_addr_gray_sync in each direction. Normal stage-to-stage timing
    remains enabled. The pixel reset has two async-assert/sync-release stages;
    only its asynchronous source-to-synchronizer paths are excepted.
    """
    prefix = ("design_0_inst/design_0_AXI4_Stream_to_Video_Out_0_Inst/"
              "alc_axi4s_vid_out_7d63f5fadd80_Inst/inst/COUPLER_INST/generate_async_fifo$FIFO_INST")
    lines = ["# Video FIFO: TD's Gray-pointer maximum-delay prescription, 150 MHz minus 0.3 ns."]
    # TD's SDC reader parses one Tcl command per line; do not emit multiline Tcl.
    for direction in ("rd_to_wr_cross_inst", "wr_to_rd_cross_inst"):
        gray = f"[get_regs -quiet {{{prefix}/{direction}/primary_addr_gray_reg[*]}}]"
        meta = f"[get_regs -quiet {{{prefix}/{direction}/sync_r1[*]}}]"
        lines += [f'if {{[llength {gray}]!=5 || [llength {meta}]!=5}} {{error "Video FIFO pointer register inventory changed"}}',
                  f"set_max_delay -from {gray} -to {meta} -datapath_only 6.366"]
    reset = f"[get_nets -quiet {{{prefix}/rst}}]"
    rst0 = f"[get_regs -quiet {{{prefix}/asy_r_rst0}}]"
    rst1 = f"[get_regs -quiet {{{prefix}/asy_r_rst1}}]"
    lines += ["# Async reset input of the verified two-stage pixel-domain reset synchronizer.",
              f'if {{[llength {reset}]!=1 || [llength {rst0}]!=1 || [llength {rst1}]!=1}} {{error "Video FIFO reset synchronizer inventory changed"}}',
              f"set_false_path -from {reset} -to {rst0}",
              f"set_false_path -from {reset} -to {rst1}"]
    return "\n".join(lines) + "\n"


def insert_bridge(text):
    # These connections and widths are checked against the extracted generated RTL,
    # rather than relying on an assumed fvdma register base or truncating addresses.
    forward = {"awaddr": 33, "awprot": 3, "awvalid": 1, "wdata": 32, "wstrb": 4,
               "wvalid": 1, "bready": 1, "araddr": 33, "arprot": 3, "arvalid": 1, "rready": 1}
    backward = {"awready": 1, "wready": 1, "bresp": 2, "bvalid": 1,
                "arready": 1, "rdata": 32, "rresp": 2, "rvalid": 1}
    ports = [".clk(w_port__AXI_Protocol_Converter_0__aclk)", ".resetn(w_port__AXI_Protocol_Converter_0__aresetn)"]
    declarations = []
    for signal, width in (forward | backward).items():
        declarations.append(f"wire {f'[{width-1}:0]' if width>1 else ''} ps_{signal};")
        if signal in forward:
            line = f"assign w_port__AXI_Matrix_0__us0_{signal} = w_net__AXI_Protocol_Converter_0__ds_axi_{signal} ;"
            source = f"w_net__AXI_Protocol_Converter_0__ds_axi_{signal}"
            target = f"w_port__AXI_Matrix_0__us0_{signal}"
        else:
            line = f"assign w_port__AXI_Protocol_Converter_0__ds_axi_{signal} = w_net__AXI_Matrix_0__us0_{signal} ;"
            source = f"w_port__AXI_Protocol_Converter_0__ds_axi_{signal}"
            target = f"w_net__AXI_Matrix_0__us0_{signal}"
        text = replace_once(text, line, "// PowerScope: original direct link replaced by checked AXI-Lite router.")
        ports += [f".s_{signal}({source})", f".m_{signal}({{ps_{signal},{target}}})"]
    glue = "\n".join(declarations) + "\nps_axi_lite_router powerscope_router(\n" + ",\n".join(ports) + "\n);\n"
    ports = [".clk(w_port__AXI_Protocol_Converter_0__aclk)", ".resetn(w_port__AXI_Protocol_Converter_0__aresetn)"]
    for signal in forward | backward:
        if signal not in ("awprot", "arprot"):
            suffix = "[15:0]" if signal in ("awaddr", "araddr") else ""
            ports.append(f".s_axi_{signal}(ps_{signal}{suffix})")
    ports += [".rs485_rx(I_rs485_rx)", ".rs485_tx(O_rs485_tx)", ".rs485_de(O_rs485_de)"]
    glue += "powerscope_peripheral #(.CLOCK_HZ(75000000)) powerscope_core(\n" + ",\n".join(ports) + "\n);\n"
    text = replace_once(text, "module design_0 (", "module design_0 (\n    input wire I_rs485_rx, output wire O_rs485_tx, output wire O_rs485_de,")
    return replace_once(text, "endmodule", glue + "endmodule")


def stage(archive, work, clock_plan="vendor", profile="full"):
    if profile not in ("full", "headless", "display") or (profile != "full" and clock_plan != "shared-axi"):
        raise ValueError("The headless/display profiles require the verified shared-axi clock plan")
    archive_hash = sha(archive)
    if clock_plan == "shared-axi" and archive_hash != "f754663eded613cf15ac2ceb241ac91533792d360c4fdb49ba4105d887ba70ae":
        raise ValueError("Shared PLL plan requires the verified vendor archive; recheck PLL parameters before adapting another archive")
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            if not member.filename.startswith("soc_prj/") or member.is_dir():
                continue
            if "_Runs/" in member.filename or member.filename.endswith((".bit", ".db", ".log")):
                continue
            target = (work / member.filename).resolve()
            if not target.is_relative_to(work.resolve()) or not str(target).isascii():
                raise ValueError("Unsafe or non-ASCII vendor path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read(member))
    project = work / "soc_prj"
    design = project / "design_0/hdl/design_0.v"
    original_design_hash = sha(design)
    text = design.read_text(encoding="utf-8-sig")
    if "assign w_port__AXI_Protocol_Converter_0__aclk = w_net__pll_75m__clk0_out ;" not in text:
        raise ValueError("Unexpected peripheral clock")
    text = insert_bridge(text)
    if clock_plan == "shared-axi":
        text = share_axi_clock_pll(text)
    profile_details = None
    if profile == "headless":
        ps_wrapper = project / "design_0/ip/design_0_ARM_Processor_System_0/design_0_ARM_Processor_System_0.v"
        text, profile_details = headless_design(text, ps_wrapper.read_text(encoding="utf-8-sig"))
    elif profile == "display":
        matrix_wrapper = project / "design_0/ip/design_0_AXI_Matrix_2/design_0_AXI_Matrix_2.v"
        text, profile_details = display_design(text, matrix_wrapper.read_text(encoding="utf-8-sig"))
    design.write_text(text, encoding="utf-8")
    top = project / "src/system_top.v"
    text = top.read_text(encoding="utf-8-sig")
    text = replace_once(text, "module system_top(", "module system_top(\n    input wire I_rs485_rx, output wire O_rs485_tx, output wire O_rs485_de,")
    text, count = re.subn(r"(design_0\s+design_0_inst\s*\()", r"\1\n.I_rs485_rx(I_rs485_rx),.O_rs485_tx(O_rs485_tx),.O_rs485_de(O_rs485_de),", text)
    if count != 1:
        raise ValueError("Unexpected top-level design instance")
    top.write_text(headless_top() if profile == "headless" else display_top() if profile == "display" else text, encoding="utf-8")
    io = project / "src/constraint/io.adc"
    io_text = io.read_text(encoding="utf-8-sig")
    for pin in ("L17", "N16", "N15"):
        if re.search(rf"LOCATION\s*=\s*{pin}\s*;", io_text):
            raise ValueError(f"RS485 pin collision: {pin}")
    io_text += "\n# Board 05_rs485 pin assignment, BANK33 LVCMOS33\n"
    for port, pin in [("I_rs485_rx", "N16"), ("O_rs485_tx", "L17"), ("O_rs485_de", "N15")]:
        io_text += f"set_pin_assignment {{{port}}} {{ LOCATION = {pin}; IOSTANDARD = LVCMOS33; }}\n"
    if profile in ("headless", "display"):
        active_ports = {"I_rs485_rx", "O_rs485_tx", "O_rs485_de", "SCL0", "SDA0", "led[0]", "led[1]", "led[2]",
                        "O_imax334_rst_n", "O_imx334_clk_24m"}
        if profile == "display":
            active_ports.update({"O_hdmi_clk_p", "O_hdmi_tx_p[0]", "O_hdmi_tx_p[1]", "O_hdmi_tx_p[2]"})
        lines = []
        for line in io_text.splitlines():
            pin = re.match(r"\s*set_pin_assignment\s*\{\s*(\S+)\s*\}", line)
            if pin and pin[1] in active_ports:
                lines.append(line)
        if len(lines) != len(active_ports):
            raise ValueError(f"{profile} I/O constraint inventory differs from the verified board pins")
        io_text = f"# {profile} profile: original locations/standards for retained pins.\n" + "\n".join(lines) + "\n"
    io.write_text(io_text, encoding="utf-8")
    timing = project / "src/constraint/timing.sdc"
    if clock_plan == "shared-axi":
        sdc = shared_clock_sdc()
        if profile in ("headless", "display"):
            sdc = sdc[:sdc.index("# This MIPIIO")]
        if profile == "display":
            # Apply the internal FIFO constraints after mapping, when the
            # synchronizer inventory can be checked against mapped registers.
            # Placement and routing must use these constraints, so optimization
            # targets actual synchronous paths instead of asynchronous phases.
            (timing.parent / "timing-implementation.sdc").write_text(sdc, encoding="ascii")
            sdc += display_cdc_sdc()
        timing.write_text(sdc, encoding="ascii")
    else:
        with timing.open("a", encoding="utf-8") as output:
            output.write("\n# External UART input is asynchronous; only the first RX synchronizer stage is excepted.\n")
            output.write("set_false_path -from [get_ports {I_rs485_rx}] -to [get_regs {design_0_inst/powerscope_core/modbus/receiver/rx_meta}]\n")
    shutil.copytree(ROOT / "pl/rtl", project / "src/powerscope")
    al = project / "soc_prj.al"
    text = al.read_text(encoding="utf-8-sig")
    text = re.sub(r'(<Project\b[^>]*\bPath=")[^"]*(")', lambda m: m[1] + project.as_posix() + m[2], text, count=1)
    added = ""
    for index, path in enumerate(sorted((project / "src/powerscope").glob("*.v")), 100):
        added += f'''<File Path="src/powerscope/{path.name}"><FileInfo>
<Attr Name="UsedInSyn" Val="true"/><Attr Name="UsedInP&R" Val="true"/>
<Attr Name="BelongTo" Val="design_1"/><Attr Name="CompileOrder" Val="{index}"/>
</FileInfo></File>\n'''
    text = replace_once(text, "</Verilog>", added + "</Verilog>")
    al.write_text(text, encoding="utf-8")
    metadata = {"vendor_archive_sha256": archive_hash, "original_design_hdl_sha256": original_design_hash,
                "patched_design_hdl_sha256": sha(design), "peripheral_clock_hz": 75000000,
                "profile": profile, "profile_details": profile_details,
                "clock_plan": clock_plan,
                "clock_plan_details": ("Original independent vendor PLLs" if clock_plan == "vendor" else
                                       "Existing pll_75m C0=75 MHz; C1=150 MHz via added BUFG. NPU/GP0 reset synchronizers use the shared PLL lock; the full profile also shares this clock/lock with video. No inherited whole-clock-group cuts."),
                "new_window": {"base": "0x80400000", "size": "0x10000", "source": "ps_axi_lite_router BASE/MASK on GP0 after AXI_Protocol_Converter_0"},
                "retained": ["GP0 video windows return DECERR" if profile == "headless" else "original GP0 matrix and all its addresses", "GP1 soft NPU control 0x1f0000000",
                             "NPU HP0 DDR master", "hard NPU/PS configuration and Ethernet IP properties",
                             "NPU/GP0 frequencies and reset synchronizers; NPU IRQ",
                             "retained pins keep original locations/voltage standards"],
                "hardware_verified": False,
                "project": str(al),
                "project_inputs_sha256": {str(path.relative_to(project)): sha(path) for path in (
                    al, top, design, io, timing,
                    project / "design_0/ip/design_0_ARM_Processor_System_0/design_0_ARM_Processor_System_0.sdc",
                    project / "design_0/ip/design_0_video_timing_generator_v01_0/design_0_video_timing_generator_v01_0.sdc",
                    project / "design_0/ip/design_0_PLL_0/design_0_PLL_0.v",
                    project / "design_0/ip/design_0_PLL_0/RTL/dr1_phy_pll_1f386b1db2c3.v",
                )},
                "source_sha256": {str(p.relative_to(ROOT)): sha(p) for p in (ROOT / "pl/rtl").glob("*.v")},
                "note": "Generated HDL is patched deterministically. Regenerating design_0 in the GUI discards insertion; rerun this staging script."}
    if profile == "display":
        metadata["project_inputs_sha256"]["src/constraint/timing-implementation.sdc"] = sha(timing.parent / "timing-implementation.sdc")
        metadata["display_cdc_signoff"] = "Vendor Gray-pointer datapath bounds and reset-synchronizer exceptions applied after mapping, before placement/routing, and refreshed for final sign-off."
    (work / "integration.json").write_text(json.dumps(metadata, indent=2))
    return al, metadata


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vendor-zip", type=Path, required=True)
    p.add_argument("--work-root", type=Path, default=Path("E:/fpga_workflow_runs"))
    p.add_argument("--td-dir", type=Path, default=Path("D:/Anlogic/TD_Release_2026.1_SP2.200.067"))
    p.add_argument("--stage-only", action="store_true")
    p.add_argument("--clock-plan", choices=("vendor", "shared-axi"), default="shared-axi",
                   help="Shared 75/150 MHz AXI PLL by default; vendor retains the old topology for diagnosis")
    p.add_argument("--profile", choices=("full", "headless", "display"), default="headless",
                   help="Headless NPU/network/AXI/RS485 by default; display adds framebuffer/HDMI, full also retains camera")
    p.add_argument("--timing-effort", choices=("standard", "high"), default="high",
                   help="High adds the route QoR report's recommended fanout/placement and post-route optimization; allow a longer timeout")
    p.add_argument("--timeout", type=int, default=7200)
    args = p.parse_args()
    args.work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="powerscope-soc-", dir=args.work_root))
    al, metadata = stage(args.vendor_zip, work, args.clock_plan, args.profile)
    print(f"Staged: {al}", flush=True)
    if args.stage_only:
        return
    products = work / "products"
    products.mkdir()
    timing, area, bit = (products / name for name in ("timing.rpt", "area.rpt", "image.bit"))
    def quote(path):
        return "{" + Path(path).as_posix() + "}"
    script = work / "build.tcl"
    required_clocks = " ".join(f"{{{name} {mhz} {{{fragment}}}}}"
                               for name, (mhz, fragment) in timing_clock_requirements(args.clock_plan, args.profile).items())
    script.write_text("\n".join([
        f"cd {quote(al.parent)}", "if {[catch {", "import_device dr1_90.db -package DR1M90GEG400 -speed 2",
        f"open_project -single_run {quote(al)}",
        "load_run_param -run syn_1", "set_param rtl async_reg on", "commit_param -step design", "elaborate -top system_top",
        # Same IP constraints as the vendor's generated settings.cfg/DefaultFlow.
        "read_sdc -ip design_0_ARM_Processor_System_0 {design_0/ip/design_0_ARM_Processor_System_0/design_0_ARM_Processor_System_0.sdc}",
        *(["read_sdc -ip design_0_video_timing_generator_v01_0 {design_0/ip/design_0_video_timing_generator_v01_0/design_0_video_timing_generator_v01_0.sdc}"] if args.profile != "headless" else []),
        "read_adc {src/constraint/io.adc}",
        "read_sdc {src/constraint/timing-implementation.sdc}" if args.profile == "display" else "read_sdc {src/constraint/timing.sdc}",
        "commit_param -step rtl", "optimize_rtl", "commit_param -step gate", "optimize_gate", "legalize_phy_inst",
        # Refresh user constraints after mapping, as in the validated single-run flow.
        "clear_sdc -user",
        "read_sdc {src/constraint/timing.sdc}",
        "update_timing", f"report_timing_summary -file {quote(products / 'gate-timing.rpt')}",
        f"export_db {quote(products / 'gate.db')}",
        # Fail before placement if the actual NPU/GP0 clocks were not recognized.
        f"set clock_report [open {quote(products / 'gate-timing.rpt')} r]",
        "set clock_text [read $clock_report]", "close $clock_report",
        f"set required_clocks {{{required_clocks}}}",
        "foreach requirement $required_clocks {",
        "lassign $requirement label expected fragment", "set matches 0",
        "foreach line [split $clock_text \\n] {",
        r"if {[regexp {^\s*clk\d+:\s+([0-9.]+)\s+([0-9]+)\s+(\S+)} $line -> freq fanout name] && [string first $fragment $name] >= 0} {",
        'if {abs($freq-$expected)>0.1 || $fanout<=0} {error "Invalid $label timing clock: $line"}',
        "incr matches", "}", "}",
        'if {$matches!=1} {error "Expected one $label timing clock; found $matches"}', "}",
        "load_run_param -run phy_1",
        *(["set_param place hfn_opt_effort high", "set_param place non_crit_opt on"] if args.timing_effort == "high" else []),
        "commit_param -step place", "place", f"export_db {quote(products / 'place.db')}",
        *(["set_param route post_route_opt high", "set_param route postrt_opt_extra_round 4"] if args.timing_effort == "high" else []),
        "commit_param -step route", "route",
        f"export_db {quote(products / 'routed.db')}",
        *(["clear_sdc -user", "read_sdc {src/constraint/timing.sdc}"] if args.profile == "display" else []),
        f"report_area -io_info -file {quote(area)}", "update_timing -mode final",
        f"report_timing_summary -file {quote(timing)}",
        f"report_timing_status -file {quote(products / 'timing-status.rpt')}",
        f"report_timing_exception -file {quote(products / 'timing-exceptions.rpt')}",
        f"check_timing -verbose -file {quote(products / 'timing-check.rpt')}",
        f"report_clock_utilization -file {quote(products / 'clock-utilization.rpt')}",
        "set rx_meta [get_regs {design_0_inst/powerscope_core/modbus/receiver/rx_meta}]",
        "set rx_sync [get_regs {design_0_inst/powerscope_core/modbus/receiver/rx_sync}]",
        'if {[llength $rx_meta]!=1 || [llength $rx_sync]!=1} {error "Expected both RX synchronizer stages"}',
        f"report_timing_path -from $rx_meta -to $rx_sync -file {quote(products / 'rx-path.rpt')}",
        "report_property $rx_meta", "report_property $rx_sync",
        "commit_param -step bitgen", f"bitgen -bit {quote(bit)}",
        '} flow_error]} { puts stderr "FLOW_FAILED: $flow_error"; exit 1 }',
        'puts "FLOW_BUILD_OK"', "exit 0", ""]), encoding="ascii")
    report = {"status": "failed", "operation": "build", "hardware_accessed": False, "device": "DR1M90GEG400",
              "source_project": str(al), "integration": metadata, "log": str(work / "build-console.log"),
              "timing_effort": args.timing_effort,
              "script_sha256": sha(script), "functional_test": "not performed", "timing_review": "required before deployment"}
    start = time.monotonic()
    try:
        with (work / "build-console.log").open("wb") as log:
            result = subprocess.run([str(args.td_dir / "bin/td_commands_prompt.exe"), script.as_posix()], cwd=work,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
        output = (work / "build-console.log").read_text(encoding="utf-8", errors="replace")
        errors = [line for line in output.splitlines() if re.search(r"\bERROR\b|FLOW_FAILED", line)]
        if result.returncode or "FLOW_BUILD_OK" not in output or errors or not bit.is_file():
            raise RuntimeError(f"TD failed ({result.returncode}): {errors[-8:]}")
        if not all(path.is_file() and path.stat().st_size for path in (bit, timing, area)):
            raise RuntimeError("Missing fresh build products")
        timing_text = timing.read_text(errors="replace")
        clocks = verify_timing_clocks(timing_text, args.clock_plan, args.profile)
        metrics = {}
        for key, pattern in {"setup_wns_ns": r"SWNS:\s*([-+\d.]+)ns", "hold_wns_ns": r"HWNS:\s*([-+\d.]+)ns",
                             "sta_coverage_percent": r"STA coverage\s*:\s*([\d.]+)%"}.items():
            match = re.search(pattern, timing_text)
            if match:
                metrics[key] = float(match[1])
        report.update(status="compiled", bit=str(bit), bit_sha256=sha(bit), bytes=bit.stat().st_size,
                      timing_report=str(timing), area_report=str(area), timing_metrics=metrics, timing_clocks=clocks,
                      deployable=False, deployment_review="Timing coverage, exceptions and physical coexistence still require review")
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        report["seconds"] = time.monotonic() - start
        (work / "result.json").write_text(json.dumps(report, indent=2))
        print(f"Result: {work / 'result.json'}", flush=True)


if __name__ == "__main__":
    main()
