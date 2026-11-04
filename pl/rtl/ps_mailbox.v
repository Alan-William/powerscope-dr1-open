`timescale 1ns/1ps
// ABI 1. All inputs, including request events, are in clk's domain.
module ps_mailbox #(
    parameter CLOCK_HZ = 75000000,
    parameter TIMEOUT_MS = 500
)(
    input wire clk, input wire resetn,
    input wire wr_en, input wire [7:0] wr_addr,
    input wire [31:0] wr_data, input wire [3:0] wr_strb,
    output reg [1:0] wr_resp,
    input wire rd_en, input wire [7:0] rd_addr,
    output reg [31:0] rd_data, output reg [1:0] rd_resp,
    input wire request_valid, input wire request_crc_error,
    input wire request_is_read, input wire [31:0] request_range,
    output wire [159:0] registers
);
    localparam TIMEOUT_CYCLES = (CLOCK_HZ / 1000) * TIMEOUT_MS;
    reg [31:0] pending_seq, pending_flags_label, pending_score;
    reg [31:0] pending_us, pending_error, commit_token, ack_token;
    reg [31:0] active_seq, active_us, active_error;
    reg [15:0] active_flags, active_label, active_score;
    reg [31:0] age;
    reg [31:0] valid_count, crc_count, last_request;
    reg [31:0] snap_valid, snap_crc, snap_last, snap_token;
    reg [1:0] snap_state;
    function [31:0] merge_bytes;
        input [31:0] old_value, value; input [3:0] strobe; integer i;
        begin
            merge_bytes = old_value;
            for (i=0;i<4;i=i+1) if (strobe[i]) merge_bytes[i*8 +: 8]=value[i*8 +: 8];
        end
    endfunction
    wire [31:0] new_token = merge_bytes(commit_token, wr_data, wr_strb);
    wire [31:0] next_token = ack_token==32'hffffffff ? 32'd1 : ack_token+1'b1;
    wire commit_fire = wr_en && wr_addr==8'h1c && wr_resp==0 && new_token!=ack_token;
    assign registers = {active_error[15:0],active_error[31:16],
                        active_us[15:0],active_us[31:16],active_seq[15:0],active_seq[31:16],
                        active_score,active_label,active_flags,16'd1};
    always @* begin
        wr_resp=2'b00;
        case (wr_addr)
            8'h08,8'h0c,8'h10,8'h14,8'h18: ;
            8'h1c: if (new_token!=ack_token && new_token!=next_token) wr_resp=2'b10;
            default: wr_resp=2'b10;
        endcase
    end
    always @(posedge clk) begin
        if (!resetn) begin
            pending_seq<=0; pending_flags_label<=32'hffff0000; pending_score<=0;
            pending_us<=0; pending_error<=0; commit_token<=0; ack_token<=0;
            active_seq<=0; active_flags<=0; active_label<=16'hffff;
            active_score<=0; active_us<=0; active_error<=0; age<=0;
            valid_count<=0; crc_count<=0; last_request<=0;
            snap_valid<=0; snap_crc<=0; snap_last<=0; snap_token<=0; snap_state<=0;
            rd_data<=0; rd_resp<=0;
        end else begin
            if (wr_en && wr_resp==0) begin
                case (wr_addr)
                    8'h08: pending_seq<=merge_bytes(pending_seq,wr_data,wr_strb);
                    8'h0c: pending_flags_label<=merge_bytes(pending_flags_label,wr_data,wr_strb);
                    8'h10: pending_score<=merge_bytes(pending_score,wr_data,wr_strb)&32'hffff;
                    8'h14: pending_us<=merge_bytes(pending_us,wr_data,wr_strb);
                    8'h18: pending_error<=merge_bytes(pending_error,wr_data,wr_strb);
                    default: ;
                endcase
            end
            if (commit_fire) begin
                active_seq<=pending_seq; active_flags<=pending_flags_label[15:0]&16'h7;
                active_label<=pending_flags_label[31:16]; active_score<=pending_score[15:0];
                active_us<=pending_us; active_error<=pending_error;
                commit_token<=new_token; ack_token<=new_token; age<=0;
            end else if (active_flags[0]) begin
                if (age >= TIMEOUT_CYCLES-1) begin
                    active_flags<=active_flags&16'h4; active_label<=16'hffff;
                    active_score<=0; active_us<=0; active_error<=5;
                end else age<=age+1'b1;
            end
            if (request_valid) begin
                valid_count<=valid_count+1'b1;
                if (request_is_read) last_request<=request_range;
            end
            if (request_crc_error) crc_count<=crc_count+1'b1;
            // Internal counters may change every cycle; expose ordered seqlock copies.
            case (snap_state)
                0: if (snap_valid!=valid_count || snap_crc!=crc_count || snap_last!=last_request) begin
                    snap_token<=snap_token+1'b1; snap_state<=1;
                end
                1: begin
                    snap_valid<=valid_count; snap_crc<=crc_count; snap_last<=last_request; snap_state<=2;
                end
                2: begin snap_token<=snap_token+1'b1; snap_state<=0; end
                default: snap_state<=0;
            endcase
            if (rd_en) begin
                rd_resp<=0;
                case (rd_addr)
                    8'h00: rd_data<=1;
                    8'h04: rd_data<=1;
                    8'h08: rd_data<=pending_seq;
                    8'h0c: rd_data<=pending_flags_label;
                    8'h10: rd_data<=pending_score;
                    8'h14: rd_data<=pending_us;
                    8'h18: rd_data<=pending_error;
                    8'h1c: rd_data<=commit_token;
                    8'h20: rd_data<=ack_token;
                    8'h24: rd_data<=snap_valid;
                    8'h28: rd_data<=snap_crc;
                    8'h2c: rd_data<=snap_last;
                    8'h30: rd_data<=snap_token;
                    default: begin rd_data<=0; rd_resp<=2'b10; end
                endcase
            end
        end
    end
endmodule
