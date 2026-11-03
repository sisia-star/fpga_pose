`timescale 1ns/1ps
// Parallel-AI v2: independent bank per lane, fully pipelined square accumulation.
// Physical train/query words use PAD_DIMS stride; padded lanes are ignored.
// LANES must be a power of two in 2..32. Public feature dimension remains 99.
// Training/query are signed int16 Q4.11; each result is uint64 sum(diff^2).
// No large RAM array is reset. PS must upload a complete model and query first.
module pose_knn_distance_axi #(
    parameter integer MAX_ROWS = 1024,
    parameter integer DIMS = 99,
    parameter integer LANES = 32,
    parameter integer C_S_AXI_ADDR_WIDTH = 12
) (
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI_CLK, ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET s_axi_aresetn, FREQ_HZ 100000000" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 S_AXI_CLK CLK" *)
    input wire s_axi_aclk,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI_RST, POLARITY ACTIVE_LOW" *)
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 S_AXI_RST RST" *)
    input wire s_axi_aresetn,
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 12, READ_WRITE_MODE READ_WRITE" *)
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR" *)
    input wire [C_S_AXI_ADDR_WIDTH-1:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWPROT" *)
    input wire [2:0] s_axi_awprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *)
    input wire s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *)
    output wire s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WDATA" *)
    input wire [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *)
    input wire [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WVALID" *)
    input wire s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WREADY" *)
    output wire s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BRESP" *)
    output reg [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BVALID" *)
    output reg s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BREADY" *)
    input wire s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *)
    input wire [C_S_AXI_ADDR_WIDTH-1:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARPROT" *)
    input wire [2:0] s_axi_arprot,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *)
    input wire s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *)
    output wire s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RDATA" *)
    output reg [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RRESP" *)
    output reg [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RVALID" *)
    output reg s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RREADY" *)
    input wire s_axi_rready
);
    function integer bits_for;
        input integer count;
        integer value;
        begin
            value = count - 1; bits_for = 0;
            while (value > 0) begin bits_for = bits_for + 1; value = value >> 1; end
            if (bits_for < 1) bits_for = 1;
        end
    endfunction
    function [31:0] merge_bytes;
        input [31:0] previous, incoming;
        input [3:0] enables;
        integer byte_index;
        begin
            merge_bytes = previous;
            for (byte_index=0; byte_index<4; byte_index=byte_index+1)
                if (enables[byte_index]) merge_bytes[byte_index*8 +: 8] = incoming[byte_index*8 +: 8];
        end
    endfunction

    localparam integer LEVELS = bits_for(LANES);
    localparam integer GROUPS = (DIMS + LANES - 1) / LANES;
    localparam integer PAD_DIMS = GROUPS * LANES;
    localparam integer TRAIN_WORDS = MAX_ROWS * PAD_DIMS;
    localparam integer TRAIN_BITS = bits_for(TRAIN_WORDS);
    localparam integer BANK_BITS = bits_for(MAX_ROWS * GROUPS);
    localparam integer ROW_BITS = bits_for(MAX_ROWS);
    localparam integer DIM_BITS = bits_for(PAD_DIMS);
    localparam [2:0] IDLE=0, STREAM=1, REDUCE=2, STORE=3, FINISH=4;
    localparam [1:0] OKAY=2'b00, SLVERR=2'b10;
    localparam [31:0] ERR_BUSY=1, ERR_ROWS=2, ERR_TRAIN=3,
        ERR_QUERY=4, ERR_DIST=5, ERR_REGISTER=6, ERR_ALIGNMENT=7;

    (* ram_style = "block" *) reg [63:0] distance_ram [0:MAX_ROWS-1];
    reg [63:0] distance_read_data;
    reg train_we, query_we, distance_we, distance_re;
    reg [TRAIN_BITS-1:0] train_write_addr;
    reg [DIM_BITS-1:0] query_write_addr;
    reg [ROW_BITS-1:0] distance_write_addr, distance_read_addr;
    reg [15:0] memory_write_data;
    reg [1:0] memory_write_strobes;
    reg [63:0] distance_write_data;
    reg busy, done, fault;
    reg [31:0] row_count, train_addr, query_addr, dist_addr;
    reg [31:0] completed_count, last_cycles, error_code, cycle_count;
    reg [2:0] state;
    reg [BANK_BITS-1:0] core_word_addr;
    reg [ROW_BITS-1:0] core_row;
    reg [7:0] issued_groups, reduce_count;
    reg read_valid, diff_valid, square_valid;
    reg read_last, diff_last, square_last;
    wire issue_read = busy && state == STREAM && issued_groups < GROUPS;
    wire [39:0] partial_sum [0:LANES-1];
    reg [39:0] reduce_tree [1:LANES-1];
    wire [63:0] row_distance = {24'b0, reduce_tree[1]};
    reg [63:0] top_distance [0:4];
    reg [31:0] top_row [0:4];
    reg [2:0] top_count, top_selector;
    integer insert_position, candidate, slot;

    // Small, ordered insertion network: strict '<' preserves lower-row tie order.
    always @* begin
        insert_position = 5;
        for (candidate=0; candidate<5; candidate=candidate+1)
            if (insert_position == 5 && row_distance < top_distance[candidate])
                insert_position = candidate;
    end

    genvar lane, node;
    generate for (lane=0; lane<LANES; lane=lane+1) begin: lanes
        (* ram_style = "block" *) reg [15:0] training_bank [0:MAX_ROWS*GROUPS-1];
        (* ram_style = "distributed" *) reg [15:0] query_bank [0:GROUPS-1];
        reg [15:0] training_sample, query_sample;
        reg sample_in_range;
        reg signed [16:0] delta;
        (* use_dsp = "yes" *) reg [33:0] square;
        reg [39:0] accumulator;
        assign partial_sum[lane] = accumulator;
        always @(posedge s_axi_aclk) begin
            if (s_axi_aresetn) begin
                if (train_we && train_write_addr % LANES == lane) begin
                    if (memory_write_strobes[0]) training_bank[train_write_addr / LANES][7:0] <= memory_write_data[7:0];
                    if (memory_write_strobes[1]) training_bank[train_write_addr / LANES][15:8] <= memory_write_data[15:8];
                end
                if (query_we && query_write_addr % LANES == lane) begin
                    if (memory_write_strobes[0]) query_bank[query_write_addr / LANES][7:0] <= memory_write_data[7:0];
                    if (memory_write_strobes[1]) query_bank[query_write_addr / LANES][15:8] <= memory_write_data[15:8];
                end
                if (issue_read) begin
                    training_sample <= training_bank[core_word_addr];
                    query_sample <= query_bank[issued_groups];
                    sample_in_range <= issued_groups*LANES + lane < DIMS;
                end
            end
            if (!s_axi_aresetn) begin
                accumulator <= 0; delta <= 0; square <= 0;
            end else begin
                if (state == IDLE || state == STORE) accumulator <= 0;
                else if (square_valid) accumulator <= accumulator + {6'b0,square};
                if (read_valid)
                    delta <= sample_in_range ?
                        $signed({query_sample[15],query_sample}) - $signed({training_sample[15],training_sample}) : 17'sd0;
                if (diff_valid) square <= delta * delta;
            end
        end
    end endgenerate

    // Registered balanced adder tree. Results settle in LEVELS clocks.
    // 40 bits cover 99*(65535^2), including signed16 extreme inputs.
    generate for (node=1; node<LANES; node=node+1) begin: reduction
        if (node >= LANES/2) begin: leaf
            always @(posedge s_axi_aclk)
                reduce_tree[node] <= partial_sum[(node-LANES/2)*2] + partial_sum[(node-LANES/2)*2+1];
        end else begin: branch
            always @(posedge s_axi_aclk)
                reduce_tree[node] <= reduce_tree[node*2] + reduce_tree[node*2+1];
        end
    end endgenerate
    // Write-address and write-data channels are accepted independently.
    reg aw_pending, w_pending;
    reg [C_S_AXI_ADDR_WIDTH-1:0] aw_saved;
    reg [31:0] w_saved;
    reg [3:0] strobe_saved;
    wire write_commit = aw_pending && w_pending && !s_axi_bvalid;
    wire busy_rejected_write = write_commit && busy && (|strobe_saved);
    assign s_axi_awready = s_axi_aresetn && !aw_pending && !s_axi_bvalid;
    assign s_axi_wready = s_axi_aresetn && !w_pending && !s_axi_bvalid;

    // RAM read response has two wait stages. A response is held until RREADY.
    reg [1:0] read_wait;
    reg read_high, read_advances;
    reg [31:0] read_dist_index;
    assign s_axi_arready = s_axi_aresetn && !s_axi_rvalid && (read_wait == 0);


    // Output RAM preserves the original LO/HI handshake protocol for diagnostics.
    always @(posedge s_axi_aclk) begin
        if (s_axi_aresetn) begin
            if (distance_we) distance_ram[distance_write_addr] <= distance_write_data;
            if (distance_re) distance_read_data <= distance_ram[distance_read_addr];
        end
    end
    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            busy<=0; done<=0; fault<=0; row_count<=0; train_addr<=0; query_addr<=0; dist_addr<=0;
            completed_count<=0; last_cycles<=0; error_code<=0; cycle_count<=0;
            state<=IDLE; core_word_addr<=0; core_row<=0; issued_groups<=0; reduce_count<=0;
            read_valid<=0; diff_valid<=0; square_valid<=0;
            read_last<=0; diff_last<=0; square_last<=0; top_count<=0; top_selector<=0;
            for (slot=0;slot<5;slot=slot+1) begin top_distance[slot]<=64'hffffffffffffffff; top_row[slot]<=32'hffffffff; end
            train_we<=0; query_we<=0; distance_we<=0; distance_re<=0;
            train_write_addr<=0; query_write_addr<=0; distance_write_addr<=0; distance_read_addr<=0;
            memory_write_data<=0; memory_write_strobes<=0; distance_write_data<=0;
            aw_pending<=0; w_pending<=0; aw_saved<=0; w_saved<=0; strobe_saved<=0;
            s_axi_bresp<=OKAY; s_axi_bvalid<=0;
            read_wait<=0; read_high<=0; read_advances<=0; read_dist_index<=0;
            s_axi_rdata<=0; s_axi_rresp<=OKAY; s_axi_rvalid<=0;
        end else begin
            train_we<=0; query_we<=0; distance_we<=0; distance_re<=0;
            if (busy) cycle_count <= cycle_count + 1;


            read_valid <= issue_read; diff_valid <= read_valid; square_valid <= diff_valid;
            read_last <= issue_read && issued_groups == GROUPS-1;
            diff_last <= read_last; square_last <= diff_last;
            case (state)
                STREAM: begin
                    if (issue_read) begin
                        issued_groups <= issued_groups + 1;
                        core_word_addr <= core_word_addr + 1;
                    end
                    if (square_valid && square_last) begin state<=REDUCE; reduce_count<=0; end
                end
                REDUCE: begin
                    if (reduce_count == LEVELS-1) state<=STORE;
                    else reduce_count <= reduce_count + 1;
                end
                STORE: begin
                    distance_we<=1; distance_write_addr<=core_row; distance_write_data<=row_distance;
                    for (slot=0;slot<5;slot=slot+1) begin
                        if (slot == insert_position) begin top_distance[slot]<=row_distance; top_row[slot]<=core_row; end
                        else if (slot > insert_position) begin top_distance[slot]<=top_distance[slot-1]; top_row[slot]<=top_row[slot-1]; end
                    end
                    if (top_count < 5) top_count <= top_count + 1;
                    if (core_row + 32'd1 >= row_count) state<=FINISH;
                    else begin core_row<=core_row+1; issued_groups<=0; state<=STREAM; end
                end
                FINISH: begin
                    busy<=0; done<=1; state<=IDLE; last_cycles<=cycle_count+1;
                    if (!fault && !busy_rejected_write) completed_count<=completed_count+1;
                end
                default: state<=IDLE;
            endcase

            if (s_axi_bvalid && s_axi_bready) s_axi_bvalid<=0;
            if (s_axi_awready && s_axi_awvalid) begin aw_saved<=s_axi_awaddr; aw_pending<=1; end
            if (s_axi_wready && s_axi_wvalid) begin
                w_saved<=s_axi_wdata; strobe_saved<=s_axi_wstrb; w_pending<=1;
            end

            if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid<=0;
                // HI increments exactly once at successful read handshake.
                if (read_advances && dist_addr == read_dist_index) dist_addr<=read_dist_index+1;
                read_advances<=0;
            end
            if (read_wait == 1) read_wait<=2;
            else if (read_wait == 2) begin
                s_axi_rdata <= read_high ? distance_read_data[63:32] : distance_read_data[31:0];
                s_axi_rresp<=OKAY; s_axi_rvalid<=1; read_wait<=0; read_advances<=read_high;
            end
            if (s_axi_arready && s_axi_arvalid) begin
                read_advances<=0; s_axi_rresp<=OKAY;
                if (s_axi_araddr[1:0] != 0) begin
                    s_axi_rdata<=0; s_axi_rresp<=SLVERR; s_axi_rvalid<=1;
                    fault<=1; error_code<=ERR_ALIGNMENT;
                end else begin
                    case (s_axi_araddr)
                        'h00: begin s_axi_rdata<={28'd0,fault,!busy,done,busy}; s_axi_rvalid<=1; end
                        'h04: begin s_axi_rdata<=row_count; s_axi_rvalid<=1; end
                        'h08: begin s_axi_rdata<=train_addr; s_axi_rvalid<=1; end
                        'h10: begin s_axi_rdata<=query_addr; s_axi_rvalid<=1; end
                        'h18: begin s_axi_rdata<=dist_addr; s_axi_rvalid<=1; end
                        'h1c, 'h20: begin
                            if (dist_addr >= MAX_ROWS) begin
                                s_axi_rdata<=0; s_axi_rresp<=SLVERR; s_axi_rvalid<=1;
                                fault<=1; error_code<=ERR_DIST;
                            end else begin
                                distance_re<=1; distance_read_addr<=dist_addr[ROW_BITS-1:0];
                                read_dist_index<=dist_addr; read_high<=(s_axi_araddr == 'h20); read_wait<=1;
                            end
                        end
                        'h24: begin s_axi_rdata<=32'h504b5032; s_axi_rvalid<=1; end
                        'h28: begin s_axi_rdata<=MAX_ROWS; s_axi_rvalid<=1; end
                        'h2c: begin s_axi_rdata<=completed_count; s_axi_rvalid<=1; end
                        'h30: begin s_axi_rdata<=last_cycles; s_axi_rvalid<=1; end
                        'h34: begin s_axi_rdata<=error_code; s_axi_rvalid<=1; end
                        'h38: begin s_axi_rdata<=DIMS; s_axi_rvalid<=1; end
                        'h3c: begin s_axi_rdata<=32'd2048; s_axi_rvalid<=1; end

                        'h40: begin s_axi_rdata<=LANES; s_axi_rvalid<=1; end
                        'h44: begin s_axi_rdata<=top_count; s_axi_rvalid<=1; end
                        'h48: begin s_axi_rdata<=top_selector; s_axi_rvalid<=1; end
                        'h4c: begin s_axi_rdata<=top_row[top_selector]; s_axi_rvalid<=1; end
                        'h50: begin s_axi_rdata<=top_distance[top_selector][31:0]; s_axi_rvalid<=1; end
                        'h54: begin s_axi_rdata<=top_distance[top_selector][63:32]; s_axi_rvalid<=1; end
                        'h58: begin s_axi_rdata<=PAD_DIMS; s_axi_rvalid<=1; end
                        'h5c: begin s_axi_rdata<=2; s_axi_rvalid<=1; end
                        default: begin
                            s_axi_rdata<=0; s_axi_rresp<=SLVERR; s_axi_rvalid<=1;
                            fault<=1; error_code<=ERR_REGISTER;
                        end
                    endcase
                end
            end

            // Later assignments give an explicit CPU write precedence over pointer auto-increment.
            if (write_commit) begin
                aw_pending<=0; w_pending<=0; s_axi_bvalid<=1; s_axi_bresp<=OKAY;
                if (strobe_saved == 0) begin end // AXI zero-byte write is a no-op.
                else if (busy) begin s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_BUSY; end
                else if (aw_saved[1:0] != 0) begin s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_ALIGNMENT; end
                else begin
                    case (aw_saved)
                        'h00: if (strobe_saved[0] && w_saved[0]) begin
                            done<=0;
                            if (row_count == 0 || row_count > MAX_ROWS) begin
                                fault<=1; error_code<=ERR_ROWS; s_axi_bresp<=SLVERR;
                            end else begin
                                busy<=1; fault<=0; error_code<=0; state<=STREAM;
                                core_row<=0; issued_groups<=0; core_word_addr<=0; cycle_count<=0; top_count<=0;
                                for (slot=0;slot<5;slot=slot+1) begin top_distance[slot]<=64'hffffffffffffffff; top_row[slot]<=32'hffffffff; end
                            end
                        end
                        'h04: row_count<=merge_bytes(row_count,w_saved,strobe_saved);
                        'h08: begin
                            if (merge_bytes(train_addr,w_saved,strobe_saved) >= TRAIN_WORDS) begin
                                s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_TRAIN;
                            end else train_addr<=merge_bytes(train_addr,w_saved,strobe_saved);
                        end
                        'h0c: if (|strobe_saved[1:0]) begin
                            if (train_addr >= TRAIN_WORDS) begin s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_TRAIN; end
                            else begin
                                train_we<=1; train_write_addr<=train_addr[TRAIN_BITS-1:0];
                                memory_write_data<=w_saved[15:0]; memory_write_strobes<=strobe_saved[1:0]; train_addr<=train_addr+1;
                            end
                        end
                        'h10: begin
                            if (merge_bytes(query_addr,w_saved,strobe_saved) >= PAD_DIMS) begin s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_QUERY; end
                            else query_addr<=merge_bytes(query_addr,w_saved,strobe_saved);
                        end
                        'h14: if (|strobe_saved[1:0]) begin
                            if (query_addr >= PAD_DIMS) begin s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_QUERY; end
                            else begin
                                query_we<=1; query_write_addr<=query_addr[DIM_BITS-1:0];
                                memory_write_data<=w_saved[15:0]; memory_write_strobes<=strobe_saved[1:0]; query_addr<=query_addr+1;
                            end
                        end

                        'h48: begin
                            if (merge_bytes({29'b0,top_selector},w_saved,strobe_saved) >= 5) begin
                                s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_DIST;
                            end else if (strobe_saved[0]) top_selector<=w_saved[2:0];
                        end
                        'h18: begin
                            if (merge_bytes(dist_addr,w_saved,strobe_saved) >= MAX_ROWS) begin s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_DIST; end
                            else dist_addr<=merge_bytes(dist_addr,w_saved,strobe_saved);
                        end
                        default: begin s_axi_bresp<=SLVERR; fault<=1; error_code<=ERR_REGISTER; end
                    endcase
                end
            end
        end
    end
endmodule
