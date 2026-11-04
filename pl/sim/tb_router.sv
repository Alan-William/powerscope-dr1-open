`timescale 1ns/1ps
module tb_router #(parameter HEADLESS=0);
    reg clk=0; always #5 clk=~clk;
    reg resetn=0;
    reg [32:0] awaddr=0,araddr=0;
    reg [31:0] wdata=0;
    reg [3:0] wstrb=15;
    reg awvalid=0,wvalid=0,bready=0,arvalid=0,rready=0;
    wire awready,wready,bvalid,arready,rvalid;
    wire [1:0] bresp,rresp;
    wire [31:0] rdata;
    wire [65:0] mawa,mara;
    wire [5:0] mawp,marp;
    wire [63:0] mwd,mrd;
    wire [7:0] mws;
    wire [3:0] mbr,mrr;
    wire [1:0] mawv,mawr,mwv,mwr,mbv,mbrdy,marv,marr,mrv,mrrdy;
    reg [15:0] cycles=0;
    always @(posedge clk) cycles<=cycles+1'b1;
    ps_axi_lite_router dut(
        .clk(clk),.resetn(resetn),
        .s_awaddr(awaddr),.s_awprot(3'b010),.s_awvalid(awvalid),.s_awready(awready),
        .s_wdata(wdata),.s_wstrb(wstrb),.s_wvalid(wvalid),.s_wready(wready),
        .s_bresp(bresp),.s_bvalid(bvalid),.s_bready(bready),
        .s_araddr(araddr),.s_arprot(3'b101),.s_arvalid(arvalid),.s_arready(arready),
        .s_rdata(rdata),.s_rresp(rresp),.s_rvalid(rvalid),.s_rready(rready),
        .m_awaddr(mawa),.m_awprot(mawp),.m_awvalid(mawv),.m_awready(mawr),
        .m_wdata(mwd),.m_wstrb(mws),.m_wvalid(mwv),.m_wready(mwr),
        .m_bresp(mbr),.m_bvalid(mbv),.m_bready(mbrdy),
        .m_araddr(mara),.m_arprot(marp),.m_arvalid(marv),.m_arready(marr),
        .m_rdata(mrd),.m_rresp(mrr),.m_rvalid(mrv),.m_rready(mrrdy));
    genvar g;
    generate for(g=0;g<2;g=g+1) begin: targets
        wire awr,wr,arr;
        wire awgate=cycles[2:0]==g;
        wire wgate=cycles[2:0]==g+4;
        wire argate=cycles[1:0]==g;
        assign mawr[g]=awr && awgate;
        assign mwr[g]=wr && wgate;
        assign marr[g]=arr && argate;
        if (HEADLESS && g==0) begin: removed_video
            ps_axi_lite_error unmapped(
                .clk(clk),.resetn(resetn),
                .s_awvalid(mawv[g] && awgate),.s_awready(awr),
                .s_wvalid(mwv[g] && wgate),.s_wready(wr),
                .s_bresp(mbr[g*2 +: 2]),.s_bvalid(mbv[g]),.s_bready(mbrdy[g]),
                .s_arvalid(marv[g] && argate),.s_arready(arr),
                .s_rdata(mrd[g*32 +: 32]),.s_rresp(mrr[g*2 +: 2]),.s_rvalid(mrv[g]),.s_rready(mrrdy[g]));
        end else begin: present_target
        powerscope_peripheral peripheral(
            .clk(clk),.resetn(resetn),
            .s_axi_awaddr(mawa[g*33 +: 16]),.s_axi_awvalid(mawv[g] && awgate),.s_axi_awready(awr),
            .s_axi_wdata(mwd[g*32 +: 32]),.s_axi_wstrb(mws[g*4 +: 4]),.s_axi_wvalid(mwv[g] && wgate),.s_axi_wready(wr),
            .s_axi_bresp(mbr[g*2 +: 2]),.s_axi_bvalid(mbv[g]),.s_axi_bready(mbrdy[g]),
            .s_axi_araddr(mara[g*33 +: 16]),.s_axi_arvalid(marv[g] && argate),.s_axi_arready(arr),
            .s_axi_rdata(mrd[g*32 +: 32]),.s_axi_rresp(mrr[g*2 +: 2]),.s_axi_rvalid(mrv[g]),.s_axi_rready(mrrdy[g]),
            .rs485_rx(1'b1),.rs485_tx(),.rs485_de());
        end
        always @(posedge clk) if(resetn) begin
            if(mawv[g] && awgate && awr && mawp[g*3 +: 3]!==3'b010) $fatal(1,"AWPROT changed");
            if(marv[g] && argate && arr && marp[g*3 +: 3]!==3'b101) $fatal(1,"ARPROT changed");
        end
    end endgenerate
    task automatic write_reg(input [32:0] a,input [31:0] value,input integer order,input [3:0] mask,input [1:0] expected);
        begin
            fork
                begin
                    if(order==2) repeat(8) @(negedge clk);
                    @(negedge clk); awaddr=a; awvalid=1;
                    @(posedge clk); while(!awready) @(posedge clk);
                    @(negedge clk); awvalid=0;
                end
                begin
                    if(order==1) repeat(8) @(negedge clk);
                    @(negedge clk); wdata=value; wstrb=mask; wvalid=1;
                    @(posedge clk); while(!wready) @(posedge clk);
                    @(negedge clk); wvalid=0;
                end
            join
            while(!bvalid) @(negedge clk);
            repeat(13) begin @(negedge clk); if(!bvalid || bresp!==expected) $fatal(1,"B response stalled"); end
            bready=1; @(negedge clk); bready=0;
        end
    endtask
    task automatic expect_reg(input [32:0] a,input [31:0] expected,input [1:0] response);
        begin
            @(negedge clk); araddr=a; arvalid=1;
            @(posedge clk); while(!arready) @(posedge clk);
            @(negedge clk); arvalid=0;
            while(!rvalid) @(negedge clk);
            repeat(11) begin
                @(negedge clk);
                if(!rvalid || rdata!==expected || rresp!==response) $fatal(1,"Router read %h got %h/%h",a,rdata,rresp);
            end
            rready=1; @(negedge clk); rready=0;
        end
    endtask
    integer i;
    initial begin
        repeat(5) @(negedge clk); resetn=1;
        if (HEADLESS) begin
            for(i=0;i<64;i=i+1) begin
                write_reg(33'h080100008 + ((i%3)*33'h100000),i+100,i%3,15,3);
                expect_reg(33'h080100008 + ((i%3)*33'h100000),0,3);
                write_reg(33'h080400008,i+200,(i+1)%3,15,0);
                expect_reg(33'h080400008,i+200,0);
            end
            // Neither an address with bit 32 set nor a nearby window may alias.
            write_reg(33'h180400008,32'h12345678,2,15,3);
            expect_reg(33'h180400008,0,3);
            expect_reg(33'h080500008,0,3);
            expect_reg(33'h080400008,263,0);
            fork
                write_reg(33'h080100008,999,1,15,3);
                expect_reg(33'h080400008,263,0);
            join
            fork
                write_reg(33'h080400008,777,2,15,0);
                expect_reg(33'h080300008,0,3);
            join
            expect_reg(33'h080400008,777,0);
            // Reset aborts a partial removed-window transaction; no phantom B.
            @(negedge clk); awaddr=33'h080200008; awvalid=1;
            @(negedge clk); awvalid=0; resetn=0;
            repeat(5) @(negedge clk); resetn=1;
            repeat(20) begin @(negedge clk); if(bvalid || rvalid) $fatal(1,"Stale response after reset"); end
            write_reg(33'h080200008,1,2,0,3);
            expect_reg(33'h080400008,0,0);
            $display("PASS headless: removed video windows DECERR, mapped mailbox retained, 33-bit decode, AW/W order, stalls, response backpressure, parallel channels, reset");
            $finish;
        end
        for(i=0;i<64;i=i+1) begin
            write_reg(33'h080300008,i+100,i%3,15,0);
            write_reg(33'h080400008,i+200,(i+1)%3,15,0);
            expect_reg(33'h080300008,i+100,0); expect_reg(33'h080400008,i+200,0);
        end
        // Check the 33rd bit prevents an alias to the new window.
        write_reg(33'h180400008,32'h12345678,2,15,0);
        expect_reg(33'h080300008,32'h12345678,0); expect_reg(33'h080400008,263,0);
        write_reg(33'h080400008,32'h11223344,1,5,0); expect_reg(33'h080400008,32'h00220144,0);
        write_reg(33'h080400020,1,0,15,2); expect_reg(33'h080400034,0,2);
        fork
            write_reg(33'h080400008,999,2,15,0);
            expect_reg(33'h080300008,32'h12345678,0);
        join
        expect_reg(33'h080400008,999,0);
        // Reset with unpaired AW; no stale data may be routed afterward.
        @(negedge clk); awaddr=33'h080400008; awvalid=1;
        @(negedge clk); awvalid=0; resetn=0;
        repeat(5) @(negedge clk); resetn=1;
        expect_reg(33'h080400008,0,0); expect_reg(33'h080300008,0,0);
        $display("PASS router: original/new targets, independent AW/W, downstream stalls, B/R backpressure, 33-bit decode, WSTRB, errors, parallel read/write, reset");
        $finish;
    end
    initial begin repeat(100000) @(posedge clk); $fatal(1,"Router timeout"); end
endmodule
