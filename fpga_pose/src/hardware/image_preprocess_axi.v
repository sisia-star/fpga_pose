`timescale 1ns/1ps
// Real PL image arithmetic. Four-byte pixels are little-endian B,G,R,unused.
// A 2x2 box filter downsamples both dimensions by two. All three channels
// are computed together, with one input pixel/clock when the output is ready.
// AXI stream TLAST describes a complete frame, NOT the end of a video line.
module image_preprocess_axi #(
    parameter integer MAX_WIDTH = 1280,
    parameter integer MAX_HEIGHT = 720,
    parameter integer C_S_AXI_ADDR_WIDTH = 12
) (
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI_CLK, ASSOCIATED_BUSIF S_AXI:S_AXIS:M_AXIS, ASSOCIATED_RESET s_axi_aresetn, FREQ_HZ 100000000" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 S_AXI_CLK CLK" *) input wire s_axi_aclk,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI_RST, POLARITY ACTIVE_LOW" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 S_AXI_RST RST" *) input wire s_axi_aresetn,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 12, READ_WRITE_MODE READ_WRITE" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR" *) input wire [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWPROT" *) input wire [2:0] s_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *) input wire s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *) output wire s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WDATA" *) input wire [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *) input wire [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WVALID" *) input wire s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WREADY" *) output wire s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BRESP" *) output reg [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BVALID" *) output reg s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BREADY" *) input wire s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *) input wire [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARPROT" *) input wire [2:0] s_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *) input wire s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *) output wire s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RDATA" *) output reg [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RRESP" *) output reg [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RVALID" *) output reg s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RREADY" *) input wire s_axi_rready,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXIS, TDATA_NUM_BYTES 4, HAS_TKEEP 1, HAS_TLAST 1" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TDATA" *) input wire [31:0] s_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TKEEP" *) input wire [3:0] s_axis_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TLAST" *) input wire s_axis_tlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TVALID" *) input wire s_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 S_AXIS TREADY" *) output wire s_axis_tready,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME M_AXIS, TDATA_NUM_BYTES 4, HAS_TKEEP 1, HAS_TLAST 1" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TDATA" *) output reg [31:0] m_axis_tdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TKEEP" *) output wire [3:0] m_axis_tkeep,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TLAST" *) output reg m_axis_tlast,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TVALID" *) output reg m_axis_tvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:axis:1.0 M_AXIS TREADY" *) input wire m_axis_tready
);
    function [31:0] merge_bytes;
        input [31:0] previous, incoming;
        input [3:0] enables;
        integer k;
        begin
            merge_bytes=previous;
            for (k=0;k<4;k=k+1) if (enables[k]) merge_bytes[k*8+:8]=incoming[k*8+:8];
        end
    endfunction
    function [7:0] average;
        input [8:0] top, bottom;
        reg [10:0] rounded;
        begin rounded={2'b0,top}+{2'b0,bottom}+11'd2; average=rounded[9:2]; end
    endfunction
    localparam [1:0] OKAY=0, SLVERR=2;
    localparam [31:0] ERR_DIMENSIONS=1, ERR_BUSY=2, ERR_EARLY_LAST=3,
        ERR_MISSING_LAST=4, ERR_KEEP=5, ERR_REGISTER=6, ERR_ALIGNMENT=7;
    reg [31:0] width, height;
    reg [31:0] input_count, output_count, cycle_count, last_cycles, completed_count, error_code;
    reg [31:0] total_pixels;
    reg [15:0] x, y;
    reg busy, done, fault, input_done;
    reg [23:0] left_pixel;
    // Each word holds the B,G,R sums of a horizontal pair from the even row.
    // Synchronous single read + single write port, never reset the RAM array.
    (* ram_style = "block" *) reg [26:0] pair_line [0:(MAX_WIDTH/2)-1];
    reg [26:0] top_pair;
    reg [26:0] bottom_pair;
    reg pair_pending, pair_last;
    wire output_slot = !m_axis_tvalid || m_axis_tready;
    assign s_axis_tready=s_axi_aresetn && busy && !input_done && output_slot;
    assign m_axis_tkeep=4'hf;
    wire input_accept=s_axis_tvalid && s_axis_tready;
    wire expected_last=(input_count+1 == total_pixels);
    wire [20:0] configured_pixels=width[10:0]*height[9:0];
    wire good_input=(s_axis_tkeep==4'hf) && (s_axis_tlast==expected_last);
    wire [8:0] sum_b={1'b0,left_pixel[7:0]}+{1'b0,s_axis_tdata[7:0]};
    wire [8:0] sum_g={1'b0,left_pixel[15:8]}+{1'b0,s_axis_tdata[15:8]};
    wire [8:0] sum_r={1'b0,left_pixel[23:16]}+{1'b0,s_axis_tdata[23:16]};
    always @(posedge s_axi_aclk) begin
        if (s_axi_aresetn && input_accept && good_input && x[0]) begin
            if (!y[0]) pair_line[x>>1]<={sum_r,sum_g,sum_b};
            else top_pair<=pair_line[x>>1];
        end
    end
    reg aw_pending,w_pending;
    reg [C_S_AXI_ADDR_WIDTH-1:0] aw_saved;
    reg [31:0] w_saved;
    reg [3:0] strobe_saved;
    wire write_commit=aw_pending && w_pending && !s_axi_bvalid;
    assign s_axi_awready=s_axi_aresetn && !aw_pending && !s_axi_bvalid;
    assign s_axi_wready=s_axi_aresetn && !w_pending && !s_axi_bvalid;
    assign s_axi_arready=s_axi_aresetn && !s_axi_rvalid;
    task abort_frame;
        input [31:0] code;
        begin
            busy<=0; done<=0; fault<=1; error_code<=code; input_done<=1;
            m_axis_tvalid<=0; m_axis_tlast<=0; pair_pending<=0;
            last_cycles<=cycle_count+(busy ? 32'd1 : 32'd0);
        end
    endtask
    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            width<=640; height<=360; total_pixels<=0; x<=0; y<=0;
            input_count<=0; output_count<=0; cycle_count<=0; last_cycles<=0; completed_count<=0;
            error_code<=0; busy<=0; done<=0; fault<=0; input_done<=0;
            left_pixel<=0; bottom_pair<=0; pair_pending<=0; pair_last<=0;
            m_axis_tdata<=0; m_axis_tvalid<=0; m_axis_tlast<=0;
            aw_pending<=0; w_pending<=0; aw_saved<=0; w_saved<=0; strobe_saved<=0;
            s_axi_bresp<=OKAY; s_axi_bvalid<=0; s_axi_rdata<=0; s_axi_rresp<=OKAY; s_axi_rvalid<=0;
        end else begin
            if (busy) cycle_count<=cycle_count+1;
            if (busy && output_slot) begin
                m_axis_tvalid<=pair_pending;
                m_axis_tlast<=pair_pending && pair_last;
                if (pair_pending) m_axis_tdata<={8'd0,
                    average(top_pair[26:18],bottom_pair[26:18]),
                    average(top_pair[17:9],bottom_pair[17:9]),
                    average(top_pair[8:0],bottom_pair[8:0])};
                pair_pending<=0;
            end
            if (busy && m_axis_tvalid && m_axis_tready) begin
                output_count<=output_count+1;
                if (m_axis_tlast) begin
                    busy<=0; done<=1; completed_count<=completed_count+1;
                    last_cycles<=cycle_count+1;
                end
            end
            if (input_accept) begin
                input_count<=input_count+1;
                if (!good_input) begin
                    if (s_axis_tkeep!=4'hf) abort_frame(ERR_KEEP);
                    else if (s_axis_tlast) abort_frame(ERR_EARLY_LAST);
                    else abort_frame(ERR_MISSING_LAST);
                end else begin
                    if (!x[0]) left_pixel<=s_axis_tdata[23:0];
                    else if (y[0]) begin
                        bottom_pair<={sum_r,sum_g,sum_b}; pair_pending<=1; pair_last<=expected_last;
                    end
                    if (x+1==width) begin x<=0; y<=y+1; end else x<=x+1;
                    if (expected_last) input_done<=1;
                end
            end
            if (s_axi_bvalid && s_axi_bready) s_axi_bvalid<=0;
            if (s_axi_awready && s_axi_awvalid) begin aw_saved<=s_axi_awaddr; aw_pending<=1; end
            if (s_axi_wready && s_axi_wvalid) begin w_saved<=s_axi_wdata; strobe_saved<=s_axi_wstrb; w_pending<=1; end
            if (s_axi_rvalid && s_axi_rready) s_axi_rvalid<=0;
            if (s_axi_arready && s_axi_arvalid) begin
                s_axi_rvalid<=1; s_axi_rresp<=OKAY;
                if (s_axi_araddr[1:0]!=0) begin
                    s_axi_rdata<=0; s_axi_rresp<=SLVERR; abort_frame(ERR_ALIGNMENT);
                end else case (s_axi_araddr)
                    'h00: s_axi_rdata<={28'd0,fault,!busy,done,busy};
                    'h04: s_axi_rdata<=width;
                    'h08: s_axi_rdata<=height;
                    'h0c: s_axi_rdata<=32'h50494d32;
                    'h10: s_axi_rdata<=input_count;
                    'h14: s_axi_rdata<=output_count;
                    'h18: s_axi_rdata<=last_cycles;
                    'h1c: s_axi_rdata<=completed_count;
                    'h20: s_axi_rdata<=error_code;
                    'h24: s_axi_rdata<=MAX_WIDTH;
                    'h28: s_axi_rdata<=MAX_HEIGHT;
                    default: begin s_axi_rdata<=0; s_axi_rresp<=SLVERR; abort_frame(ERR_REGISTER); end
                endcase
            end
            // Configuration writes take precedence; AW and W arrive independently.
            if (write_commit) begin
                aw_pending<=0; w_pending<=0; s_axi_bvalid<=1; s_axi_bresp<=OKAY;
                if (strobe_saved==0) begin end
                else if (aw_saved[1:0]!=0) begin s_axi_bresp<=SLVERR; abort_frame(ERR_ALIGNMENT); end
                else if (aw_saved=='h00 && strobe_saved[0] && w_saved[2]) begin
                    busy<=0; done<=0; fault<=0; error_code<=0; input_done<=0;
                    pair_pending<=0; m_axis_tvalid<=0; m_axis_tlast<=0;
                    input_count<=0; output_count<=0; cycle_count<=0; last_cycles<=0; x<=0; y<=0;
                end else if (busy) begin s_axi_bresp<=SLVERR; abort_frame(ERR_BUSY); end
                else case (aw_saved)
                    'h00: if (strobe_saved[0] && w_saved[0]) begin
                        if (width<2 || height<2 || width>MAX_WIDTH || height>MAX_HEIGHT || width[0] || height[0]) begin
                            s_axi_bresp<=SLVERR; abort_frame(ERR_DIMENSIONS);
                        end else begin
                            busy<=1; done<=0; fault<=0; error_code<=0; input_done<=0;
                            total_pixels<=configured_pixels; x<=0; y<=0; input_count<=0; output_count<=0; cycle_count<=0;
                            pair_pending<=0; m_axis_tvalid<=0; m_axis_tlast<=0;
                        end
                    end
                    'h04: width<=merge_bytes(width,w_saved,strobe_saved);
                    'h08: height<=merge_bytes(height,w_saved,strobe_saved);
                    default: begin s_axi_bresp<=SLVERR; abort_frame(ERR_REGISTER); end
                endcase
            end
        end
    end
endmodule
