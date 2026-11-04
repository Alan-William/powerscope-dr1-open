`timescale 1ns/1ps
// Address 1/function 03, RTU 115200 8E1. Input and result snapshots share clk.
module ps_modbus_rtu #(
    parameter CLOCK_HZ=75000000, parameter BAUD=115200, parameter SLAVE_ADDRESS=1
)(
    input wire clk, input wire resetn, input wire rs485_rx,
    output wire rs485_tx, output wire rs485_de,
    input wire [159:0] registers,
    output reg request_valid, output reg request_crc_error,
    output reg request_is_read, output reg [31:0] request_range
);
    localparam T15=(CLOCK_HZ/1000000)*750;
    localparam T35=(CLOCK_HZ/1000000)*1750;
    localparam BIT_CYCLES=(CLOCK_HZ+BAUD/2)/BAUD;
    localparam RECEIVE=0, BUILD=1, CRC_LOW=2, CRC_HIGH=3, DE_SETUP=4, SEND=5, DRAIN=6, DE_HOLD=7;
    reg [3:0] state;
    wire [7:0] rx_data;
    wire rx_valid, rx_error, rx_start, rx_busy, tx_ready, tx_done;
    reg [7:0] first [0:5];
    reg [8:0] rx_length;
    reg [15:0] rx_crc;
    reg bad_frame;
    reg [31:0] gap_ticks, turn_ticks;
    reg [159:0] snapshot;
    reg [7:0] response [0:24];
    reg [4:0] response_length, index;
    reg [15:0] tx_crc;
    reg [15:0] start_register, register_count;
    reg [7:0] response_function, exception_code;
    reg [7:0] build_byte;
    reg [15:0] selected_register;
    wire [15:0] req_start={first[2],first[3]};
    wire [15:0] req_count={first[4],first[5]};
    wire [16:0] req_end={1'b0,req_start}+{1'b0,req_count};
    assign rs485_de=(state>=DE_SETUP);
    ps_uart_rx #(.CLOCK_HZ(CLOCK_HZ),.BAUD(BAUD)) receiver(
        .clk(clk),.resetn(resetn),.enable(!rs485_de),.rx(rs485_rx),
        .data(rx_data),.valid(rx_valid),.error(rx_error),.start(rx_start),.busy(rx_busy));
    ps_uart_tx #(.CLOCK_HZ(CLOCK_HZ),.BAUD(BAUD)) transmitter(
        .clk(clk),.resetn(resetn),.data(response[index]),.valid(state==SEND),
        .ready(tx_ready),.tx(rs485_tx),.done(tx_done));
    function [15:0] crc_byte;
        input [15:0] crc; input [7:0] value; reg [15:0] c; integer i;
        begin
            c=crc^{8'b0,value};
            for (i=0;i<8;i=i+1) c=c[0] ? (c>>1)^16'ha001 : c>>1;
            crc_byte=c;
        end
    endfunction
    always @* begin
        build_byte=0; selected_register=0;
        if (index==0) build_byte=SLAVE_ADDRESS;
        else if (index==1) build_byte=response_function;
        else if (index==2) build_byte=exception_code!=0 ? exception_code : register_count[6:0]*2;
        else begin
            selected_register=snapshot[(start_register+((index-3)>>1))*16 +: 16];
            build_byte=index[0] ? selected_register[15:8] : selected_register[7:0];
        end
    end
    always @(posedge clk) begin
        if (!resetn) begin
            state<=RECEIVE; rx_length<=0; rx_crc<=16'hffff; bad_frame<=0;
            gap_ticks<=0; turn_ticks<=0; snapshot<=0; response_length<=0; index<=0;
            tx_crc<=16'hffff; start_register<=0; register_count<=0;
            response_function<=0; exception_code<=0;
            request_valid<=0; request_crc_error<=0; request_is_read<=0; request_range<=0;
        end else begin
            request_valid<=0; request_crc_error<=0; request_is_read<=0;
            case (state)
                RECEIVE: begin
                    if (!rx_busy && !rx_start && gap_ticks<T35) gap_ticks<=gap_ticks+1'b1;
                    if (rx_start && ((rx_length!=0 && gap_ticks>=T15) || (rx_length==0 && gap_ticks<T35))) bad_frame<=1;
                    if (rx_valid || rx_error) begin
                        gap_ticks<=0;
                        if (rx_error) bad_frame<=1;
                        if (rx_length<257) rx_length<=rx_length+1'b1;
                        if (rx_length<6) first[rx_length]<=rx_data;
                        rx_crc<=crc_byte(rx_crc,rx_data);
                    end else if (!rx_busy && !rx_start && gap_ticks>=T35) begin
                        // Ignore other addresses before counting CRC failures; framing errors are not CRC errors.
                        if (rx_length>=4 && rx_length<=256 && !bad_frame && first[0]==SLAVE_ADDRESS) begin
                            if (rx_crc!=0) request_crc_error<=1;
                            else if (first[1]!=3 || rx_length==8) begin
                                request_valid<=1;
                                request_is_read<=(first[1]==3);
                                request_range<={req_start,req_count};
                                snapshot<=registers; index<=0; tx_crc<=16'hffff;
                                start_register<=req_start; register_count<=req_count;
                                state<=BUILD;
                                if (first[1]!=3) begin
                                    response_function<=first[1]|8'h80; exception_code<=1; response_length<=5;
                                end else if (req_count==0 || req_count>125) begin
                                    response_function<=8'h83; exception_code<=3; response_length<=5;
                                end else if (req_end>10) begin
                                    response_function<=8'h83; exception_code<=2; response_length<=5;
                                end else begin
                                    response_function<=3; exception_code<=0; response_length<=5+req_count*2;
                                end
                            end
                        end
                        rx_length<=0; rx_crc<=16'hffff; bad_frame<=0;
                    end
                end
                BUILD: begin
                    response[index]<=build_byte; tx_crc<=crc_byte(tx_crc,build_byte);
                    index<=index+1'b1;
                    if (index==response_length-3) state<=CRC_LOW;
                end
                CRC_LOW: begin response[index]<=tx_crc[7:0]; index<=index+1'b1; state<=CRC_HIGH; end
                CRC_HIGH: begin response[index]<=tx_crc[15:8]; index<=0; turn_ticks<=0; state<=DE_SETUP; end
                DE_SETUP: begin
                    if (turn_ticks==BIT_CYCLES-1) begin state<=SEND; turn_ticks<=0; end
                    else turn_ticks<=turn_ticks+1'b1;
                end
                SEND: if (tx_ready) begin
                    if (index==response_length-1) state<=DRAIN;
                    else index<=index+1'b1;
                end
                DRAIN: if (tx_done) begin state<=DE_HOLD; turn_ticks<=0; end
                DE_HOLD: begin
                    if (turn_ticks==BIT_CYCLES-1) begin state<=RECEIVE; gap_ticks<=0; end
                    else turn_ticks<=turn_ticks+1'b1;
                end
                default: state<=RECEIVE;
            endcase
        end
    end
endmodule
