`timescale 1ns/1ps
// One outstanding write and read, independently. Register reads are synchronous.
module ps_axi_lite_regs #(
    parameter ADDR_WIDTH = 16
)(
    input wire clk, input wire resetn,
    input wire [ADDR_WIDTH-1:0] s_axi_awaddr,
    input wire s_axi_awvalid, output wire s_axi_awready,
    input wire [31:0] s_axi_wdata, input wire [3:0] s_axi_wstrb,
    input wire s_axi_wvalid, output wire s_axi_wready,
    output reg [1:0] s_axi_bresp, output reg s_axi_bvalid,
    input wire s_axi_bready,
    input wire [ADDR_WIDTH-1:0] s_axi_araddr,
    input wire s_axi_arvalid, output wire s_axi_arready,
    output reg [31:0] s_axi_rdata, output reg [1:0] s_axi_rresp,
    output reg s_axi_rvalid, input wire s_axi_rready,
    output wire wr_en, output reg [ADDR_WIDTH-1:0] wr_addr,
    output reg [31:0] wr_data, output reg [3:0] wr_strb,
    input wire [1:0] wr_resp,
    output reg rd_en, output reg [ADDR_WIDTH-1:0] rd_addr,
    input wire [31:0] rd_data, input wire [1:0] rd_resp
);
    reg aw_pending, w_pending, rd_capture;
    assign s_axi_awready = resetn && !aw_pending && !s_axi_bvalid;
    assign s_axi_wready = resetn && !w_pending && !s_axi_bvalid;
    assign s_axi_arready = resetn && !rd_en && !rd_capture && !s_axi_rvalid;
    assign wr_en = aw_pending && w_pending && !s_axi_bvalid;
    always @(posedge clk) begin
        if (!resetn) begin
            aw_pending <= 0; w_pending <= 0; rd_en <= 0; rd_capture <= 0;
            s_axi_bvalid <= 0; s_axi_rvalid <= 0;
            s_axi_bresp <= 0; s_axi_rresp <= 0; s_axi_rdata <= 0;
            wr_addr <= 0; wr_data <= 0; wr_strb <= 0; rd_addr <= 0;
        end else begin
            if (s_axi_awvalid && s_axi_awready) begin
                wr_addr <= s_axi_awaddr; aw_pending <= 1;
            end
            if (s_axi_wvalid && s_axi_wready) begin
                wr_data <= s_axi_wdata; wr_strb <= s_axi_wstrb; w_pending <= 1;
            end
            if (wr_en) begin
                aw_pending <= 0; w_pending <= 0;
                s_axi_bvalid <= 1; s_axi_bresp <= wr_resp;
            end else if (s_axi_bvalid && s_axi_bready) s_axi_bvalid <= 0;
            rd_en <= 0;
            rd_capture <= rd_en;
            if (s_axi_arvalid && s_axi_arready) begin
                rd_addr <= s_axi_araddr; rd_en <= 1;
            end
            if (rd_capture) begin
                s_axi_rvalid <= 1; s_axi_rdata <= rd_data; s_axi_rresp <= rd_resp;
            end else if (s_axi_rvalid && s_axi_rready) s_axi_rvalid <= 0;
        end
    end
endmodule
