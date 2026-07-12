cmake_minimum_required(VERSION 3.21)

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_BINARY_DIR)
    message(FATAL_ERROR
        "In-source builds are not allowed. "
        "Please create a separate build directory and run cmake from there.\n"
        "To clean up, remove CMakeCache.txt and CMakeFiles/ directory.")
endif()

project(fcitx5-grimodex-settings VERSION 0.2.1 LANGUAGES CXX)

set(CMAKE_AUTOUIC ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)
set(CMAKE_AUTOUIC_SEARCH_PATHS ${CMAKE_CURRENT_SOURCE_DIR})

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Qt6 REQUIRED COMPONENTS Widgets LinguistTools)
find_package(Protobuf REQUIRED)

set(TS_FILES hazkey-settings_ja_JP.ts)

configure_file(constants.h.in constants.h @ONLY)

# Proto files
set(PROTO_FILES
    ${CMAKE_CURRENT_SOURCE_DIR}/../protocol/base.proto
    ${CMAKE_CURRENT_SOURCE_DIR}/../protocol/commands.proto
    ${CMAKE_CURRENT_SOURCE_DIR}/../protocol/config.proto
)

set(PROJECT_SOURCES
    main.cpp
    mainwindow.cpp
    mainwindow.h
    mainwindow.ui
    controllers/tab_context.h
    controllers/warning_widget_factory.cpp
    controllers/warning_widget_factory.h
    controllers/user_interface_tab_controller.cpp
    controllers/user_interface_tab_controller.h
    controllers/conversion_tab_controller.cpp
    controllers/conversion_tab_controller.h
    controllers/input_style_tab_controller.cpp
    controllers/input_style_tab_controller.h
    controllers/dictionary_tab_controller.cpp
    controllers/dictionary_tab_controller.h
    controllers/ai_tab_controller.cpp
    controllers/ai_tab_controller.h
    controllers/about_tab_controller.cpp
    controllers/about_tab_controller.h
    controllers/dual_list_controller.cpp
    controllers/dual_list_controller.h
    serverconnector.cpp
    serverconnector.h
    settings_product_paths.h
    config_definitions.h
    config_macros.h
    ${TS_FILES}
)

add_library(grimodex-settings-protocol STATIC)

qt_add_executable(fcitx5-grimodex-settings ${PROJECT_SOURCES}
    hazkey-icon.qrc)
qt_create_translation(QM_FILES ${PROJECT_SOURCES} ${TS_FILES})

foreach(_qm ${QM_FILES})
    get_filename_component(_qm_name ${_qm} NAME)
    set_source_files_properties(${_qm} PROPERTIES QT_RESOURCE_ALIAS ${_qm_name})
endforeach()

qt_add_resources(fcitx5-grimodex-settings "translations"
    PREFIX "/i18n"
    FILES ${QM_FILES}
)

if(Protobuf_VERSION VERSION_GREATER_EQUAL "3.15")
    # 3.15 ~：stable proto3 optional support
    message(STATUS "Using standard protobuf_generate (protobuf ${Protobuf_VERSION})")
    protobuf_generate(
        TARGET grimodex-settings-protocol
        LANGUAGE cpp
        PROTOS ${PROTO_FILES}
        IMPORT_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/../protocol
        PROTOC_OUT_DIR ${CMAKE_CURRENT_BINARY_DIR}
    )
elseif(Protobuf_VERSION VERSION_GREATER_EQUAL "3.12")
    # 3.12 ~ 3.14: build with flag. cannot use PROTOC_OPTIONS.
    # Ubuntu 22.04: v3.12.4 (2025/8/28)
    message(STATUS "Using manual protoc execution for proto3 optional (protobuf ${Protobuf_VERSION})")

    set(PROTO_SRCS)
    set(PROTO_HDRS)

    foreach(PROTO_FILE ${PROTO_FILES})
        get_filename_component(PROTO_NAME ${PROTO_FILE} NAME_WE)
        get_filename_component(PROTO_PATH ${PROTO_FILE} ABSOLUTE)
        set(PROTO_SRC "${CMAKE_CURRENT_BINARY_DIR}/${PROTO_NAME}.pb.cc")
        set(PROTO_HDR "${CMAKE_CURRENT_BINARY_DIR}/${PROTO_NAME}.pb.h")

        list(APPEND PROTO_SRCS ${PROTO_SRC})
        list(APPEND PROTO_HDRS ${PROTO_HDR})

        add_custom_command(
            OUTPUT ${PROTO_SRC} ${PROTO_HDR}
            COMMAND ${Protobuf_PROTOC_EXECUTABLE}
            ARGS --cpp_out=${CMAKE_CURRENT_BINARY_DIR}
                 --experimental_allow_proto3_optional
                 --proto_path=${CMAKE_CURRENT_SOURCE_DIR}/../protocol
                 ${CMAKE_CURRENT_SOURCE_DIR}/../protocol/${PROTO_NAME}.proto
            DEPENDS ${PROTO_FILE}
            COMMENT "Generating C++ protobuf files from ${PROTO_FILE}"
            VERBATIM
        )
    endforeach()

    target_sources(grimodex-settings-protocol PRIVATE ${PROTO_SRCS} ${PROTO_HDRS})
