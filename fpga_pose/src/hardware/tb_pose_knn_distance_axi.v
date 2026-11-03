`timescale 1ns/1ps
// Portable behavioral testbench; no Xilinx primitives. RTL timing is not tested here.
module tb_pose_knn_distance_axi;
    parameter LANES=32;
    localparam DIMS=99, MAX_ROWS=1024, GROUPS=(DIMS+LANES-1)/LANES;
    localparam PAD_DIMS=GROUPS*LANES, TRAIN_WORDS=PAD_DIMS*MAX_ROWS;
    localparam LEVELS=$clog2(LANES);
    reg clk=0; always #5 clk=~clk;
    reg resetn=0;
    reg [11:0] awaddr=0, araddr=0;
    reg awvalid=0, wvalid=0, bready=0, arvalid=0, rready=0;
    wire awready,wready,bvalid,arready,rvalid;
    reg [31:0] wdata=0; reg [3:0] wstrb=4'hf;
    wire [1:0] bresp,rresp; wire [31:0] rdata;
    reg signed [15:0] reference_training[0:TRAIN_WORDS-1];
    reg signed [15:0] reference_query[0:PAD_DIMS-1];
    integer row,dim,index;
    reg [31:0] value,lo,hi,count_before;
    reg [63:0] actual,expected_value;

    pose_knn_distance_axi #(.LANES(LANES)) dut(
        .s_axi_aclk(clk),.s_axi_aresetn(resetn),
        .s_axi_awaddr(awaddr),.s_axi_awprot(3'b0),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
        .s_axi_wdata(wdata),.s_axi_wstrb(wstrb),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
        .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(bready),
        .s_axi_araddr(araddr),.s_axi_arprot(3'b0),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
        .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(rready));

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
            held_data=rdata; held_response=rresp; held_dist_addr=dut.dist_addr;
            if(rresp!==wanted_response) $fatal(1,"Unexpected RRESP addr=%h response=%h",address,rresp);
            repeat(response_stall) begin
                @(negedge clk);
                if(!rvalid || rdata!==held_data || rresp!==held_response)
                    $fatal(1,"R response lost/changed during backpressure");
                if(arready) $fatal(1,"Accepted another read while response pending");
                if(address=='h20 && dut.dist_addr!==held_dist_addr)
                    $fatal(1,"DIST_HI auto-increment happened before R handshake");
            end
            data=held_data; rready=1; @(posedge clk); @(negedge clk); rready=0;
        end
    endtask

    function [63:0] reference_distance;
        input integer wanted_row;
        integer feature;
        reg signed [63:0] difference;
        reg [63:0] total;
        begin
            total=0;
            for(feature=0;feature<DIMS;feature=feature+1) begin
                difference=$signed(reference_query[feature]);
                difference=difference-$signed(reference_training[wanted_row*PAD_DIMS+feature]);
                total=total+difference*difference;
            end
            reference_distance=total;
        end
    endfunction

    task upload_query;
        integer feature;
        begin
            axi_write('h10,0,2,3,0);
            for(feature=0;feature<PAD_DIMS;feature=feature+1)
                axi_write('h14,{16'b0,reference_query[feature]},0,0,0);
            axi_read('h10,0,0,value);
            if(value!==PAD_DIMS) $fatal(1,"QUERY_DATA auto-increment incorrect");
        end
    endtask
    task wait_done;
        integer polls;
        reg [31:0] status;
        begin
            status=1; polls=0;
            while(status[0]) begin
                axi_read('h00,0,0,status); polls=polls+1;
                if(polls>200000) $fatal(1,"PL completion timeout");
            end
            if(!status[1]) $fatal(1,"Done not latched after calculation");
        end
    endtask
    task check_distances;
        input integer count;
        integer wanted_row;
        reg [31:0] low_word,high_word,pointer;
        reg [63:0] got;
        begin
            axi_write('h18,0,-3,2,0);
            for(wanted_row=0;wanted_row<count;wanted_row=wanted_row+1) begin
                axi_read('h1c,3,0,low_word);
                axi_read('h18,0,0,pointer);
                if(pointer!==wanted_row) $fatal(1,"LO unexpectedly advanced pointer");
                axi_read('h20,5,0,high_word);
                got={high_word,low_word};
                if(got!==reference_distance(wanted_row))
                    $fatal(1,"Distance mismatch row=%0d got=%0d expected=%0d",wanted_row,got,reference_distance(wanted_row));
                axi_read('h18,0,0,pointer);
                if(pointer!==wanted_row+1) $fatal(1,"HI did not advance pointer once");
            end
        end
    endtask


    task check_top5;
        input integer rows;
        integer r,k,j,pos,wanted;
        reg [63:0] best[0:4],d;
        integer ids[0:4];
        reg [31:0] idx,l,h,c;
        begin
            for(k=0;k<5;k=k+1) begin best[k]=64'hffffffffffffffff; ids[k]=-1; end
            for(r=0;r<rows;r=r+1) begin
                d=reference_distance(r); pos=5;
                for(k=0;k<5;k=k+1) if(pos==5 && d<best[k]) pos=k;
                if(pos<5) begin
                    for(j=4;j>pos;j=j-1) begin best[j]=best[j-1]; ids[j]=ids[j-1]; end
                    best[pos]=d; ids[pos]=r;
                end
            end
            wanted=(rows<5)?rows:5;
            axi_read('h44,0,0,c); if(c!==wanted) $fatal(1,"Top count mismatch");
            for(k=0;k<wanted;k=k+1) begin
                axi_write('h48,k,k%3-1,2,0);
                axi_read('h4c,3,0,idx); axi_read('h50,2,0,l); axi_read('h54,4,0,h);
                if(idx!==ids[k] || {h,l}!==best[k])
                    $fatal(1,"Top5 mismatch rank=%0d row=%0d wanted=%0d d=%0d expected=%0d",k,idx,ids[k],{h,l},best[k]);
            end
        end
    endtask
    task run_and_check;
        input integer rows;
        begin
            axi_write('h04,rows,0,0,0); axi_write('h00,1,-3,2,0); wait_done;
            axi_read('h30,0,0,value);
            if(value!==rows*(GROUPS+LEVELS+4)+1)
                $fatal(1,"Pipeline cycle mismatch: %0d",value);
            check_distances(rows); check_top5(rows);
        end
    endtask
    initial begin #40000000; $fatal(1,"Testbench watchdog"); end
    initial begin
        repeat(6) @(negedge clk); resetn=1;
        axi_read('h24,8,0,value); if(value!==32'h504b5032) $fatal(1,"Wrong parallel ID");
        axi_read('h40,0,0,value); if(value!==LANES) $fatal(1,"Wrong lanes");
        axi_read('h58,0,0,value); if(value!==PAD_DIMS) $fatal(1,"Wrong padded dims");
        axi_read('h38,0,0,value); if(value!==DIMS) $fatal(1,"Wrong public dims");
        axi_write('h04,0,4,5,0); axi_write('h00,1,-4,5,2);
        axi_read('h34,0,0,value); if(value!==2) $fatal(1,"Zero rows accepted");
        axi_write('h04,MAX_ROWS+1,0,0,0); axi_write('h00,1,0,0,2);
        axi_write('h08,TRAIN_WORDS,0,0,2);
        axi_write('h10,PAD_DIMS,0,0,2);
        axi_write('h18,MAX_ROWS,0,0,2);
        axi_write('h48,5,0,0,2);
        axi_write('h24,0,0,0,2);
        axi_write('h005,0,0,0,2);
        axi_read('h34,0,0,value); if(value!==7) $fatal(1,"Alignment guard");
        // Complete BRAM upload; padding deliberately poisoned to prove masking.
        axi_write('h08,0,0,0,0);
        for(row=0;row<MAX_ROWS;row=row+1) begin
            for(dim=0;dim<PAD_DIMS;dim=dim+1) begin
                index=row*PAD_DIMS+dim;
                if(dim>=DIMS) reference_training[index]=-32768;
                else if(row==0) reference_training[index]=-32768;
                else if(row==1) reference_training[index]=32767;
                else reference_training[index]=(row%17-8)*211+dim*3;
                axi_write('h0c,{16'b0,reference_training[index]},index%29==0?3:0,index%31==0?2:0,0);
            end
        end
        axi_read('h08,0,0,value); if(value!==TRAIN_WORDS) $fatal(1,"Train pointer");
        axi_write('h0c,0,0,0,2);
        for(dim=0;dim<PAD_DIMS;dim=dim+1) reference_query[dim]=32767;
        upload_query;
        axi_write('h14,0,0,0,2);
        run_and_check(1); run_and_check(2); run_and_check(19);
        // Signed extremes, all output addresses, stable distance/row tie order.
        run_and_check(MAX_ROWS);
        axi_read('h1c,0,2,value);
        axi_read('h34,0,0,value); if(value!==5) $fatal(1,"Distance boundary");
        // Addressed byte writes exercise the physical bank mapping.
        axi_write('h08,4*PAD_DIMS+37,0,0,0);
        wstrb=1; axi_write('h0c,32'h000000a5,-2,2,0); wstrb=15;
        reference_training[4*PAD_DIMS+37][7:0]=8'ha5;
        axi_write('h08,4*PAD_DIMS+37,0,0,0);
        wstrb=2; axi_write('h0c,32'h00005a00,2,2,0); wstrb=15;
        reference_training[4*PAD_DIMS+37][15:8]=8'h5a;
        for(dim=0;dim<PAD_DIMS;dim=dim+1) reference_query[dim]=dim-49;
        upload_query; run_and_check(MAX_ROWS);
        axi_read('h2c,0,0,count_before);
        // Long run is intentional: rejection writes must arrive before completion.
        axi_write('h08,0,0,0,0); axi_write('h10,0,0,0,0);
        axi_write('h00,1,0,0,0);
        axi_write('h0c,0,2,2,2); axi_write('h14,0,-2,2,2); axi_write('h00,1,0,0,2);
        wait_done; axi_read('h2c,0,0,value);
        if(value!==count_before) $fatal(1,"Faulted inference counted");
        axi_read('h34,0,0,value); if(value!==1) $fatal(1,"Busy guard");
        check_distances(MAX_ROWS); check_top5(MAX_ROWS);
        run_and_check(MAX_ROWS);
        axi_read('h2c,0,0,value); if(value!==count_before+1) $fatal(1,"Fault recovery");
        $display("PASS: AXI AW/W independence; parallel lanes=%0d; signed16 extremes; padding masked; 1/2/19/1024 rows; exact distances and PL top5; ties; byte writes; backpressure; busy guards; cycle count",LANES);
        $finish;
    end
endmodule
