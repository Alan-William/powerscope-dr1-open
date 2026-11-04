`timescale 1ns/1ps
// Insert after the vendor GP0 AXI-to-Lite converter. m0 retains the original
// matrix, m1 owns ONLY BASE/MASK. Address/data are buffered independently.
module ps_axi_lite_router #(
    parameter [32:0] BASE=33'h080400000,
    parameter [32:0] MASK=33'h1ffff0000
)(
    input wire clk, input wire resetn,
    input wire [32:0] s_awaddr, input wire [2:0] s_awprot,
    input wire s_awvalid, output wire s_awready,
    input wire [31:0] s_wdata, input wire [3:0] s_wstrb,
    input wire s_wvalid, output wire s_wready,
    output wire [1:0] s_bresp, output wire s_bvalid, input wire s_bready,
    input wire [32:0] s_araddr, input wire [2:0] s_arprot,
    input wire s_arvalid, output wire s_arready,
    output wire [31:0] s_rdata, output wire [1:0] s_rresp,
    output wire s_rvalid, input wire s_rready,
    output wire [65:0] m_awaddr, output wire [5:0] m_awprot,
    output wire [1:0] m_awvalid, input wire [1:0] m_awready,
    output wire [63:0] m_wdata, output wire [7:0] m_wstrb,
    output wire [1:0] m_wvalid, input wire [1:0] m_wready,
    input wire [3:0] m_bresp, input wire [1:0] m_bvalid, output wire [1:0] m_bready,
    output wire [65:0] m_araddr, output wire [5:0] m_arprot,
    output wire [1:0] m_arvalid, input wire [1:0] m_arready,
    input wire [63:0] m_rdata, input wire [3:0] m_rresp,
    input wire [1:0] m_rvalid, output wire [1:0] m_rready
);
    reg have_aw,have_w,write_active,send_aw,send_w,write_select;
    reg read_active,send_ar,read_select;
    reg [32:0] awaddr,araddr;
    reg [2:0] awprot,arprot;
    reg [31:0] wdata;
    reg [3:0] wstrb;
    wire write_response=write_active && !send_aw && !send_w;
    wire read_response=read_active && !send_ar;
    assign s_awready=resetn && !have_aw && !write_active;
    assign s_wready=resetn && !have_w && !write_active;
    assign s_arready=resetn && !read_active;
    assign m_awaddr={2{awaddr}}; assign m_awprot={2{awprot}};
    assign m_wdata={2{wdata}}; assign m_wstrb={2{wstrb}};
    assign m_araddr={2{araddr}}; assign m_arprot={2{arprot}};
    assign m_awvalid=send_aw ? (write_select ? 2'b10 : 2'b01) : 0;
    assign m_wvalid=send_w ? (write_select ? 2'b10 : 2'b01) : 0;
    assign m_arvalid=send_ar ? (read_select ? 2'b10 : 2'b01) : 0;
    assign s_bvalid=write_response && m_bvalid[write_select];
    assign s_bresp=m_bresp[write_select*2 +: 2];
    assign m_bready=write_response && s_bready ? (write_select ? 2'b10 : 2'b01) : 0;
    assign s_rvalid=read_response && m_rvalid[read_select];
    assign s_rdata=m_rdata[read_select*32 +: 32];
    assign s_rresp=m_rresp[read_select*2 +: 2];
    assign m_rready=read_response && s_rready ? (read_select ? 2'b10 : 2'b01) : 0;
    always @(posedge clk) begin
        if (!resetn) begin
            have_aw<=0; have_w<=0; write_active<=0; send_aw<=0; send_w<=0; write_select<=0;
            read_active<=0; send_ar<=0; read_select<=0;
            awaddr<=0; araddr<=0; awprot<=0; arprot<=0; wdata<=0; wstrb<=0;
        end else begin
            if(s_awvalid && s_awready) begin awaddr<=s_awaddr; awprot<=s_awprot; have_aw<=1; end
            if(s_wvalid && s_wready) begin wdata<=s_wdata; wstrb<=s_wstrb; have_w<=1; end
            if(have_aw && have_w && !write_active) begin
                write_select<=((awaddr&MASK)==BASE); write_active<=1; send_aw<=1; send_w<=1;
            end
            if(send_aw && m_awready[write_select]) send_aw<=0;
            if(send_w && m_wready[write_select]) send_w<=0;
            if(s_bvalid && s_bready) begin write_active<=0; have_aw<=0; have_w<=0; end
            if(s_arvalid && s_arready) begin
                araddr<=s_araddr; arprot<=s_arprot; read_select<=((s_araddr&MASK)==BASE);
                read_active<=1; send_ar<=1;
            end
            if(send_ar && m_arready[read_select]) send_ar<=0;
            if(s_rvalid && s_rready) read_active<=0;
        end
    end
endmodule