else()
    # ~ 3.12：no proto3 optional support
    message(FATAL_ERROR "protobuf 3.12+ required for proto3 optional support. Current version: ${Protobuf_VERSION}")
endif()

target_include_directories(grimodex-settings-protocol PUBLIC
    ${CMAKE_CURRENT_BINARY_DIR}
    ${Protobuf_INCLUDE_DIRS}
)

target_link_libraries(grimodex-settings-protocol PUBLIC
    ${Protobuf_LITE_LIBRARIES}
)

target_include_directories(fcitx5-grimodex-settings PRIVATE
    ${CMAKE_CURRENT_BINARY_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../linux-shared
    ${Protobuf_INCLUDE_DIRS}
)

target_link_libraries(fcitx5-grimodex-settings PRIVATE
    Qt6::Widgets
    grimodex-settings-protocol
)

include(GNUInstallDirs)
install(TARGETS fcitx5-grimodex-settings
    RUNTIME DESTINATION ${CMAKE_INSTALL_FULL_LIBDIR}/fcitx5-grimodex
)

# install desktop entry
if(GRIMODEX_FLATPAK OR HAZKEY_FLATPAK)
    set(GRIMODEX_ICON_NAME "org.grimodex.Grimodex")
    set(GRIMODEX_SETTINGS_DESKTOP_FILE_NAME org.grimodex.Grimodex.IME.Settings)
else()
    set(GRIMODEX_ICON_NAME "fcitx5-grimodex")
    set(GRIMODEX_SETTINGS_DESKTOP_FILE_NAME fcitx5-grimodex-settings)
endif()

configure_file(hazkey-settings.desktop.in ${GRIMODEX_SETTINGS_DESKTOP_FILE_NAME}.desktop @ONLY)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/${GRIMODEX_SETTINGS_DESKTOP_FILE_NAME}.desktop"
    DESTINATION ${CMAKE_INSTALL_FULL_DATADIR}/applications)

# create a symlink in the binary directory
install(CODE "
    file(MAKE_DIRECTORY \"\$ENV{DESTDIR}${CMAKE_INSTALL_FULL_BINDIR}\")
    file(CREATE_LINK
    \"${CMAKE_INSTALL_FULL_LIBDIR}/fcitx5-grimodex/fcitx5-grimodex-settings\"
    \"\$ENV{DESTDIR}${CMAKE_INSTALL_FULL_BINDIR}/fcitx5-grimodex-settings\"
    SYMBOLIC)
")

include(CTest)
if(BUILD_TESTING)
    add_executable(grimodex-settings-product-paths-test
        tests/settings_product_paths_test.cpp)
    target_include_directories(grimodex-settings-product-paths-test PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/../linux-shared)
    add_test(NAME grimodex-settings-product-paths
        COMMAND grimodex-settings-product-paths-test)
    add_executable(grimodex-settings-server-connector-save-test
        tests/serverconnector_save_test.cpp
        serverconnector.cpp)
    target_include_directories(grimodex-settings-server-connector-save-test PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/../linux-shared)
    target_link_libraries(grimodex-settings-server-connector-save-test PRIVATE
        Qt6::Widgets
        grimodex-settings-protocol)
    add_test(NAME grimodex-settings-server-connector-save
        COMMAND grimodex-settings-server-connector-save-test)
    add_test(NAME grimodex-settings-build-contract
        COMMAND ${CMAKE_COMMAND}
            -DSETTINGS_SOURCE_DIR=${CMAKE_CURRENT_SOURCE_DIR}
            -P ${CMAKE_CURRENT_SOURCE_DIR}/tests/settings_build_contract.cmake)
endif()
