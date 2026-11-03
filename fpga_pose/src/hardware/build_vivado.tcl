# Default path: Vivado 2022.x ONLY, no Vitis HLS dependency.
# Run: vivado -mode batch -source hardware/build_vivado.tcl
# Target syntax: Vivado 2022.2. Requires official TUL PYNQ-Z2 board files.
set script_dir [file dirname [file normalize [info script]]]
set package_dir [file dirname $script_dir]
set build_dir [file join $package_dir build]
if {[info exists ::env(POSE_BUILD_DIR)] && $::env(POSE_BUILD_DIR) ne ""} {
    set build_dir [file normalize $::env(POSE_BUILD_DIR)]
}
set tool_version [version -short]
if {![string match "2022.*" $tool_version]} {
    error "This reference script targets Vivado 2022.x; found $tool_version. Review/adapt for this release."
}
if {[info exists ::env(POSE_BOARD_REPO)] && $::env(POSE_BOARD_REPO) ne ""} {
    set repo [file normalize $::env(POSE_BOARD_REPO)]
    if {![file isdirectory $repo]} {error "POSE_BOARD_REPO is not a directory: $repo"}
    set repos [get_param board.repoPaths]
    lappend repos $repo
    set_param board.repoPaths $repos
}
set board_part {tul.com.tw:pynq-z2:part0:1.0}
if {[llength [get_board_parts -quiet $board_part]] != 1} {
    error "Official $board_part not found. Install TUL pynq-z2/A.0 board files or set POSE_BOARD_REPO to their parent directory. No guessed DDR fallback is used."
}
set jobs 4
if {[info exists ::env(POSE_JOBS)]} {
    if {![string is integer -strict $::env(POSE_JOBS)] || $::env(POSE_JOBS)<1 || $::env(POSE_JOBS)>32} {
        error "POSE_JOBS must be an integer from 1 to 32"
    }
    set jobs $::env(POSE_JOBS)
}
set run_sim 0
if {[info exists ::env(POSE_RUN_RTL_SIM)]} {
    if {$::env(POSE_RUN_RTL_SIM) ni {0 1}} {error "POSE_RUN_RTL_SIM must be 0 or 1"}
    set run_sim $::env(POSE_RUN_RTL_SIM)
}
set project_dir [file join $build_dir vivado_pose]
if {[file exists [file join $project_dir pose_distance.xpr]]} {
    error "Project exists at $project_dir. Choose a new POSE_BUILD_DIR or preserve edits before removing this generated project."
}
file mkdir $build_dir

# Test the core in its own project, with both DUT and testbench explicitly present.
# A packaged BD IP does not guarantee that its original module is visible to a
# testbench that directly instantiates pose_knn_distance_axi (Vivado 2022.2).
if {$run_sim} {
    source [file join $script_dir run_rtl_sim.tcl]
    pose_run_rtl_sim $script_dir $build_dir
    pose_run_image_rtl_sim $script_dir $build_dir
    pose_run_conv_rtl_sim $script_dir $build_dir
}

# Package the actual Verilog source, so PYNQ sees a stable VLNV and register map.
# A module_reference block is intentionally not used.
set ip_repo [file join $build_dir rtl_ip]
create_project pose_knn_pack [file join $build_dir ip_pack_project] -part xc7z020clg400-1 -force
set_property target_language Verilog [current_project]
add_files -norecurse [file join $script_dir pose_knn_distance_axi.v]
set_property top pose_knn_distance_axi [get_filesets sources_1]
update_compile_order -fileset sources_1
ipx::package_project -root_dir [file join $ip_repo distance] -vendor pose.local -library pose \
    -taxonomy /UserIP -import_files -set_current true
