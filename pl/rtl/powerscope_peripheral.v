`timescale 1ns/1ps
// A 64 KiB relative window. No physical address is assigned in this core.
module powerscope_peripheral #(
    parameter CLOCK_HZ=75000000, parameter TIMEOUT_MS=500
)(
    input wire clk, input wire resetn,
    input wire [15:0] s_axi_awaddr, input wire s_axi_awvalid, output wire s_axi_awready,
    input wire [31:0] s_axi_wdata, input wire [3:0] s_axi_wstrb,
    input wire s_axi_wvalid, output wire s_axi_wready,
    output wire [1:0] s_axi_bresp, output wire s_axi_bvalid, input wire s_axi_bready,
    input wire [15:0] s_axi_araddr, input wire s_axi_arvalid, output wire s_axi_arready,
    output wire [31:0] s_axi_rdata, output wire [1:0] s_axi_rresp,
    output wire s_axi_rvalid, input wire s_axi_rready,
    input wire rs485_rx, output wire rs485_tx, output wire rs485_de
);
    wire wr_en,rd_en;
    wire [15:0] wr_addr,rd_addr;
    wire [31:0] wr_data;
    wire [3:0] wr_strb;
    wire [31:0] mb_rd_data,wave_rd_data;
    wire [1:0] mb_wr_resp,wave_wr_resp,mb_rd_resp,wave_rd_resp;
    reg read_mailbox;
    wire write_mailbox=wr_addr<16'h0100;
    wire [159:0] registers;
    wire request_valid,request_crc_error,request_is_read;
    wire [31:0] request_range;
    always @(posedge clk) begin
        if (!resetn) read_mailbox<=1;
        else if (rd_en) read_mailbox<=rd_addr<16'h0100;
    end
    ps_axi_lite_regs bus(
        .clk(clk),.resetn(resetn),
        .s_axi_awaddr(s_axi_awaddr),.s_axi_awvalid(s_axi_awvalid),.s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata),.s_axi_wstrb(s_axi_wstrb),.s_axi_wvalid(s_axi_wvalid),.s_axi_wready(s_axi_wready),
        .s_axi_bresp(s_axi_bresp),.s_axi_bvalid(s_axi_bvalid),.s_axi_bready(s_axi_bready),
        .s_axi_araddr(s_axi_araddr),.s_axi_arvalid(s_axi_arvalid),.s_axi_arready(s_axi_arready),
        .s_axi_rdata(s_axi_rdata),.s_axi_rresp(s_axi_rresp),.s_axi_rvalid(s_axi_rvalid),.s_axi_rready(s_axi_rready),
        .wr_en(wr_en),.wr_addr(wr_addr),.wr_data(wr_data),.wr_strb(wr_strb),
        .wr_resp(write_mailbox ? mb_wr_resp : wave_wr_resp),
        .rd_en(rd_en),.rd_addr(rd_addr),.rd_data(read_mailbox ? mb_rd_data : wave_rd_data),
        .rd_resp(read_mailbox ? mb_rd_resp : wave_rd_resp));
    ps_mailbox #(.CLOCK_HZ(CLOCK_HZ),.TIMEOUT_MS(TIMEOUT_MS)) mailbox(
        .clk(clk),.resetn(resetn),.wr_en(wr_en && write_mailbox),.wr_addr(wr_addr[7:0]),
        .wr_data(wr_data),.wr_strb(wr_strb),.wr_resp(mb_wr_resp),
        .rd_en(rd_en && rd_addr<16'h0100),.rd_addr(rd_addr[7:0]),.rd_data(mb_rd_data),.rd_resp(mb_rd_resp),
        .request_valid(request_valid),.request_crc_error(request_crc_error),
        .request_is_read(request_is_read),.request_range(request_range),.registers(registers));
    ps_waveform waveform(
        .clk(clk),.resetn(resetn),.wr_en(wr_en && !write_mailbox),.wr_addr(wr_addr),
        .wr_data(wr_data),.wr_strb(wr_strb),.wr_resp(wave_wr_resp),
        .rd_en(rd_en && rd_addr>=16'h0100),.rd_addr(rd_addr),.rd_data(wave_rd_data),.rd_resp(wave_rd_resp));
    ps_modbus_rtu #(.CLOCK_HZ(CLOCK_HZ)) modbus(
        .clk(clk),.resetn(resetn),.rs485_rx(rs485_rx),.rs485_tx(rs485_tx),.rs485_de(rs485_de),
        .registers(registers),.request_valid(request_valid),.request_crc_error(request_crc_error),
        .request_is_read(request_is_read),.request_range(request_range));
endmodule
