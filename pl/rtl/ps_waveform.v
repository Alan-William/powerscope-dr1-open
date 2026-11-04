`timescale 1ns/1ps
// One frame in flight. Buffer writes are full-word, sequential, and gated by ownership.
// The PS validates PSW1/CRC32; PL returns all bytes unchanged, including bad CRCs.
module ps_waveform(
    input wire clk, input wire resetn,
    input wire wr_en, input wire [15:0] wr_addr,
    input wire [31:0] wr_data, input wire [3:0] wr_strb,
    output reg [1:0] wr_resp,
    input wire rd_en, input wire [15:0] rd_addr,
    output reg [31:0] rd_data, output reg [1:0] rd_resp
);
    reg [31:0] memory [0:757];
    reg occupied;
    reg [9:0] words_written;
    reg [31:0] length, commit_token, ack_token, consume_token, accepted_count, error_count;
    wire [31:0] next_token = ack_token==32'hffffffff ? 32'd1 : ack_token+1'b1;
    wire buffer_write = wr_addr>=16'h2000 && wr_addr<16'h2bd8 && wr_addr[1:0]==0;
    wire buffer_read = rd_addr>=16'h2000 && rd_addr<16'h2bd8 && rd_addr[1:0]==0;
    wire [9:0] wr_index = wr_addr[11:2];
    always @* begin
        wr_resp=2'b10;
        if (wr_strb==4'hf) begin
            if (buffer_write && !occupied && wr_index==words_written) wr_resp=0;
            case (wr_addr)
                16'h1008: if (!occupied) wr_resp=0;
                16'h100c: if (!occupied && length==3032 && words_written==758 && wr_data==next_token) wr_resp=0;
                16'h1014: if (occupied && wr_data==ack_token) wr_resp=0;
                16'h1020: if (!occupied && wr_data==1) wr_resp=0; // discard incomplete input
                default: ;
            endcase
        end
    end
    // Dedicated RAM processes: no reset on RAM data, synchronous read, one write port.
    reg [31:0] ram_read_data;
    reg ram_read_selected;
    always @(posedge clk) begin
        if (wr_en && buffer_write && wr_resp==0) memory[wr_index]<=wr_data;
        if (rd_en && buffer_read) ram_read_data<=memory[rd_addr[11:2]];
    end
    reg [31:0] reg_read_data;
    always @* rd_data = ram_read_selected ? ram_read_data : reg_read_data;
    always @(posedge clk) begin
        if (!resetn) begin
            occupied<=0; words_written<=0; length<=3032; commit_token<=0; ack_token<=0;
            consume_token<=0; accepted_count<=0; error_count<=0;
            reg_read_data<=0; ram_read_selected<=0; rd_resp<=0;
        end else begin
            if (wr_en) begin
                if (wr_resp!=0) error_count<=error_count+1'b1;
                else if (buffer_write) words_written<=words_written+1'b1;
                else case (wr_addr)
                    16'h1008: length<=wr_data;
                    16'h100c: begin
                        occupied<=1; commit_token<=wr_data; ack_token<=wr_data;
                        accepted_count<=accepted_count+1'b1;
                    end
                    16'h1014: begin occupied<=0; consume_token<=wr_data; words_written<=0; end
                    16'h1020: begin words_written<=0; length<=3032; end
                    default: ;
                endcase
            end
            if (rd_en) begin
                ram_read_selected<=0; rd_resp<=0; reg_read_data<=0;
                if (buffer_read) begin
                    if (occupied) ram_read_selected<=1;
                    else rd_resp<=2'b10;
                end else case (rd_addr)
                    16'h1000: reg_read_data<=1;
                    16'h1004: reg_read_data<=occupied ? 2 : 1;
                    16'h1008: reg_read_data<=length;
                    16'h100c: reg_read_data<=commit_token;
                    16'h1010: reg_read_data<=ack_token;
                    16'h1014: reg_read_data<=consume_token;
                    16'h1018: reg_read_data<=accepted_count;
                    16'h101c: reg_read_data<=error_count;
                    16'h1024: reg_read_data<={22'b0,words_written};
                    default: rd_resp<=2'b10;
                endcase
            end
        end
    end
endmodule