set core [ipx::current_core]
set_property name pose_knn_distance_axi $core
set_property version 1.0 $core
set_property display_name {Pose parallel squared distances and stable top5} $core
set_property description {99-dimensional signed16 Q4.11 distances; actual PL arithmetic and completion counter} $core
set_property supported_families {zynq Production} $core
set bus [ipx::get_bus_interfaces S_AXI -of_objects $core]
if {[llength $bus] != 1} {error "AXI interface inference failed: expected S_AXI from RTL attributes"}
set_property interface_mode slave $bus
foreach {name value} {PROTOCOL AXI4LITE DATA_WIDTH 32 ADDR_WIDTH 12} {
    set parameter [ipx::get_bus_parameters $name -of_objects $bus]
    if {[llength $parameter]==0} {set parameter [ipx::add_bus_parameter $name $bus]}
    set_property value $value $parameter
}
set map [ipx::get_memory_maps S_AXI -of_objects $core]
if {[llength $map]==0} {set map [ipx::add_memory_map S_AXI $core]}
set_property slave_memory_map_ref S_AXI $bus
set existing_blocks [ipx::get_address_blocks -of_objects $map]
if {[llength $existing_blocks] == 0} {
    set block [ipx::add_address_block registers $map]
} elseif {[llength $existing_blocks] == 1} {
    set block [lindex $existing_blocks 0]
} else {error "Unexpected overlapping/inferred address blocks; review packaged S_AXI memory map"}
set_property name registers $block
set_property base_address 0 $block
set_property range 4096 $block
set_property width 32 $block
set_property usage register $block
foreach {name offset access description} {
    CTRL             0x00 read-write {Write bit0 starts; read busy/done/idle/fault in bits0/1/2/3}
    ROW_COUNT        0x04 read-write {Number of training rows, 1..1024}
    TRAIN_ADDR       0x08 read-write {Training word index; auto-increments on low16 write}
    TRAIN_DATA       0x0c write-only {Signed16 training feature in low16 bits}
    QUERY_ADDR       0x10 read-write {Query word index; auto-increments on low16 write}
    QUERY_DATA       0x14 write-only {Signed16 query feature in low16 bits}
    DIST_ADDR        0x18 read-write {Distance row index}
    DIST_LO          0x1c read-only  {Result bits31..0; does not increment index}
    DIST_HI          0x20 read-only  {Result bits63..32; successful read handshake increments index once}
    ID               0x24 read-only  {RTL identity 0x504b5032}
    MAX_ROWS         0x28 read-only  {Maximum row count 1024}
    COMPLETED_COUNT  0x2c read-only  {Actual successful PL calculation count, wraps uint32}
    LAST_CYCLES      0x30 read-only  {Clock cycles of most recently completed calculation}
    ERROR            0x34 read-only  {Latched error code; cleared by valid start}
    DIMS             0x38 read-only  {Feature dimension 99}
    SCALE            0x3c read-only  {Quantization scale 2048}
    PARALLEL_LANES   0x40 read-only  {Parallel compute lanes}
    TOP_COUNT        0x44 read-only  {Valid ordered nearest neighbors, up to 5}
    TOP_SELECTOR     0x48 read-write {Readback rank 0..4}
    TOP_ROW          0x4c read-only  {Selected nearest row index}
    TOP_LO           0x50 read-only  {Selected squared distance low word}
    TOP_HI           0x54 read-only  {Selected squared distance high word}
    PAD_DIMS         0x58 read-only  {Physical padded train/query row stride}
    PROTOCOL_VERSION 0x5c read-only  {Parallel protocol version 2}
} {
    set register [ipx::add_register $name $block]
    set_property address_offset $offset $register
    set_property size 32 $register
    set_property access $access $register
    set_property description $description $register
}
ipx::create_xgui_files $core
ipx::update_checksums $core
ipx::check_integrity -quiet $core
ipx::save_core $core
close_project

# A second packaged RTL core processes real camera pixels on AXI streams.
create_project pose_image_pack [file join $build_dir image_pack_project] -part xc7z020clg400-1 -force
set_property target_language Verilog [current_project]
add_files -norecurse [file join $script_dir image_preprocess_axi.v]
set_property top image_preprocess_axi [get_filesets sources_1]
update_compile_order -fileset sources_1
ipx::package_project -root_dir [file join $ip_repo image] -vendor pose.local -library pose \
    -taxonomy /UserIP -import_files -set_current true
