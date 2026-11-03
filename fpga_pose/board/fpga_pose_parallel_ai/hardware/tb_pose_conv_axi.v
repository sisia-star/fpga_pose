`timescale 1ns/1ps
// Portable behavioral testbench; no Xilinx primitives. RTL timing is not tested here.
module tb_pose_conv_axi;
    localparam LANES=36, FEATURES=768, WORDS=FEATURES*(LANES+1);
    reg clk=0; always #5 clk=~clk;
    reg resetn=0;
    reg [11:0] awaddr=0, araddr=0;
    reg awvalid=0, wvalid=0, bready=0, arvalid=0, rready=0;
    wire awready,wready,bvalid,arready,rvalid;
    reg [31:0] wdata=0; reg [3:0] wstrb=4'hf;
    wire [1:0] bresp,rresp; wire [31:0] rdata;

    reg [31:0] value;
    reg [31:0] tx=0;reg tv=0,tl=0;reg [3:0]tk=15;wire tr;
    wire [31:0]rx;wire rv,rl;wire [3:0]rk;reg rr=0;
    reg signed [15:0] features[0:FEATURES-1], weights[0:FEATURES*LANES-1];
    reg signed [63:0] expected[0:LANES-1],actual;
    integer f,l,i,seen=0,pass;
    reg [31:0] low;
    pose_conv_axi dut(
        .s_axi_aclk(clk),.s_axi_aresetn(resetn),
        .s_axi_awaddr(awaddr),.s_axi_awprot(3'b0),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
        .s_axi_wdata(wdata),.s_axi_wstrb(wstrb),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
        .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(bready),
        .s_axi_araddr(araddr),.s_axi_arprot(3'b0),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
        .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(rready),
        .s_axis_tdata(tx),.s_axis_tvalid(tv),.s_axis_tready(tr),.s_axis_tlast(tl),.s_axis_tkeep(tk),
        .m_axis_tdata(rx),.m_axis_tvalid(rv),.m_axis_tready(rr),.m_axis_tlast(rl),.m_axis_tkeep(rk));

    task axi_write;
        input [11:0] address; input [31:0] data;
        input integer channel_gap, response_stall; input [1:0] wanted_response;
        reg [1:0] held_response;
        begin
            fork
                begin
                    if(channel_gap<0) repeat(-channel_gap) @(negedge clk);
                    @(negedge clk); awaddr=address; awvalid=1;
                    @(posedge clk); while(!awready) @(posedge clk);
                    @(negedge clk); awvalid=0;
                end
                begin
                    if(channel_gap>0) repeat(channel_gap) @(negedge clk);
                    @(negedge clk); wdata=data; wvalid=1;
                    @(posedge clk); while(!wready) @(posedge clk);
                    @(negedge clk); wvalid=0;
                end
            join
            while(!bvalid) @(negedge clk);
            held_response=bresp;
            if(bresp!==wanted_response) $fatal(1,"Unexpected BRESP addr=%h response=%h",address,bresp);
            repeat(response_stall) begin
                @(negedge clk);
                if(!bvalid || bresp!==held_response) $fatal(1,"B response lost during backpressure");
                if(awready || wready) $fatal(1,"Accepted another write while B response pending");
            end
            bready=1; @(posedge clk); @(negedge clk); bready=0;
        end
    endtask

    task axi_read;
        input [11:0] address; input integer response_stall;
        input [1:0] wanted_response; output [31:0] data;
        reg [31:0] held_data, held_dist_addr; reg [1:0] held_response;
        begin
            @(negedge clk); araddr=address; arvalid=1;
            @(posedge clk); while(!arready) @(posedge clk);
            @(negedge clk); arvalid=0;
            while(!rvalid) @(negedge clk);
            held_data=rdata; held_response=rresp; held_dist_addr=0;
            if(rresp!==wanted_response) $fatal(1,"Unexpected RRESP addr=%h response=%h",address,rresp);
            repeat(response_stall) begin
                @(negedge clk);
                if(!rvalid || rdata!==held_data || rresp!==held_response)
                    $fatal(1,"R response lost/changed during backpressure");
                if(arready) $fatal(1,"Accepted another read while response pending");
                if(1'b0)
                    $fatal(1,"DIST_HI auto-increment happened before R handshake");
            end
            data=held_data; rready=1; @(posedge clk); @(negedge clk); rready=0;
        end
    endtask


    always @(posedge clk) if(rv && rr)begin
        if(rk!==15 || rl!==(seen==LANES*2-1))$fatal(1,"Output framing");
        if(seen%2==0)low=rx;
        else begin
            actual=$signed({rx,low});
            if(actual!==expected[seen/2])$fatal(1,"Convolution mismatch lane=%0d got=%0d expected=%0d",seen/2,actual,expected[seen/2]);
        end
        seen=seen+1;
    end
    task send_word;
        input [31:0]word; input last;
        begin
            @(negedge clk);tx=word;tv=1;tl=last;
            @(posedge clk);while(!tr)@(posedge clk);
            @(negedge clk);tv=0;tl=0;
        end
    endtask
    initial begin #20000000;$fatal(1,"Watchdog");end
    initial begin
        repeat(6)@(negedge clk);resetn=1;
        axi_read('h08,5,0,value);if(value!==32'h50434e31)$fatal(1,"Magic");
        axi_read('h0c,0,0,value);if(value!==FEATURES)$fatal(1,"Features");
        for(pass=0;pass<2;pass=pass+1)begin
            seen=0;
            for(f=0;f<FEATURES;f=f+1)features[f]=(pass==0)?((f%2)?-32768:32767):(f-384);
            for(l=0;l<LANES;l=l+1)begin
                expected[l]=0;
                for(f=0;f<FEATURES;f=f+1)begin
                    weights[f*LANES+l]=(pass==0)?((l%2)?-32768:32767):((f*3+l*11)%163-81);
                    expected[l]=expected[l]+$signed(features[f])*$signed(weights[f*LANES+l]);
                end
            end
            axi_write('h00,1,pass==0?4:-3,3,0);
            fork
                begin
                    for(f=0;f<FEATURES;f=f+1)send_word({16'b0,features[f]},0);
                    for(i=0;i<FEATURES*LANES;i=i+1)send_word({16'b0,weights[i]},i==FEATURES*LANES-1);
                end
                begin
                    while(seen<LANES*2)begin
                        @(negedge clk);rr=($random%4!=0);
                    end
                    @(negedge clk);rr=0;
                end
            join
            axi_read(0,3,0,value);if(value[3:0]!==6)$fatal(1,"Not done");
            axi_read('h18,0,0,value);if(value!==pass+1)$fatal(1,"Completion");
        end
        axi_write(0,1,0,0,0);send_word(123,1);
        axi_read('h1c,0,0,value);if(value!==3)$fatal(1,"Early TLAST guard");
        axi_read('h18,0,0,value);if(value!==2)$fatal(1,"Faulted transaction counted");
        $display("PASS: CONV 36 lanes x 768 inputs, signed16 extremes, repeated packets, AXI AW/W independence, output backpressure, exact int48 MAC, TLAST fault");
        $finish;
    end
endmodule
