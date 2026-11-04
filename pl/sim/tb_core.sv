`timescale 1ns/1ps
module tb_core;
    reg clk=0; always #5 clk=~clk;
    reg resetn=0;
    reg [15:0] awaddr=0,araddr=0;
    reg [31:0] wdata=0;
    reg [3:0] wstrb=0;
    reg awvalid=0,wvalid=0,bready=0,arvalid=0,rready=0;
    wire awready,wready,bvalid,arready,rvalid;
    wire [1:0] bresp,rresp;
    wire [31:0] rdata;
    wire tx,de;
    powerscope_peripheral #(.CLOCK_HZ(1000000),.TIMEOUT_MS(5)) dut(
        .clk(clk),.resetn(resetn),
        .s_axi_awaddr(awaddr),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
        .s_axi_wdata(wdata),.s_axi_wstrb(wstrb),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
        .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(bready),
        .s_axi_araddr(araddr),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
        .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(rready),
        .rs485_rx(1'b1),.rs485_tx(tx),.rs485_de(de));
    task automatic write_reg(input [15:0] a,input [31:0] d,input [3:0] strb,
                             input integer order,input integer stall,input [1:0] expected);
        begin
            fork
                begin
                    if (order==2) repeat(4) @(negedge clk);
                    @(negedge clk); awaddr=a; awvalid=1;
                    @(posedge clk); while(!awready) @(posedge clk);
                    @(negedge clk); awvalid=0;
                end
                begin
                    if (order==1) repeat(4) @(negedge clk);
                    @(negedge clk); wdata=d; wstrb=strb; wvalid=1;
                    @(posedge clk); while(!wready) @(posedge clk);
                    @(negedge clk); wvalid=0;
                end
            join
            while(!bvalid) @(negedge clk);
            if (bresp!==expected) $fatal(1,"BRESP %h: got %h expected %h",a,bresp,expected);
            repeat(stall) begin
                @(negedge clk);
                if (!bvalid || bresp!==expected || awready || wready) $fatal(1,"Write backpressure");
            end
            bready=1; @(negedge clk); bready=0;
        end
    endtask
    task automatic read_reg(input [15:0] a,output [31:0] value,input integer stall,input [1:0] expected);
        begin
            @(negedge clk); araddr=a; arvalid=1;
            @(posedge clk); while(!arready) @(posedge clk);
            @(negedge clk); arvalid=0;
            while(!rvalid) @(negedge clk);
            value=rdata;
            if (rresp!==expected) $fatal(1,"RRESP %h",a);
            repeat(stall) begin
                @(negedge clk);
                if (!rvalid || rdata!==value || rresp!==expected || arready) $fatal(1,"Read backpressure");
            end
            rready=1; @(negedge clk); rready=0;
        end
    endtask
    task automatic expect_reg(input [15:0] a,input [31:0] expected);
        reg [31:0] value;
        begin
            read_reg(a,value,3,0);
            if (value!==expected) $fatal(1,"Register %h got %h expected %h",a,value,expected);
        end
    endtask
    reg [31:0] value, word_value;
    integer i,j,fd,rc,output_fd;
    reg [31:0] token;
    reg [95:0] previous_snapshot;
    reg [31:0] previous_snap_token;
    reg history_valid=0;
    // Check the seqlock's ordering at every edge, including consecutive events.
    always @(posedge clk) begin
        #1;
        if(!resetn) history_valid=0;
        else begin
            if(history_valid && {dut.mailbox.snap_valid,dut.mailbox.snap_crc,dut.mailbox.snap_last}!==previous_snapshot
               && !previous_snap_token[0]) $fatal(1,"Snapshot fields changed without prior odd token");
            previous_snapshot={dut.mailbox.snap_valid,dut.mailbox.snap_crc,dut.mailbox.snap_last};
            previous_snap_token=dut.mailbox.snap_token;
            history_valid=1;
        end
    end
    initial begin
        repeat(5) @(negedge clk); resetn=1;
        expect_reg(0,1); expect_reg(4,1); expect_reg('h1c,0); expect_reg('h20,0);
        if (dut.registers[47:32]!==16'hffff || de!==0 || tx!==1) $fatal(1,"Reset values");
        write_reg('h08,'h11223344,15,1,7,0);
        write_reg('h08,'haa55bb66,5,2,7,0); expect_reg('h08,'h11553366);
        write_reg('h08,0,0,0,0,0); expect_reg('h08,'h11553366);
        write_reg('h10,'hffffabcd,15,0,0,0); expect_reg('h10,'habcd);
        write_reg('h01,0,15,0,0,2); write_reg('h20,0,15,0,0,2);
        read_reg('h34,value,0,2); read_reg('h0100,value,0,2);
        write_reg('h0c,'h00070007,15,0,0,0);
        write_reg('h14,1234,15,0,0,0); write_reg('h18,0,15,0,0,0);
        if (dut.registers[47:32]!==16'hffff) $fatal(1,"Pending leaked");
        write_reg('h1c,1,15,2,0,0); expect_reg('h20,1);
        if (dut.registers[47:32]!==7 || dut.registers[63:48]!==16'habcd) $fatal(1,"Atomic commit");
        write_reg('h0c,'h00080007,15,0,0,0);
        if (dut.registers[47:32]!==7) $fatal(1,"Pending overwrote active");
        write_reg('h1c,5,15,0,0,2); expect_reg('h20,1);
        // Same-token write does not refresh the liveness timer.
        repeat(2500) @(negedge clk); write_reg('h1c,1,15,0,0,0);
        repeat(2600) @(negedge clk);
        if (dut.registers[31:16]!==4 || dut.registers[47:32]!==16'hffff || dut.registers[159:128]!==32'h00050000)
            $fatal(1,"Watchdog");
        write_reg('h1c,2,15,0,0,0);
        if (dut.registers[47:32]!==8) $fatal(1,"Recovery");
        // Seed terminal counter values to exercise uint32 wrap without 2^32 transactions.
        @(negedge clk); dut.mailbox.ack_token=32'hffffffff; dut.mailbox.commit_token=32'hffffffff;
        write_reg('h1c,1,15,0,0,0); expect_reg('h20,1);
        @(negedge clk); force dut.mailbox.request_valid=1; force dut.mailbox.request_is_read=1;
        force dut.mailbox.request_range=32'h00090001; force dut.mailbox.request_crc_error=1;
        repeat(9) @(negedge clk);
        release dut.mailbox.request_valid; release dut.mailbox.request_is_read;
        release dut.mailbox.request_range; release dut.mailbox.request_crc_error;
        repeat(12) @(negedge clk);
        expect_reg('h24,9); expect_reg('h28,9); expect_reg('h2c,'h00090001);
        read_reg('h30,value,0,0); if (value[0]) $fatal(1,"Snapshot odd");
        @(negedge clk); dut.mailbox.valid_count=32'hffffffff; dut.mailbox.crc_count=32'hffffffff;
        dut.mailbox.snap_token=32'hfffffffe;
        force dut.mailbox.request_valid=1; force dut.mailbox.request_crc_error=1;
        @(negedge clk); release dut.mailbox.request_valid; release dut.mailbox.request_crc_error;
        repeat(12) @(negedge clk); expect_reg('h24,0); expect_reg('h28,0);
        read_reg('h30,value,0,0); if (value[0]) $fatal(1,"Snapshot wrap");
        // Reset discards an AW that has no corresponding W.
        @(negedge clk); awaddr='h08; awvalid=1;
        @(negedge clk); awvalid=0; resetn=0;
        repeat(3) @(negedge clk); resetn=1;
        expect_reg('h08,0); expect_reg('h20,0);
        fork
            write_reg('h08,77,15,2,4,0);
            expect_reg(0,1);
        join
        expect_reg('h08,77);
        // Waveform rejects partial, out-of-order and truncated input, then recovers.
        read_reg('h2000,value,0,2);
        write_reg('h2000,32'h31575350,3,0,0,2);
        write_reg('h2004,1,15,0,0,2);
        write_reg('h2000,32'h31575350,15,0,0,0);
        write_reg('h100c,1,15,0,0,2);
        write_reg('h1020,1,15,0,0,0); expect_reg('h1024,0);
        fd=$fopen("waveforms.hex","r"); if (!fd) $fatal(1,"Missing vectors");
        token=0;
        for(i=0;i<1100;i=i+1) begin
            for(j=0;j<758;j=j+1) begin
                rc=$fscanf(fd,"%h\n",word_value); if(rc!=1) $fatal(1,"Short vectors");
                write_reg('h2000+j*4,word_value,15,j%3,0,0);
            end
            token=token+1;
            write_reg('h100c,token,15,2,2,0);
            expect_reg('h1004,2); expect_reg('h1010,token);
            if(i==0) begin
                write_reg('h2000,0,15,0,0,2);
                write_reg('h100c,token+1,15,0,0,2);
                write_reg('h1014,token+1,15,0,0,2);
                write_reg('h1020,1,15,0,0,2);
            end
            for(j=0;j<758;j=j+1) begin
                read_reg('h2000+j*4,value,j%7==0 ? 2 : 0,0);
                $fdisplay(output_fd,"%08x",value);
            end
            write_reg('h1014,token,15,0,0,0); expect_reg('h1004,1);
        end
        $fclose(fd); $fclose(output_fd); expect_reg('h1018,1100);
        read_reg('h2000,value,0,2);
        // Waveform token wrap and reset while the returned buffer is still owned.
        @(negedge clk); dut.waveform.ack_token=32'hffffffff;
        dut.waveform.commit_token=32'hffffffff; dut.waveform.consume_token=32'hffffffff;
        for(j=0;j<758;j=j+1) write_reg('h2000+j*4,j,15,0,0,0);
        write_reg('h100c,1,15,0,0,0); expect_reg('h1010,1);
        @(negedge clk); resetn=0;
        repeat(3) @(negedge clk); resetn=1;
        expect_reg('h1004,1); expect_reg('h1010,0); expect_reg('h1024,0);
        read_reg('h2000,value,0,2);
        write_reg('h2000,123,15,0,0,0);
        @(negedge clk); resetn=0;
        repeat(3) @(negedge clk); resetn=1;
        expect_reg('h1024,0); read_reg('h2000,value,0,2);
        $display("PASS core: AXI ordering/WSTRB/backpressure/reset, commit/watchdog/wrap/snapshot, 1100 waveform frames");
        $finish;
    end
    initial output_fd=$fopen("waveforms-returned.hex","w");
    initial begin repeat(30000000) @(posedge clk); $fatal(1,"Global timeout"); end
endmodule