set core [ipx::current_core]
set_property name pose_image_preprocess_axi $core
set_property version 1.0 $core
set_property display_name {Pose BGRX 2x2 box downsample} $core
set_property description {Real PL BGR pixel averaging; one input pixel per clock when unstalled; frame TLAST and completion counters} $core
set_property supported_families {zynq Production} $core
set bus [ipx::get_bus_interfaces S_AXI -of_objects $core]
if {[llength $bus] != 1} {error "Image AXI interface inference failed: expected S_AXI"}
set_property interface_mode slave $bus
foreach {name value} {PROTOCOL AXI4LITE DATA_WIDTH 32 ADDR_WIDTH 12} {
    set parameter [ipx::get_bus_parameters $name -of_objects $bus]
    if {[llength $parameter]==0} {set parameter [ipx::add_bus_parameter $name $bus]}
    set_property value $value $parameter
}
foreach {axis_name axis_mode} {S_AXIS slave M_AXIS master} {
    set axis [ipx::get_bus_interfaces $axis_name -of_objects $core]
    if {[llength $axis] != 1} {error "Image streaming interface inference failed: $axis_name"}
    set_property interface_mode $axis_mode $axis
    foreach {name value} {TDATA_NUM_BYTES 4 HAS_TKEEP 1 HAS_TLAST 1 HAS_TREADY 1} {
        set parameter [ipx::get_bus_parameters $name -of_objects $axis]
        if {[llength $parameter]==0} {set parameter [ipx::add_bus_parameter $name $axis]}
        set_property value $value $parameter
    }
}
set map [ipx::get_memory_maps S_AXI -of_objects $core]
if {[llength $map]==0} {set map [ipx::add_memory_map S_AXI $core]}
set_property slave_memory_map_ref S_AXI $bus
set existing_blocks [ipx::get_address_blocks -of_objects $map]
if {[llength $existing_blocks] == 0} {set block [ipx::add_address_block registers $map]} \
elseif {[llength $existing_blocks] == 1} {set block [lindex $existing_blocks 0]} \
else {error "Unexpected image AXI memory map"}
set_property name registers $block
set_property base_address 0 $block
set_property range 4096 $block
set_property width 32 $block
set_property usage register $block
foreach {name offset access description} {
    CTRL            0x00 read-write {Write 1 starts; write 4 aborts and clears; read busy0/done1/idle2/fault3}
    WIDTH           0x04 read-write {Input width in pixels; even 2..1280}
    HEIGHT          0x08 read-write {Input height in pixels; even 2..720}
    ID              0x0c read-only {RTL identity 0x50494d32}
    INPUT_COUNT     0x10 read-only {Accepted input pixels in this frame}
    OUTPUT_COUNT    0x14 read-only {Accepted output pixels in this frame}
    LAST_CYCLES     0x18 read-only {Elapsed FCLK cycles through final output transfer; includes stream stalls}
    COMPLETED_COUNT 0x1c read-only {Successful frame TLAST transfer count; wraps uint32}
    ERROR           0x20 read-only {Latched error; clears by clear or valid start}
    MAX_WIDTH       0x24 read-only {Maximum input width 1280}
    MAX_HEIGHT      0x28 read-only {Maximum input height 720}
} {
    set register [ipx::add_register $name $block]
    set_property address_offset $offset $register
    set_property size 32 $register
    set_property access $access $register
    set_property description $description $register
}
ipx::create_xgui_files $core
ipx::update_checksums $core
ipx::check_integrity -quiet $core
ipx::save_core $core
close_project

# A second packaged RTL core processes real camera pixels on AXI streams.
create_project pose_conv_pack [file join $build_dir conv_pack_project] -part xc7z020clg400-1 -force
set_property target_language Verilog [current_project]
add_files -norecurse [file join $script_dir pose_conv_axi.v]
set_property top pose_conv_axi [get_filesets sources_1]
update_compile_order -fileset sources_1
ipx::package_project -root_dir [file join $ip_repo conv] -vendor pose.local -library pose \
    -taxonomy /UserIP -import_files -set_current true
set core [ipx::current_core]
set_property name pose_conv_axi $core
set_property version 1.0 $core
set_property display_name {MediaPipe trained Conv2D head 36-lane MAC} $core
set_property description {36 signed16 MAC lanes with BRAM weights; signed48 accumulators; AXI DMA packets and completion evidence} $core
set_property supported_families {zynq Production} $core
set bus [ipx::get_bus_interfaces S_AXI -of_objects $core]
if {[llength $bus] != 1} {error "Image AXI interface inference failed: expected S_AXI"}
set_property interface_mode slave $bus
foreach {name value} {PROTOCOL AXI4LITE DATA_WIDTH 32 ADDR_WIDTH 12} {
    set parameter [ipx::get_bus_parameters $name -of_objects $bus]
    if {[llength $parameter]==0} {set parameter [ipx::add_bus_parameter $name $bus]}
    set_property value $value $parameter
}
foreach {axis_name axis_mode} {S_AXIS slave M_AXIS master} {
    set axis [ipx::get_bus_interfaces $axis_name -of_objects $core]
    if {[llength $axis] != 1} {error "Image streaming interface inference failed: $axis_name"}
    set_property interface_mode $axis_mode $axis
    foreach {name value} {TDATA_NUM_BYTES 4 HAS_TKEEP 1 HAS_TLAST 1 HAS_TREADY 1} {
        set parameter [ipx::get_bus_parameters $name -of_objects $axis]
        if {[llength $parameter]==0} {set parameter [ipx::add_bus_parameter $name $axis]}
        set_property value $value $parameter
    }
}
set map [ipx::get_memory_maps S_AXI -of_objects $core]
if {[llength $map]==0} {set map [ipx::add_memory_map S_AXI $core]}
set_property slave_memory_map_ref S_AXI $bus
set existing_blocks [ipx::get_address_blocks -of_objects $map]
if {[llength $existing_blocks] == 0} {set block [ipx::add_address_block registers $map]} \
elseif {[llength $existing_blocks] == 1} {set block [lindex $existing_blocks 0]} \
else {error "Unexpected image AXI memory map"}
set_property name registers $block
set_property base_address 0 $block
set_property range 4096 $block
set_property width 32 $block
set_property usage register $block
foreach {name offset access description} {
 CTRL 0x00 read-write {Start 1; busy/done/idle/fault}
 INPUT_COUNT 0x04 read-only {Accepted 32-bit input words}
 ID 0x08 read-only {Convolution RTL 0x50434e31}
 FEATURES 0x0c read-only {768 feature inputs}
 LANES 0x10 read-only {36 output MAC lanes}
 LAST_CYCLES 0x14 read-only {Hardware cycles including stream stalls}
 COMPLETED 0x18 read-only {Actual output TLAST completion count}
 ERROR 0x1c read-only {Latched input or register fault}
} {
    set register [ipx::add_register $name $block]
    set_property address_offset $offset $register
    set_property size 32 $register
    set_property access $access $register
    set_property description $description $register
}
ipx::create_xgui_files $core
ipx::update_checksums $core
ipx::check_integrity -quiet $core
ipx::save_core $core
close_project

