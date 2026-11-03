`timescale 1ns/1ps
// Real PL image arithmetic. Four-byte pixels are little-endian B,G,R,unused.
// A 2x2 box filter downsamples both dimensions by two. All three channels
// are computed together, with one input pixel/clock when the output is ready.
// AXI stream TLAST describes a complete frame, NOT the end of a video line.
module pose_conv_axi #(
    parameter integer LANES = 36,
    parameter integer FEATURES = 768,
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

    localparam IDLE=0, LOAD=1, RUN=2, DRAIN=3, OUTPUT=4;
    localparam WORDS=FEATURES*(LANES+1);
    reg [2:0] state;
    reg busy,done,fault;
    reg [31:0] input_count,cycle_count,last_cycles,completed_count,error_code;
    reg [15:0] load_feature,load_lane,issue_index,output_index;
    reg read_valid,read_last;
    (* ram_style="block" *) reg signed [15:0] features [0:FEATURES-1];
    reg signed [15:0] feature_sample;
    wire signed [47:0] sums [0:LANES-1];
    wire input_accept=s_axis_tvalid && s_axis_tready;
    wire good_input=s_axis_tkeep==15 && s_axis_tlast==(input_count==WORDS-1);
    wire issue=state==RUN && issue_index<FEATURES;
    wire output_slot=!m_axis_tvalid || m_axis_tready;
    assign s_axis_tready=s_axi_aresetn && busy && state==LOAD;
    assign m_axis_tkeep=15;
    always @(posedge s_axi_aclk) begin
        if(s_axi_aresetn) begin
            if(input_accept && good_input && input_count<FEATURES)
                features[load_feature]<=s_axis_tdata[15:0];
            if(issue) feature_sample<=features[issue_index];
        end
    end
    genvar lane;
    generate for(lane=0;lane<LANES;lane=lane+1) begin: mac
        (* ram_style="block" *) reg signed [15:0] weights [0:FEATURES-1];
        reg signed [15:0] weight_sample;
        (* use_dsp="yes" *) reg signed [47:0] accumulator;
        assign sums[lane]=accumulator;
        always @(posedge s_axi_aclk) begin
            if(s_axi_aresetn) begin
                if(input_accept && good_input && input_count>=FEATURES && load_lane==lane)
                    weights[load_feature]<=s_axis_tdata[15:0];
                if(issue) weight_sample<=weights[issue_index];
            end
            if(!s_axi_aresetn || state==IDLE || state==LOAD) accumulator<=0;
            else if(read_valid)
                accumulator<=accumulator+$signed(feature_sample)*$signed(weight_sample);
        end
    end endgenerate
    reg aw_pending,w_pending;
    reg [C_S_AXI_ADDR_WIDTH-1:0] aw_saved;
    reg [31:0] w_saved;
    reg [3:0] strobe_saved;
    wire write_commit=aw_pending && w_pending && !s_axi_bvalid;
    assign s_axi_awready=s_axi_aresetn && !aw_pending && !s_axi_bvalid;
    assign s_axi_wready=s_axi_aresetn && !w_pending && !s_axi_bvalid;
    assign s_axi_arready=s_axi_aresetn && !s_axi_rvalid;
    wire [63:0] selected_sum={{16{sums[output_index>>1][47]}},sums[output_index>>1]};
    task abort_frame;
        input [31:0] code;
        begin busy<=0;done<=0;fault<=1;error_code<=code;state<=IDLE;
            m_axis_tvalid<=0;m_axis_tlast<=0;read_valid<=0;
            last_cycles<=cycle_count+(busy?1:0);
        end
    endtask
    always @(posedge s_axi_aclk) begin
        if(!s_axi_aresetn) begin
            state<=IDLE;busy<=0;done<=0;fault<=0;input_count<=0;
            cycle_count<=0;last_cycles<=0;completed_count<=0;error_code<=0;
            load_feature<=0;load_lane<=0;issue_index<=0;output_index<=0;read_valid<=0;read_last<=0;
            aw_pending<=0;w_pending<=0;aw_saved<=0;w_saved<=0;strobe_saved<=0;
            s_axi_bresp<=0;s_axi_bvalid<=0;s_axi_rdata<=0;s_axi_rresp<=0;s_axi_rvalid<=0;
            m_axis_tdata<=0;m_axis_tvalid<=0;m_axis_tlast<=0;
        end else begin
            if(busy)cycle_count<=cycle_count+1;
            read_valid<=issue;read_last<=issue && issue_index==FEATURES-1;
            if(issue)issue_index<=issue_index+1;
            if(state==RUN && read_valid && read_last)state<=DRAIN;
            if(state==DRAIN)begin state<=OUTPUT;output_index<=0;end
            if(state==OUTPUT && output_slot) begin
                if(output_index<LANES*2)begin
                    m_axis_tvalid<=1;
                    m_axis_tdata<=output_index[0]?selected_sum[63:32]:selected_sum[31:0];
                    m_axis_tlast<=output_index==LANES*2-1;
                    output_index<=output_index+1;
                end else begin m_axis_tvalid<=0;m_axis_tlast<=0;end
            end
            if(m_axis_tvalid && m_axis_tready && m_axis_tlast)begin
                busy<=0;done<=1;state<=IDLE;completed_count<=completed_count+1;last_cycles<=cycle_count+1;
            end
            if(input_accept)begin
                input_count<=input_count+1;
                if(!good_input)abort_frame(s_axis_tkeep!=15?5:(s_axis_tlast?3:4));
                else if(input_count<FEATURES)begin
                    if(load_feature==FEATURES-1)load_feature<=0;else load_feature<=load_feature+1;
                end else begin
                    if(load_lane==LANES-1)begin load_lane<=0;load_feature<=load_feature+1;end
                    else load_lane<=load_lane+1;
                    if(input_count==WORDS-1)begin state<=RUN;issue_index<=0;end
                end
            end
            if(s_axi_bvalid && s_axi_bready)s_axi_bvalid<=0;
            if(s_axi_awready && s_axi_awvalid)begin aw_saved<=s_axi_awaddr;aw_pending<=1;end
            if(s_axi_wready && s_axi_wvalid)begin w_saved<=s_axi_wdata;strobe_saved<=s_axi_wstrb;w_pending<=1;end
            if(s_axi_rvalid && s_axi_rready)s_axi_rvalid<=0;
            if(s_axi_arready && s_axi_arvalid)begin
                s_axi_rvalid<=1;s_axi_rresp<=0;
                if(s_axi_araddr[1:0]!=0)begin s_axi_rdata<=0;s_axi_rresp<=2;abort_frame(7);end
                else case(s_axi_araddr)
                    'h00:s_axi_rdata<={28'd0,fault,!busy,done,busy};
                    'h04:s_axi_rdata<=input_count;
                    'h08:s_axi_rdata<=32'h50434e31;
                    'h0c:s_axi_rdata<=FEATURES;
                    'h10:s_axi_rdata<=LANES;
                    'h14:s_axi_rdata<=last_cycles;
                    'h18:s_axi_rdata<=completed_count;
                    'h1c:s_axi_rdata<=error_code;
                    default:begin s_axi_rdata<=0;s_axi_rresp<=2;abort_frame(6);end
                endcase
            end
            if(write_commit)begin
                aw_pending<=0;w_pending<=0;s_axi_bvalid<=1;s_axi_bresp<=0;
                if(strobe_saved==0)begin end
                else if(aw_saved[1:0]!=0)begin s_axi_bresp<=2;abort_frame(7);end
                else if(busy)begin s_axi_bresp<=2;abort_frame(2);end
                else if(aw_saved==0 && strobe_saved[0] && w_saved[0])begin
                    busy<=1;done<=0;fault<=0;error_code<=0;state<=LOAD;
                    input_count<=0;cycle_count<=0;load_feature<=0;load_lane<=0;issue_index<=0;
                    m_axis_tvalid<=0;m_axis_tlast<=0;
                end else if(aw_saved==0 && strobe_saved[0] && w_saved[2])begin
                    fault<=0;done<=0;error_code<=0;
                end else begin s_axi_bresp<=2;abort_frame(6);end
            end
        end
    end
endmodule
