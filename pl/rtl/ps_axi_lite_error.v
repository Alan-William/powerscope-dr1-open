`timescale 1ns/1ps
// Terminal target for removed/unmapped GP0 peripherals. Every transaction
// completes with DECERR; AW and W may arrive in either order.
module ps_axi_lite_error(
    input wire clk, input wire resetn,
    input wire s_awvalid, output wire s_awready,
    input wire s_wvalid, output wire s_wready,
    output wire [1:0] s_bresp, output reg s_bvalid, input wire s_bready,
    input wire s_arvalid, output wire s_arready,
    output wire [31:0] s_rdata, output wire [1:0] s_rresp,
    output reg s_rvalid, input wire s_rready
);
    reg have_aw, have_w;
    assign s_awready = resetn && !have_aw && !s_bvalid;
    assign s_wready = resetn && !have_w && !s_bvalid;
    assign s_arready = resetn && !s_rvalid;
    assign s_bresp = 2'b11;
    assign s_rresp = 2'b11;
    assign s_rdata = 32'b0;
    always @(posedge clk) begin
        if (!resetn) begin
            have_aw <= 0;
            have_w <= 0;
            s_bvalid <= 0;
            s_rvalid <= 0;
        end else begin
            if (s_awvalid && s_awready) have_aw <= 1;
            if (s_wvalid && s_wready) have_w <= 1;
            if (have_aw && have_w) s_bvalid <= 1;
            if (s_bvalid && s_bready) begin
                s_bvalid <= 0;
                have_aw <= 0;
                have_w <= 0;
            end
            if (s_arvalid && s_arready) s_rvalid <= 1;
            if (s_rvalid && s_rready) s_rvalid <= 0;
        end
    end
endmodule