create_project pose_distance $project_dir -part xc7z020clg400-1
set_property board_part $board_part [current_project]
if {[get_property PART [current_project]] ne "xc7z020clg400-1"} {error "Board part does not match PYNQ-Z2 FPGA"}
set_property target_language Verilog [current_project]
set_property ip_repo_paths [list $ip_repo] [current_project]
update_ip_catalog
set kernel_vlnv {pose.local:pose:pose_knn_distance_axi:1.0}
set image_vlnv {pose.local:pose:pose_image_preprocess_axi:1.0}
if {[llength [get_ipdefs -quiet $image_vlnv]] != 1} {error "Packaged image RTL IP not found: $image_vlnv"}
if {[llength [get_ipdefs -quiet $kernel_vlnv]] != 1} {error "Packaged RTL IP not found: $kernel_vlnv"}

create_bd_design pose_system
create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0
apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
    -config {make_external "FIXED_IO, DDR" apply_board_preset "1" Master "Disable" Slave "Disable"} \
    [get_bd_cells processing_system7_0]
set ps [get_bd_cells processing_system7_0]
# Cross-checked against TUL pynq-z2/A.0/preset.xml. Keep complete DDR/MIO preset.
foreach {property expected} {
    CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH {16 Bit}
    CONFIG.PCW_UIPARAM_DDR_MEMORY_TYPE {DDR 3}
    CONFIG.PCW_UIPARAM_DDR_PARTNO {MT41J256M16 RE-125}
} {
    set actual [get_property $property $ps]
    if {$actual ne $expected} {error "PYNQ-Z2 preset mismatch: $property=$actual, expected $expected"}
}
foreach {property expected} {
    CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ 525
    CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ 50
    CONFIG.PCW_DDR_RAM_HIGHADDR 0x1FFFFFFF
} {
    set actual [get_property $property $ps]
    if {![string is double -strict $actual] && ![string is wideinteger -strict $actual]} {
        error "Non-numeric board preset property: $property=$actual"
    }
    if {[expr {double($actual)}] != [expr {double($expected)}]} {error "PYNQ-Z2 preset mismatch: $property=$actual, expected $expected"}
}
set_property -dict [list CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_S_AXI_HP0 {1} CONFIG.PCW_S_AXI_HP0_DATA_WIDTH {64} CONFIG.PCW_EN_CLK0_PORT {1} \
    CONFIG.PCW_EN_RST0_PORT {1} CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100}] $ps
# PCW_M_AXI_GP0_FREQMHZ is derived/read-only in this PS IP. The official
# PYNQ-Z2 preset contains 10 MHz legacy metadata; do not try to write it.
create_bd_cell -type ip -vlnv $kernel_vlnv pose_distance_0
set_property -dict [list CONFIG.MAX_ROWS {1024} CONFIG.DIMS {99} CONFIG.C_S_AXI_ADDR_WIDTH {12}] [get_bd_cells pose_distance_0]
create_bd_cell -type ip -vlnv $image_vlnv image_preprocess_0
set_property -dict [list CONFIG.MAX_WIDTH {1280} CONFIG.MAX_HEIGHT {720} CONFIG.C_S_AXI_ADDR_WIDTH {12}] [get_bd_cells image_preprocess_0]
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 image_dma_0
set_property -dict [list CONFIG.c_include_sg {0} CONFIG.c_micro_dma {0} \
    CONFIG.c_include_mm2s {1} CONFIG.c_include_s2mm {1} CONFIG.c_sg_length_width {23} \
    CONFIG.c_m_axi_mm2s_data_width {64} CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_m_axis_mm2s_tdata_width {32} \
    CONFIG.c_include_mm2s_dre {0} CONFIG.c_include_s2mm_dre {0} \
    CONFIG.c_mm2s_burst_size {16} CONFIG.c_s2mm_burst_size {16}] [get_bd_cells image_dma_0]
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_control
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {3}] [get_bd_cells axi_control]
connect_bd_intf_net [get_bd_intf_pins processing_system7_0/M_AXI_GP0] [get_bd_intf_pins axi_control/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_control/M00_AXI] [get_bd_intf_pins pose_distance_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_control/M01_AXI] [get_bd_intf_pins image_preprocess_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_control/M02_AXI] [get_bd_intf_pins image_dma_0/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins image_dma_0/M_AXIS_MM2S] [get_bd_intf_pins image_preprocess_0/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins image_preprocess_0/M_AXIS] [get_bd_intf_pins image_dma_0/S_AXIS_S2MM]
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_memory
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}] [get_bd_cells axi_memory]
connect_bd_intf_net [get_bd_intf_pins image_dma_0/M_AXI_MM2S] [get_bd_intf_pins axi_memory/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins image_dma_0/M_AXI_S2MM] [get_bd_intf_pins axi_memory/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_memory/M00_AXI] [get_bd_intf_pins processing_system7_0/S_AXI_HP0]

