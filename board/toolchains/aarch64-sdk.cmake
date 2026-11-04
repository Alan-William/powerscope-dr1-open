set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
foreach(var IN ITEMS POWERSCOPE_CROSS_ROOT POWERSCOPE_BOARD_SYSROOT POWERSCOPE_CRT_DIR)
  if(NOT DEFINED ENV{${var}})
    message(FATAL_ERROR "Set environment variable ${var}")
  endif()
endforeach()
set(CMAKE_CXX_COMPILER "$ENV{POWERSCOPE_CROSS_ROOT}/usr/bin/aarch64-linux-gnu-g++")
set(CMAKE_SYSROOT "$ENV{POWERSCOPE_BOARD_SYSROOT}")
# Use the provided board's GCC 9 C++ headers, startup objects and runtime libs.
# The GCC 13 compiler itself runs on the x86_64 Ubuntu host.
# GCC 13 reserves __remove_cv; GCC 9's unique_ptr uses it as a private alias.
set(CMAKE_CXX_FLAGS_INIT "-D__remove_cv=__powerscope_sdk9_remove_cv -B$ENV{POWERSCOPE_CRT_DIR}/ -nostdinc++ -isystem ${CMAKE_SYSROOT}/usr/include/c++/9 -isystem ${CMAKE_SYSROOT}/usr/include/aarch64-linux-gnu/c++/9")
set(CMAKE_EXE_LINKER_FLAGS_INIT "-L${CMAKE_SYSROOT}/usr/lib/gcc/aarch64-linux-gnu/9 -L${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu -L${CMAKE_SYSROOT}/lib/aarch64-linux-gnu -Wl,-rpath-link,${CMAKE_SYSROOT}/usr/lib/aarch64-linux-gnu -Wl,-rpath-link,${CMAKE_SYSROOT}/lib/aarch64-linux-gnu")

set(CMAKE_SHARED_LINKER_FLAGS_INIT "${CMAKE_EXE_LINKER_FLAGS_INIT}")
