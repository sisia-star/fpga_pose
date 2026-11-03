`timescale 1ns/1ps
module tb_image_preprocess_axi;
    reg clk=0; always #5 clk=~clk;
    reg resetn=0;
    reg [11:0] awaddr=0, araddr=0;
    reg awvalid=0,wvalid=0,bready=0,arvalid=0,rready=0;
    reg [31:0] wdata=0;
    reg [3:0] wstrb=0;
    wire awready,wready,bvalid,arready,rvalid;
    wire [1:0] bresp,rresp;
    wire [31:0] rdata;
    reg [31:0] indata=0;
    reg [3:0] inkeep=15;
    reg invalid=0,inlast=0,outready=0;
    wire inready,outvalid,outlast;
    wire [31:0] outdata;
    wire [3:0] outkeep;
    image_preprocess_axi dut (
        .s_axi_aclk(clk),.s_axi_aresetn(resetn),
        .s_axi_awaddr(awaddr),.s_axi_awprot(3'b0),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
        .s_axi_wdata(wdata),.s_axi_wstrb(wstrb),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
        .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(bready),
        .s_axi_araddr(araddr),.s_axi_arprot(3'b0),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
        .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(rready),
        .s_axis_tdata(indata),.s_axis_tkeep(inkeep),.s_axis_tvalid(invalid),.s_axis_tlast(inlast),.s_axis_tready(inready),
        .m_axis_tdata(outdata),.m_axis_tkeep(outkeep),.m_axis_tvalid(outvalid),.m_axis_tlast(outlast),.m_axis_tready(outready)
    );
    integer seed=32'h12345678, output_seen=0, tests=0;
    integer frame_width=0,frame_height=0;
    reg scoring=0, random_ready=0;
    reg [31:0] pixels[0:921599];
    reg held_output=0;
    reg [31:0] held_data;
    reg held_last;
    reg [1:0] held_bresp,held_rresp;
    reg [31:0] held_rdata;
    reg held_b=0,held_r=0;
    integer sum_b,sum_g,sum_r,idx,ox,oy;
    reg [31:0] expected;
    // Handshake monitor: compare complete pixel order and rounded arithmetic.
    always @(posedge clk) if (resetn) begin
        if (held_output && (!outvalid || outdata!==held_data || outlast!==held_last || outkeep!==4'hf))
            $fatal(1,"Output changed while backpressured");
        held_output=outvalid && !outready;
        held_data=outdata; held_last=outlast;
        if (held_b && (!bvalid || bresp!==held_bresp)) $fatal(1,"B response changed under backpressure");
        held_b=bvalid && !bready; held_bresp=bresp;
        if (held_r && (!rvalid || rdata!==held_rdata || rresp!==held_rresp)) $fatal(1,"R response changed under backpressure");
        held_r=rvalid && !rready; held_rdata=rdata; held_rresp=rresp;
        if (scoring && outvalid && outready) begin
            ox=(output_seen % (frame_width/2))*2;
            oy=(output_seen / (frame_width/2))*2;
            idx=oy*frame_width+ox;
            sum_b=pixels[idx][7:0]+pixels[idx+1][7:0]+pixels[idx+frame_width][7:0]+pixels[idx+frame_width+1][7:0];
            sum_g=pixels[idx][15:8]+pixels[idx+1][15:8]+pixels[idx+frame_width][15:8]+pixels[idx+frame_width+1][15:8];
            sum_r=pixels[idx][23:16]+pixels[idx+1][23:16]+pixels[idx+frame_width][23:16]+pixels[idx+frame_width+1][23:16];
            expected=(((sum_r+2)>>2)<<16)|(((sum_g+2)>>2)<<8)|((sum_b+2)>>2);
            if (outdata!==expected) $fatal(1,"Pixel %0d mismatch got=%08x expected=%08x",output_seen,outdata,expected);
            if (outlast !== (output_seen==(frame_width*frame_height/4)-1)) $fatal(1,"TLAST order mismatch at %0d",output_seen);
            if (outkeep!==4'hf) $fatal(1,"Output TKEEP mismatch");
            output_seen=output_seen+1;
        end
    end
    always @(negedge clk) if (random_ready) outready=($random(seed)&3)!=0;
    task write_reg;
        input [11:0] address;
        input [31:0] value;
        input [3:0] strobes;
        input integer aw_delay,w_delay;
        input [1:0] response;
        integer t;
        begin
            fork
                begin
                    repeat(aw_delay) @(negedge clk);
                    @(negedge clk); awaddr=address; awvalid=1;
                    t=0; @(posedge clk); while (!awready) begin @(posedge clk); t=t+1; if(t>100)$fatal(1,"AW timeout"); end
                    @(negedge clk); awvalid=0;
                end
                begin
                    repeat(w_delay) @(negedge clk);
                    @(negedge clk); wdata=value; wstrb=strobes; wvalid=1;
                    @(posedge clk); while (!wready) @(posedge clk);
                    @(negedge clk); wvalid=0;
                end
            join
            t=0;
            while (!bvalid) begin @(negedge clk); t=t+1; if(t>100)$fatal(1,"B timeout"); end
            if(bresp!==response)$fatal(1,"Write %x response got=%x expected=%x",address,bresp,response);
            repeat(3) @(negedge clk);
            bready=1; @(negedge clk); bready=0;
        end
    endtask
    task read_reg;
        input [11:0] address;
        output [31:0] value;
        input [1:0] response;
        integer t;
        begin
            @(negedge clk); araddr=address; arvalid=1;
            @(posedge clk); while(!arready) @(posedge clk);
            @(negedge clk); arvalid=0;
            t=0; while(!rvalid) begin @(negedge clk); t=t+1; if(t>100)$fatal(1,"R timeout"); end
            if(rresp!==response)$fatal(1,"Read %x response mismatch",address);
            value=rdata;
            repeat(3) @(negedge clk);
            rready=1; @(negedge clk); rready=0;
        end
    endtask
    task send_pixel;
        input [31:0] value;
        input last;
        input [3:0] keep;
        integer t;
        begin
            @(negedge clk); indata=value; inlast=last; inkeep=keep; invalid=1;
            t=0; @(posedge clk); while(!inready) begin @(posedge clk); t=t+1; if(t>1000)$fatal(1,"Input timeout"); end
            @(negedge clk); invalid=0;
        end
    endtask
    task configure;
        input integer w,h;
        begin
            write_reg(0,4,15,0,2,0);
            write_reg(4,w,15,3,0,0);
            write_reg(8,h,15,0,3,0);
        end
    endtask
    reg [31:0] v,count_before;
    task run_frame;
        input integer w,h,stalls;
        integer i,waits,gap;
        begin
            configure(w,h);
            read_reg('h1c,count_before,0);
            frame_width=w; frame_height=h; output_seen=0;
            for(i=0;i<w*h;i=i+1) begin
                // Include unused high bytes, channel extremes and sums at ties.
                pixels[i]=32'hff000000|((i*37&255)<<16)|((i*17+255&255)<<8)|(i*7+1&255);
            end
            scoring=1; random_ready=stalls; outready=1;
            write_reg(0,1,15,0,0,0);
            // Consecutive input pixels when stalls=0, not one-pixel task bubbles.
            @(negedge clk); invalid=1; inkeep=15; indata=pixels[0]; inlast=(w*h==1);
            for(i=0;i<w*h;i=i+1) begin
                waits=0; @(posedge clk);
                while(!inready) begin @(posedge clk); waits=waits+1; if(waits>1000)$fatal(1,"Stream timeout"); end
                if(!stalls && waits!=0)$fatal(1,"Unstalled input did not sustain one pixel/clock");
                @(negedge clk); invalid=0;
                if(stalls) begin gap=($random(seed)&3); repeat(gap) @(negedge clk); end
                if(i+1<w*h) begin invalid=1; indata=pixels[i+1]; inlast=(i+2==w*h); end
            end
            waits=0;
            while(output_seen<w*h/4) begin @(negedge clk); waits=waits+1; if(waits>1000)$fatal(1,"Output drain timeout"); end
            random_ready=0; outready=1;
            read_reg(0,v,0); if((v&11)!==2)$fatal(1,"Not done after frame ctrl=%x",v);
            read_reg('h10,v,0); if(v!==w*h)$fatal(1,"Input count mismatch");
            read_reg('h14,v,0); if(v!==w*h/4)$fatal(1,"Output count mismatch");
            read_reg('h1c,v,0); if(v!==count_before+1)$fatal(1,"Completion count mismatch");
            read_reg('h18,v,0); if(v<w*h+2)$fatal(1,"Cycle count too small");
            if(inready)$fatal(1,"Accepted overrun after complete frame");
            scoring=0; tests=tests+1;
        end
    endtask
    task expect_fault;
        input [31:0] code;
        begin
            read_reg(0,v,0); if((v&11)!==8)$fatal(1,"Fault/busy/done wrong: %x",v);
            read_reg('h20,v,0); if(v!==code)$fatal(1,"Error got=%d expected=%d",v,code);
            read_reg('h1c,v,0); if(v!==count_before)$fatal(1,"Fault incorrectly counted completion");
            if(inready)$fatal(1,"Faulted core ready");
            tests=tests+1;
        end
    endtask
    integer d;
    initial begin
        repeat(6) @(negedge clk); resetn=1;
        read_reg('h0c,v,0); if(v!==32'h50494d32)$fatal(1,"Wrong ID");
        read_reg('h24,v,0); if(v!==1280)$fatal(1,"Wrong max width");
        read_reg('h28,v,0); if(v!==720)$fatal(1,"Wrong max height");
        run_frame(2,2,0); run_frame(8,6,1); run_frame(64,32,1);
        run_frame(1280,2,0); run_frame(2,720,1); run_frame(640,360,0); run_frame(1280,720,0);
        // Completion must wait for output transfer, including a stalled TLAST.
        configure(2,2);read_reg('h1c,count_before,0);outready=0;
        write_reg(0,1,15,0,0,0);
        send_pixel(32'hffffffff,0,15);send_pixel(32'hffffffff,0,15);
        send_pixel(32'hffffffff,0,15);send_pixel(32'hffffffff,1,15);
        repeat(20) @(negedge clk);
        if(!outvalid || !outlast || outdata!==32'h00ffffff)$fatal(1,"Stalled final output mismatch");
        read_reg(0,v,0);if((v&11)!==1)$fatal(1,"Done before final TLAST transfer");
        read_reg('h1c,v,0);if(v!==count_before)$fatal(1,"Count before final output transfer");
        @(negedge clk);outready=1;repeat(3) @(negedge clk);
        read_reg('h1c,v,0);if(v!==count_before+1)$fatal(1,"Missing final transfer completion");
        tests=tests+1;
        read_reg('h1c,count_before,0);
        for(d=0;d<5;d=d+1) begin
            case(d)
                0:configure(0,2); 1:configure(3,2); 2:configure(2,3);
                3:configure(1282,2); 4:configure(2,722);
            endcase
            write_reg(0,1,15,2,0,2); expect_fault(1);
        end
        configure(2,2); write_reg(0,1,15,0,0,0);
        send_pixel(32'h1234,1,15); expect_fault(3);
        configure(2,2); write_reg(0,1,15,0,0,0);
        send_pixel(1,0,15);send_pixel(2,0,15);send_pixel(3,0,15);send_pixel(4,0,15);expect_fault(4);
        configure(2,2); write_reg(0,1,15,0,0,0);
        send_pixel(1,0,7);expect_fault(5);
        configure(2,2); write_reg(0,1,15,0,0,0);
        write_reg(4,4,15,0,2,2);expect_fault(2);
        configure(2,2); write_reg('h0c,0,15,2,0,2);expect_fault(6);
        configure(2,2); write_reg(5,0,15,0,2,2);expect_fault(7);
        configure(2,2); read_reg('h2c,v,2);expect_fault(6);
        configure(2,2); read_reg(5,v,2);expect_fault(7);
        configure(2,2); write_reg(4,255,0,2,0,0);
        read_reg(4,v,0);if(v!==2)$fatal(1,"Zero-strobe modified width");
        write_reg(4,4,1,0,2,0);read_reg(4,v,0);if(v!==4)$fatal(1,"WSTRB merge wrong");
        // Clear aborts an active job and permits another good frame.
        write_reg(0,1,15,0,0,0);send_pixel(0,0,15);write_reg(0,4,15,2,0,0);
        read_reg(0,v,0);if(v!==4)$fatal(1,"Clear did not return idle");
        run_frame(4,4,1);
        $display("PASS: IMAGE_AXI %0d frames/fault cases; rounded RGB arithmetic, order, TLAST, one-pixel-per-clock, randomized stalls, independent AW/W, response hold, clear recovery",tests);
        $finish;
    end
    initial begin #100000000; $fatal(1,"Global test timeout"); end
endmodule