create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_fclk0
# Vivado 2022.2 manages C_EXT_RESET_HIGH through the connected reset signal;
# it is read-only here. Connect PS FCLK_RESET0_N, then verify propagation below.
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_one
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] [get_bd_cells const_one]
create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_zero
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] [get_bd_cells const_zero]
# Both reset inputs are active-low in the generated Vivado 2022.2 design.
# An unused aux_reset_in must therefore be HIGH, not tied to const_zero.
# Keep the debug reset (always active-high) LOW and dcm_locked HIGH.
connect_bd_net [get_bd_pins const_one/dout] [get_bd_pins rst_fclk0/dcm_locked] [get_bd_pins rst_fclk0/aux_reset_in]
connect_bd_net [get_bd_pins const_zero/dout] [get_bd_pins rst_fclk0/mb_debug_sys_rst]
connect_bd_net [get_bd_pins processing_system7_0/FCLK_RESET0_N] [get_bd_pins rst_fclk0/ext_reset_in]
set pose_clock_pins {
    processing_system7_0/M_AXI_GP0_ACLK processing_system7_0/S_AXI_HP0_ACLK
    pose_distance_0/s_axi_aclk image_preprocess_0/s_axi_aclk rst_fclk0/slowest_sync_clk
    image_dma_0/s_axi_lite_aclk image_dma_0/m_axi_mm2s_aclk image_dma_0/m_axi_s2mm_aclk
    axi_control/ACLK axi_control/S00_ACLK axi_control/M00_ACLK axi_control/M01_ACLK axi_control/M02_ACLK
    axi_memory/ACLK axi_memory/S00_ACLK axi_memory/S01_ACLK axi_memory/M00_ACLK
}
foreach pin $pose_clock_pins {
    connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins $pin]
}
connect_bd_net [get_bd_pins rst_fclk0/interconnect_aresetn] [get_bd_pins axi_control/ARESETN] [get_bd_pins axi_memory/ARESETN]
foreach pin {
    pose_distance_0/s_axi_aresetn image_preprocess_0/s_axi_aresetn image_dma_0/axi_resetn
    axi_control/S00_ARESETN axi_control/M00_ARESETN axi_control/M01_ARESETN axi_control/M02_ARESETN
    axi_memory/S00_ARESETN axi_memory/S01_ARESETN axi_memory/M00_ARESETN
} {
    connect_bd_net [get_bd_pins rst_fclk0/peripheral_aresetn] [get_bd_pins $pin]
}
# Explicit CPU windows preserve the classifier ABI and add image and DMA control.
assign_bd_address -offset 0x43C00000 -range 64K -target_address_space [get_bd_addr_spaces processing_system7_0/Data] [get_bd_addr_segs pose_distance_0/S_AXI/registers]
assign_bd_address -offset 0x43C10000 -range 64K -target_address_space [get_bd_addr_spaces processing_system7_0/Data] [get_bd_addr_segs image_preprocess_0/S_AXI/registers]
assign_bd_address -offset 0x40400000 -range 64K -target_address_space [get_bd_addr_spaces processing_system7_0/Data] [get_bd_addr_segs image_dma_0/S_AXI_LITE/Reg]
# DMA reads and writes share the PS HP0 DDR port. The TUL preset is 512 MiB.
foreach space {Data_MM2S Data_S2MM} {
    assign_bd_address -offset 0x00000000 -range 512M -target_address_space [get_bd_addr_spaces image_dma_0/$space] [get_bd_addr_segs processing_system7_0/S_AXI_HP0/HP0_DDR_LOWOCM]
}

# Real MediaPipe landmark output convolution: dedicated streaming DMA and HP0 arbitration.
create_bd_cell -type ip -vlnv pose.local:pose:pose_conv_axi:1.0 pose_conv_0
create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 conv_dma_0
set_property -dict [list CONFIG.c_include_sg {0} CONFIG.c_micro_dma {0} \
 CONFIG.c_include_mm2s {1} CONFIG.c_include_s2mm {1} CONFIG.c_sg_length_width {23} \
 CONFIG.c_m_axi_mm2s_data_width {64} CONFIG.c_m_axi_s2mm_data_width {64} \
 CONFIG.c_m_axis_mm2s_tdata_width {32} CONFIG.c_include_mm2s_dre {0} CONFIG.c_include_s2mm_dre {0} \
 CONFIG.c_mm2s_burst_size {16} CONFIG.c_s2mm_burst_size {16}] [get_bd_cells conv_dma_0]
