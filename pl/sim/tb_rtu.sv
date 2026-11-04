`timescale 1ns/1ps
module tb_rtu;
    localparam CLOCK_HZ=75000000, BIT_CYCLES=651, T15=56250, T35=131250;
    reg clk=0; always #(500000000.0/CLOCK_HZ) clk=~clk;
    reg resetn=0,rx=1;
    wire tx,de;
    reg [15:0] awaddr=0,araddr=0;
    reg [31:0] wdata=0;
    reg [3:0] wstrb=15;
    reg awvalid=0,wvalid=0,bready=0,arvalid=0,rready=0;
    wire awready,wready,bvalid,arready,rvalid;
    wire [1:0] bresp,rresp;
    wire [31:0] rdata;
    powerscope_peripheral dut(
        .clk(clk),.resetn(resetn),
        .s_axi_awaddr(awaddr),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
        .s_axi_wdata(wdata),.s_axi_wstrb(wstrb),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
        .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(bready),
        .s_axi_araddr(araddr),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
        .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(rready),
        .rs485_rx(rx),.rs485_tx(tx),.rs485_de(de));
    task automatic write_reg(input [15:0] a,input [31:0] value);
        begin
            @(negedge clk); awaddr=a; awvalid=1;
            @(posedge clk); while(!awready) @(posedge clk);
            @(negedge clk); awvalid=0;
            repeat(2) @(negedge clk); wdata=value; wvalid=1;
            @(posedge clk); while(!wready) @(posedge clk);
            @(negedge clk); wvalid=0;
            while(!bvalid) @(negedge clk);
            if (bresp!==0) $fatal(1,"AXI write");
            bready=1; @(negedge clk); bready=0;
        end
    endtask
    task automatic send_byte(input [7:0] value,input integer bad_parity,input integer bad_stop);
        integer k;
        begin
            @(negedge clk); rx=0; repeat(BIT_CYCLES) @(negedge clk);
            for(k=0;k<8;k=k+1) begin rx=value[k]; repeat(BIT_CYCLES) @(negedge clk); end
            rx=(^value)^bad_parity; repeat(BIT_CYCLES) @(negedge clk);
            rx=!bad_stop; repeat(BIT_CYCLES) @(negedge clk); rx=1;
        end
    endtask
    reg [7:0] captured[0:255];
    integer captured_count=0,reply_count=0;
    integer cycles=0,de_rose=0,last_stop_end=0;
    always @(posedge clk) cycles=cycles+1;
    always @(posedge de) de_rose=cycles;
    always @(negedge de) if(resetn && cycles-last_stop_end<BIT_CYCLES)
        $fatal(1,"DE hold time after full stop");
    reg [7:0] decoded;
    integer k;
    // Independent serial decoder checks even parity, stop bit, and DE through the full stop.
    initial forever begin
        @(negedge tx);
        if(resetn) begin
            if(!de) $fatal(1,"TX without DE");
            if(captured_count==0 && cycles-de_rose<BIT_CYCLES) $fatal(1,"DE setup time");
            repeat(BIT_CYCLES/2) @(posedge clk);
            if(tx!==0) $fatal(1,"Start bit");
            for(k=0;k<8;k=k+1) begin repeat(BIT_CYCLES) @(posedge clk); decoded[k]=tx; end
            repeat(BIT_CYCLES) @(posedge clk); if(tx!==( ^decoded)) $fatal(1,"TX parity");
            repeat(BIT_CYCLES) @(posedge clk); if(tx!==1) $fatal(1,"TX stop");
            repeat(BIT_CYCLES-BIT_CYCLES/2-1) @(posedge clk);
            if(!de) $fatal(1,"DE dropped before final stop completion");
            last_stop_end=cycles;
            captured[captured_count]=decoded; captured_count=captured_count+1;
        end
    end
    task automatic check_reply(input [199:0] expected,input integer count);
        integer j;
        begin
            if(captured_count!=count) $fatal(1,"RTU length got %d expected %d",captured_count,count);
            for(j=0;j<count;j=j+1) if(captured[j]!==expected[j*8 +: 8])
                $fatal(1,"RTU byte %d got %h expected %h",j,captured[j],expected[j*8 +: 8]);
            reply_count=reply_count+1;
        end
    endtask
    task automatic wait_reply(input integer count);
        integer j; reg seen;
        begin : wait_block
            seen=0;
            for(j=0;j<750000;j=j+1) begin
                @(negedge clk);
                if(de) seen=1;
                if(seen && !de) disable wait_block;
            end
            if(count!=0) $fatal(1,"No reply");
        end
    endtask
    task automatic transaction(input [2047:0] req,input integer n,input [199:0] expected,input integer count);
        integer j;
        begin
            captured_count=0;
            for(j=0;j<n;j=j+1) send_byte(req[j*8 +: 8],0,0);
            wait_reply(count); check_reply(expected,count);
            repeat(T35+30) @(negedge clk);
        end
    endtask
    integer token=0;
    initial begin
        repeat(5) @(negedge clk); resetn=1;
        repeat(T35+30) @(negedge clk);
        `include "rtu_vectors.svh"
        // An inter-character gap > t1.5 but < t3.5 invalidates the whole RTU frame.
        captured_count=0;
        send_byte(1,0,0); send_byte(3,0,0); repeat(T15+100) @(negedge clk);
        send_byte(0,0,0); send_byte(0,0,0); send_byte(0,0,0); send_byte(10,0,0); send_byte('hc5,0,0); send_byte('hcd,0,0);
        wait_reply(0); check_reply(0,0); repeat(T35+30) @(negedge clk);
        // Parity and framing errors are discarded, without counting them as CRC failures.
        captured_count=0;
        send_byte(1,1,0); send_byte(3,0,0); send_byte(0,0,0); send_byte(0,0,0);
        send_byte(0,0,0); send_byte(10,0,0); send_byte('hc5,0,0); send_byte('hcd,0,0);
        wait_reply(0); check_reply(0,0); repeat(T35+30) @(negedge clk);
        captured_count=0;
        send_byte(1,0,1); send_byte(3,0,0); send_byte(0,0,0); send_byte(0,0,0);
        send_byte(0,0,0); send_byte(10,0,0); send_byte('hc5,0,0); send_byte('hcd,0,0);
        wait_reply(0); check_reply(0,0); repeat(T35+30) @(negedge clk);
        `include "rtu_atomic.svh"
        $display("PASS RTU: %0d cases, actual 75 MHz divisors, bit/parity/stop/DE, atomic response, t1.5 and error recovery",reply_count);
        $finish;
    end
    initial begin repeat(100000000) @(posedge clk); $fatal(1,"Global timeout"); end
endmodule
