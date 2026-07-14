cmake_minimum_required(VERSION 3.21)

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_BINARY_DIR)
    message(FATAL_ERROR
        "In-source builds are not allowed. "
        "Please create a separate build directory and run cmake from there.\n"
        "To clean up, remove CMakeCache.txt and CMakeFiles/ directory.")
endif()

project(grimodex-ime VERSION 0.2.1)
include(GNUInstallDirs)
include(CTest)

# Persist GNUInstallDirs' resolved paths for repo-local runtime helpers. The
# FULL variables include multiarch layouts and CMake's special prefix rules.
set(GRIMODEX_INSTALL_FULL_BINDIR "${CMAKE_INSTALL_FULL_BINDIR}" CACHE INTERNAL
    "Resolved Grimodex executable install directory")
set(GRIMODEX_INSTALL_FULL_LIBDIR "${CMAKE_INSTALL_FULL_LIBDIR}" CACHE INTERNAL
    "Resolved Grimodex library install directory")
set(GRIMODEX_INSTALL_FULL_DATADIR "${CMAKE_INSTALL_FULL_DATADIR}" CACHE INTERNAL
    "Resolved Grimodex data install directory")

set(CMAKE_SKIP_INSTALL_ALL_DEPENDENCY TRUE)

add_subdirectory(fcitx5-hazkey)
add_subdirectory(hazkey-server)
add_subdirectory(hazkey-settings)

if(BUILD_TESTING)
    add_subdirectory(fcitx5-hazkey/tests)
endif()
