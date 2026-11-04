`timescale 1ns/1ps
// 8E1 only. tx_done is AFTER the complete stop bit, never at its midpoint.
module ps_uart_tx #(parameter CLOCK_HZ=75000000, parameter BAUD=115200)(
    input wire clk, input wire resetn,
    input wire [7:0] data, input wire valid, output wire ready,
    output reg tx, output reg done
);
    localparam BIT_CYCLES=(CLOCK_HZ+BAUD/2)/BAUD;
    reg [31:0] ticks;
    reg [10:0] bits;
    reg [3:0] bit_index;
    reg busy;
    assign ready=resetn && !busy;
    always @(posedge clk) begin
        if (!resetn) begin tx<=1; done<=0; busy<=0; ticks<=0; bits<=0; bit_index<=0; end
        else begin
            done<=0;
            if (valid && ready) begin
                bits<={1'b1,^data,data,1'b0}; tx<=0;
                busy<=1; ticks<=BIT_CYCLES-1; bit_index<=0;
            end else if (busy) begin
                if (ticks!=0) ticks<=ticks-1'b1;
                else if (bit_index==10) begin tx<=1; busy<=0; done<=1; end
                else begin
                    bit_index<=bit_index+1'b1; tx<=bits[bit_index+1'b1]; ticks<=BIT_CYCLES-1;
                end
            end
        end
    end
endmodule

module ps_uart_rx #(parameter CLOCK_HZ=75000000, parameter BAUD=115200)(
    input wire clk, input wire resetn, input wire enable, input wire rx,
    output reg [7:0] data, output reg valid, output reg error,
    output reg start, output wire busy
);
    localparam BIT_CYCLES=(CLOCK_HZ+BAUD/2)/BAUD;
    localparam HALF_CYCLES=BIT_CYCLES/2;
    localparam IDLE=0, START_BIT=1, DATA_BITS=2, PARITY=3, STOP_BIT=4, END_BIT=5, WAIT_HIGH=6;
    (* ASYNC_REG = "TRUE" *) reg rx_meta, rx_sync;
    reg [2:0] state;
    reg [31:0] ticks;
    reg [2:0] bit_index;
    reg [7:0] shift;
    reg bad;
    assign busy=state!=IDLE;
    always @(posedge clk) begin
        if (!resetn) begin rx_meta<=1; rx_sync<=1; end
        else begin rx_meta<=rx; rx_sync<=rx_meta; end
    end
    always @(posedge clk) begin
        if (!resetn) begin
            state<=IDLE; ticks<=0; bit_index<=0; shift<=0; bad<=0;
            data<=0; valid<=0; error<=0; start<=0;
        end else begin
            valid<=0; error<=0; start<=0;
            if (!enable) state<=IDLE;
            else if (state==IDLE) begin
                if (!rx_sync) begin state<=START_BIT; ticks<=HALF_CYCLES-1; start<=1; bad<=0; end
            end else if (state==WAIT_HIGH) begin
                if (rx_sync) state<=IDLE;
            end else if (ticks!=0) ticks<=ticks-1'b1;
            else case (state)
                START_BIT: begin
                    if (rx_sync) begin state<=IDLE; error<=1; end
                    else begin state<=DATA_BITS; ticks<=BIT_CYCLES-1; bit_index<=0; end
                end
                DATA_BITS: begin
                    shift[bit_index]<=rx_sync; ticks<=BIT_CYCLES-1;
                    if (bit_index==7) state<=PARITY; else bit_index<=bit_index+1'b1;
                end
                PARITY: begin bad<=(rx_sync != ^shift); state<=STOP_BIT; ticks<=BIT_CYCLES-1; end
                STOP_BIT: begin bad<=bad || !rx_sync; state<=END_BIT; ticks<=BIT_CYCLES-HALF_CYCLES-1; end
                END_BIT: begin
                    data<=shift; valid<=!bad; error<=bad;
                    state<=bad && !rx_sync ? WAIT_HIGH : IDLE;
                end
                default: state<=IDLE;
            endcase
        end
    end
endmodule