set_property CONFIG.NUM_MI 5 [get_bd_cells axi_control]
set_property CONFIG.NUM_SI 4 [get_bd_cells axi_memory]
connect_bd_intf_net [get_bd_intf_pins axi_control/M03_AXI] [get_bd_intf_pins pose_conv_0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_control/M04_AXI] [get_bd_intf_pins conv_dma_0/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins conv_dma_0/M_AXIS_MM2S] [get_bd_intf_pins pose_conv_0/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins pose_conv_0/M_AXIS] [get_bd_intf_pins conv_dma_0/S_AXIS_S2MM]
connect_bd_intf_net [get_bd_intf_pins conv_dma_0/M_AXI_MM2S] [get_bd_intf_pins axi_memory/S02_AXI]
connect_bd_intf_net [get_bd_intf_pins conv_dma_0/M_AXI_S2MM] [get_bd_intf_pins axi_memory/S03_AXI]
foreach pin {
 pose_conv_0/s_axi_aclk conv_dma_0/s_axi_lite_aclk conv_dma_0/m_axi_mm2s_aclk conv_dma_0/m_axi_s2mm_aclk
 axi_control/M03_ACLK axi_control/M04_ACLK axi_memory/S02_ACLK axi_memory/S03_ACLK
} {
 connect_bd_net [get_bd_pins processing_system7_0/FCLK_CLK0] [get_bd_pins $pin]
 lappend pose_clock_pins $pin
}
foreach pin {
 pose_conv_0/s_axi_aresetn conv_dma_0/axi_resetn axi_control/M03_ARESETN axi_control/M04_ARESETN
 axi_memory/S02_ARESETN axi_memory/S03_ARESETN
} {connect_bd_net [get_bd_pins rst_fclk0/peripheral_aresetn] [get_bd_pins $pin]}
assign_bd_address -offset 0x43C20000 -range 64K -target_address_space [get_bd_addr_spaces processing_system7_0/Data] [get_bd_addr_segs pose_conv_0/S_AXI/registers]
assign_bd_address -offset 0x40410000 -range 64K -target_address_space [get_bd_addr_spaces processing_system7_0/Data] [get_bd_addr_segs conv_dma_0/S_AXI_LITE/Reg]
foreach space {Data_MM2S Data_S2MM} {
 assign_bd_address -offset 0x00000000 -range 512M -target_address_space [get_bd_addr_spaces conv_dma_0/$space] [get_bd_addr_segs processing_system7_0/S_AXI_HP0/HP0_DDR_LOWOCM]
}

