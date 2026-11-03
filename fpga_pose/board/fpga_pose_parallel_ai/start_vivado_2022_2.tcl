# Windows entry point, configured for the user's Vivado 2022.2 board repository.
# Run in an empty Vivado session:
# source {D:/FPGA/fpga_pose_ai_release/fpga_pose/start_vivado_2022_2.tcl}
# The build runs both image and classifier RTL simulations before synthesis.
set pose_start_dir [file dirname [file normalize [info script]]]
if {[llength [get_projects -quiet]] > 0} {
    error "An existing project is open. Save any needed changes, close the project, and run this entry again in an empty Vivado session."
}
set ::env(POSE_BOARD_REPO) {D:/vivado/Vivado/2022.2/data/xhub/boards/XilinxBoardStore/boards/Xilinx}
set ::env(POSE_JOBS) 4
set ::env(POSE_RUN_RTL_SIM) 1
set pose_run_base [file join $pose_start_dir build run_[clock format [clock seconds] -format %Y%m%d_%H%M%S]]
set pose_run_dir $pose_run_base
set pose_run_suffix 1
while {[file exists $pose_run_dir]} {
    set pose_run_dir ${pose_run_base}_${pose_run_suffix}
    incr pose_run_suffix
}
file mkdir $pose_run_dir
set ::env(POSE_BUILD_DIR) $pose_run_dir
cd $pose_start_dir
puts "Project folder: $pose_start_dir"
puts "Board repository: $::env(POSE_BOARD_REPO)"
puts "Fresh build directory: $::env(POSE_BUILD_DIR)"
puts "Both image and classifier standalone RTL simulations are enabled and must pass before synthesis."
source [file join $pose_start_dir hardware build_vivado.tcl]
puts "BUILD COMPLETED: check overlays/pose_distance.bit and overlays/pose_distance.hwh."
