cmake_minimum_required(VERSION 3.21)

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_BINARY_DIR)
    message(FATAL_ERROR
        "In-source builds are not allowed. "
        "Please create a separate build directory and run cmake from there.\n"
        "To clean up, remove CMakeCache.txt and CMakeFiles/ directory.")
endif()

project(fcitx5-grimodex VERSION 0.2.1)
include(GNUInstallDirs)

set(CMAKE_CXX_STANDARD 20)

add_definitions(-DFCITX_GETTEXT_DOMAIN=\"fcitx5-grimodex\")

find_package(Protobuf REQUIRED)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
    pkg_check_modules(PROTOBUF_PKG QUIET IMPORTED_TARGET protobuf)
endif()

find_package(Fcitx5Core 5.0.4 REQUIRED)
find_package(Fcitx5Utils 5.0.4 REQUIRED)
find_package(Fcitx5Config 5.0.4 REQUIRED)

set(GRIMODEX_FCITX_ADDON_INSTALL_DIR
    "${FCITX_INSTALL_LIBDIR}/fcitx5"
    CACHE STRING "Install directory for the Grimodex Fcitx addon")

include("${FCITX_INSTALL_CMAKECONFIG_DIR}/Fcitx5Utils/Fcitx5CompilerSettings.cmake")

fcitx5_add_i18n_definition()

find_package(Gettext REQUIRED)

if(HAZKEY_FLATPAK)
    set(HAZKEY_ICON_NAME "com.miyakey.grimodex.ime.fcitx5")
else()
    set(HAZKEY_ICON_NAME "fcitx5-grimodex")
endif()

add_subdirectory(po)
add_subdirectory(src)

fcitx5_translate_desktop_file(org.fcitx.Fcitx5.Addon.Hazkey.metainfo.xml.in
                              com.miyakey.grimodex.ime.fcitx5.metainfo.xml XML)

install(FILES "${CMAKE_CURRENT_BINARY_DIR}/com.miyakey.grimodex.ime.fcitx5.metainfo.xml" DESTINATION "${CMAKE_INSTALL_FULL_DATADIR}/metainfo")