assign_bd_address
validate_bd_design
# BEGIN IMAGE_DMA_CHECK
# Derived S2MM stream width is checked after connecting the 32-bit image output.
foreach {dma_parameter expected} {
    c_include_sg 0 c_micro_dma 0 c_include_mm2s 1 c_include_s2mm 1 c_sg_length_width 23
    c_m_axi_mm2s_data_width 64 c_m_axi_s2mm_data_width 64
    c_m_axis_mm2s_tdata_width 32 c_s_axis_s2mm_tdata_width 32
    c_include_mm2s_dre 0 c_include_s2mm_dre 0
} {
    set actual [get_property CONFIG.$dma_parameter [get_bd_cells image_dma_0]]
    if {![string is integer -strict $actual] || $actual != $expected} {
        error "DMA parameter mismatch: $dma_parameter=$actual, expected $expected"
    }
}
foreach {source sink} {
    image_dma_0/M_AXIS_MM2S image_preprocess_0/S_AXIS
    image_preprocess_0/M_AXIS image_dma_0/S_AXIS_S2MM
    image_dma_0/M_AXI_MM2S axi_memory/S00_AXI
    image_dma_0/M_AXI_S2MM axi_memory/S01_AXI
    axi_memory/M00_AXI processing_system7_0/S_AXI_HP0
} {
    set source_net [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins $source]]
    set sink_net [get_bd_intf_nets -quiet -of_objects [get_bd_intf_pins $sink]]
    if {[llength $source_net] != 1 || [llength $sink_net] != 1 || $source_net ne $sink_net} {
        error "Image/DMA data connection mismatch: $source -> $sink"
    }
}
puts "IMAGE_DMA_CHECK_PASS: simple DMA 64-bit DDR/32-bit pixel stream, 23-bit length, HP0 data path."
# END IMAGE_DMA_CHECK
# BEGIN POSE_RESET_CHECK
# Never export a design whose unused auxiliary reset is asserted. These
# polarity properties are derived/read-only; inspect them rather than set them.
foreach pose_reset_parameter {C_EXT_RESET_HIGH C_AUX_RESET_HIGH} {
    set pose_reset_polarity [get_property CONFIG.$pose_reset_parameter [get_bd_cells rst_fclk0]]
    if {$pose_reset_polarity ne "0"} {
        error "Expected active-low $pose_reset_parameter=0, found $pose_reset_polarity. Review reset connections before synthesis."
    }
}
foreach {pose_const pose_value} {const_one 1 const_zero 0} {
    set pose_const_width [get_property CONFIG.CONST_WIDTH [get_bd_cells $pose_const]]
    set pose_const_value [get_property CONFIG.CONST_VAL [get_bd_cells $pose_const]]
    if {$pose_const_width ne "1" || ![string is wideinteger -strict $pose_const_value] || $pose_const_value != $pose_value} {
        error "Reset constant $pose_const must be width 1, value $pose_value; found width=$pose_const_width value=$pose_const_value."
    }
}
foreach {pose_reset_source pose_reset_sink} {
    const_one/dout rst_fclk0/aux_reset_in
    const_one/dout rst_fclk0/dcm_locked
    const_zero/dout rst_fclk0/mb_debug_sys_rst
    processing_system7_0/FCLK_RESET0_N rst_fclk0/ext_reset_in
    rst_fclk0/interconnect_aresetn axi_control/ARESETN
    rst_fclk0/peripheral_aresetn pose_distance_0/s_axi_aresetn
    rst_fclk0/peripheral_aresetn axi_control/S00_ARESETN
    rst_fclk0/peripheral_aresetn axi_control/M00_ARESETN
    rst_fclk0/peripheral_aresetn axi_control/M01_ARESETN
    rst_fclk0/peripheral_aresetn axi_control/M02_ARESETN
    rst_fclk0/peripheral_aresetn image_preprocess_0/s_axi_aresetn
    rst_fclk0/peripheral_aresetn image_dma_0/axi_resetn
    rst_fclk0/interconnect_aresetn axi_memory/ARESETN
    rst_fclk0/peripheral_aresetn axi_memory/S00_ARESETN
    rst_fclk0/peripheral_aresetn axi_memory/S01_ARESETN
    rst_fclk0/peripheral_aresetn axi_memory/M00_ARESETN
} {
    set pose_reset_source_pin [get_bd_pins -quiet $pose_reset_source]
    set pose_reset_sink_pin [get_bd_pins -quiet $pose_reset_sink]
    if {[llength $pose_reset_source_pin] != 1 || [llength $pose_reset_sink_pin] != 1} {
        error "Missing or ambiguous reset endpoint: $pose_reset_source -> $pose_reset_sink."
    }
    set pose_reset_source_net [get_bd_nets -quiet -of_objects $pose_reset_source_pin]
    set pose_reset_sink_net [get_bd_nets -quiet -of_objects $pose_reset_sink_pin]
    if {[llength $pose_reset_source_net] != 1 || [llength $pose_reset_sink_net] != 1 || [lindex $pose_reset_source_net 0] ne [lindex $pose_reset_sink_net 0]} {
        error "Reset connection mismatch: $pose_reset_source -> $pose_reset_sink."
    }
}
puts "RESET_CHECK_PASS: EXT/AUX active-low; AUX tied HIGH, debug reset LOW, dcm_locked HIGH; all AXI reset connections verified."
# END POSE_RESET_CHECK
# BEGIN POSE_CLOCK_CHECK
# Check the configured clock source and physical BD net connections. Legacy
# GP0 metadata is not the clock generator or evidence of an actual 10 MHz clock.
set pose_fclk_mhz [get_property CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ $ps]
if {![string is double -strict $pose_fclk_mhz] || [expr {double($pose_fclk_mhz)}] != 100.0} {
    error "FCLK0 configuration is not 100 MHz: $pose_fclk_mhz"
}
set pose_fclk_pin [get_bd_pins -quiet processing_system7_0/FCLK_CLK0]
if {[llength $pose_fclk_pin] != 1} {error "FCLK_CLK0 source pin is missing or ambiguous"}
set pose_fclk_hz [get_property CONFIG.FREQ_HZ $pose_fclk_pin]
if {![string is double -strict $pose_fclk_hz] || [expr {double($pose_fclk_hz)}] != 100000000.0} {
    error "FCLK_CLK0 output frequency is not 100000000 Hz: $pose_fclk_hz"
}
set pose_fclk_net [get_bd_nets -quiet -of_objects $pose_fclk_pin]
if {[llength $pose_fclk_net] != 1} {error "FCLK_CLK0 must drive one connected clock net"}
foreach pose_clock_pin $pose_clock_pins {
    set pose_sink [get_bd_pins -quiet $pose_clock_pin]
    if {[llength $pose_sink] != 1} {error "Missing clock sink: $pose_clock_pin"}
    set pose_sink_net [get_bd_nets -quiet -of_objects $pose_sink]
    if {[llength $pose_sink_net] != 1 || [lindex $pose_sink_net 0] ne [lindex $pose_fclk_net 0]} {
        error "Clock sink $pose_clock_pin is not connected to the FCLK_CLK0 net"
    }
}
set gp0_frequency [get_property CONFIG.PCW_M_AXI_GP0_FREQMHZ $ps]
puts "CLOCK_CHECK_PASS: FCLK0 is configured for 100 MHz; GP0, RTL, reset and interconnect clocks share its BD net."
if {![string is double -strict $gp0_frequency] || [expr {double($gp0_frequency)}] != 100.0} {
    puts "NOTE: board preset retains legacy PCW_M_AXI_GP0_FREQMHZ=$gp0_frequency. Clock source/topology checks passed; implementation timing must still pass."
}
# END POSE_CLOCK_CHECK
save_bd_design
set bd_file [get_files */pose_system.bd]
if {[llength $bd_file] != 1} {error "Expected one pose_system.bd"}
generate_target all $bd_file
set wrappers [make_wrapper -files $bd_file -top]
add_files -norecurse $wrappers
set_property top pose_system_wrapper [get_filesets sources_1]
update_compile_order -fileset sources_1
write_bd_tcl -force [file join $build_dir pose_system_generated.tcl]

launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {error "Synthesis failed; inspect synth_1 logs"}
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {error "Implementation failed; inspect impl_1 logs"}
open_run impl_1
set reports [file join $build_dir reports]
file mkdir $reports
report_utilization -file [file join $reports utilization.rpt]
report_utilization -hierarchical -file [file join $reports utilization_hierarchical.rpt]
report_timing -max_paths 10 -file [file join $reports timing_paths.rpt]
report_timing_summary -file [file join $reports timing_summary.rpt]
report_drc -file [file join $reports drc.rpt]
report_clocks -file [file join $reports clocks.rpt]
# BEGIN POSE_IMPLEMENTED_CLOCK_CHECK
# Verify the timing clock at the custom core's implemented flip-flops. Positive
# slack alone would not detect a clock accidentally constrained to 100 ns.
set pose_ff_clock_pins {}
foreach pose_clock_instance {pose_distance_0 image_preprocess_0 pose_conv_0} {
    set instance_clock_pins [get_pins -quiet -hierarchical -filter "NAME =~ *${pose_clock_instance}* && REF_PIN_NAME == C"]
    if {[llength $instance_clock_pins] == 0} {
        error "Cannot locate the implemented RTL flip-flop clocks for $pose_clock_instance. Inspect hierarchy and [file join $reports clocks.rpt]; overlay was not copied."
    }
    set pose_ff_clock_pins [concat $pose_ff_clock_pins $instance_clock_pins]
}
foreach pose_ff_clock_pin $pose_ff_clock_pins {
    set pose_impl_clock [get_clocks -quiet -of_objects $pose_ff_clock_pin]
    if {[llength $pose_impl_clock] != 1} {
        error "RTL clock pin $pose_ff_clock_pin is unconstrained or has multiple clocks; overlay was not copied."
    }
    set pose_impl_period [get_property PERIOD [lindex $pose_impl_clock 0]]
    if {![string is double -strict $pose_impl_period] || [expr {abs(double($pose_impl_period)-10.0)}] > 0.000001} {
        error "RTL timing clock period must be 10 ns (100 MHz), found $pose_impl_period at $pose_ff_clock_pin; overlay was not copied."
    }
}
puts "IMPLEMENTED_CLOCK_CHECK_PASS: every identified RTL flip-flop clock is constrained to 10 ns."
# END POSE_IMPLEMENTED_CLOCK_CHECK
foreach delay_type {max min} {
    set path [get_timing_paths -quiet -delay_type $delay_type -max_paths 1]
    if {[llength $path] != 1 || [get_property SLACK $path] < 0} {
        error "Timing not verified/met for $delay_type paths; overlay was not copied. Review reports."
    }
}
set bit [file join [get_property DIRECTORY [get_runs impl_1]] pose_system_wrapper.bit]
set hwh [file join $project_dir pose_distance.gen sources_1 bd pose_system hw_handoff pose_system.hwh]
if {![file isfile $hwh]} {set hwh [file join [file dirname [lindex $bd_file 0]] hw_handoff pose_system.hwh]}
if {![file isfile $bit] || ![file isfile $hwh]} {error "Real bit/HWH outputs not found; no placeholder is created"}
set overlays [file join $package_dir overlays]
file mkdir $overlays
file copy -force $bit [file join $overlays pose_distance.bit]
file copy -force $hwh [file join $overlays pose_distance.hwh]
puts "Real camera image + pose distance RTL overlay: [file join $overlays pose_distance.bit]"
puts "Matching HWH: [file join $overlays pose_distance.hwh]"
puts "Check reports for actual BRAM/DSP usage and setup/hold timing. No speedup is claimed."
