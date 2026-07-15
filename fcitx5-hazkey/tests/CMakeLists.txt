cmake_minimum_required(VERSION 3.21)

project(hazkey-session-client-tests LANGUAGES CXX)

include(CTest)
find_package(Protobuf REQUIRED)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
    pkg_check_modules(PROTOBUF_PKG QUIET IMPORTED_TARGET protobuf)
endif()
find_package(Fcitx5Utils REQUIRED)
find_package(Fcitx5Core REQUIRED)
find_package(Fcitx5Module COMPONENTS TestFrontend QUIET)
find_package(Python3 COMPONENTS Interpreter REQUIRED)

option(GRIMODEX_ENABLE_MOZC_INTEGRATION_TESTS
    "Enable opt-in Mozc Fcitx full-stack and soak tests" OFF)
set(GRIMODEX_MOZC_SOAK_ITERATIONS "100" CACHE STRING
    "Conversions per cycle in the opt-in Mozc Fcitx soak test")
set(GRIMODEX_MOZC_RESTART_CYCLES "3" CACHE STRING
    "Fresh Fcitx/server cycles in the opt-in Mozc Fcitx soak test")
set(GRIMODEX_MOZC_SOAK_TIMEOUT_SECONDS "90" CACHE STRING
    "Per-cycle runner timeout for the opt-in Mozc Fcitx soak test")

if(GRIMODEX_ENABLE_MOZC_INTEGRATION_TESTS)
    foreach(_GRIMODEX_MOZC_POSITIVE_INTEGER IN ITEMS
            GRIMODEX_MOZC_SOAK_ITERATIONS
            GRIMODEX_MOZC_RESTART_CYCLES
            GRIMODEX_MOZC_SOAK_TIMEOUT_SECONDS)
        if(NOT "${${_GRIMODEX_MOZC_POSITIVE_INTEGER}}" MATCHES
                "^[1-9][0-9]*$")
            message(FATAL_ERROR
                "${_GRIMODEX_MOZC_POSITIVE_INTEGER} must be a positive "
                "decimal integer")
        endif()
    endforeach()
    if(GRIMODEX_MOZC_SOAK_ITERATIONS GREATER 1000000)
        message(FATAL_ERROR
            "GRIMODEX_MOZC_SOAK_ITERATIONS must not exceed 1000000")
    endif()
    if(GRIMODEX_MOZC_RESTART_CYCLES GREATER 1000)
        message(FATAL_ERROR
            "GRIMODEX_MOZC_RESTART_CYCLES must not exceed 1000")
    endif()
    if(GRIMODEX_MOZC_SOAK_TIMEOUT_SECONDS GREATER 86400)
        message(FATAL_ERROR
            "GRIMODEX_MOZC_SOAK_TIMEOUT_SECONDS must not exceed 86400")
    endif()
    math(EXPR _GRIMODEX_MOZC_SOAK_CTEST_TIMEOUT
        "60 + ${GRIMODEX_MOZC_RESTART_CYCLES} * (${GRIMODEX_MOZC_SOAK_TIMEOUT_SECONDS} + 10)")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(PROTOCOL_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../protocol")
set(PROTO_FILES
    "${PROTOCOL_DIR}/base.proto"
    "${PROTOCOL_DIR}/commands.proto"
    "${PROTOCOL_DIR}/config.proto"
)

add_library(hazkey-session-protocol STATIC)
protobuf_generate(
    TARGET hazkey-session-protocol
    LANGUAGE cpp
    PROTOS ${PROTO_FILES}
    IMPORT_DIRS "${PROTOCOL_DIR}"
    PROTOC_OUT_DIR "${CMAKE_CURRENT_BINARY_DIR}"
)
target_include_directories(hazkey-session-protocol PUBLIC "${CMAKE_CURRENT_BINARY_DIR}")
if(TARGET PkgConfig::PROTOBUF_PKG)
    set(HAZKEY_TEST_PROTOBUF_LINK_TARGET PkgConfig::PROTOBUF_PKG)
else()
    set(HAZKEY_TEST_PROTOBUF_LINK_TARGET ${Protobuf_LIBRARIES})
endif()
target_link_libraries(hazkey-session-protocol PUBLIC ${HAZKEY_TEST_PROTOBUF_LINK_TARGET})

add_executable(
    hazkey-session-client-test
    hazkey_session_client_test.cpp
    ../src/hazkey_session_client.cpp
    ../src/hazkey_effect_ledger.cpp
    ../src/hazkey_recovery_journal.cpp
)
target_include_directories(
    hazkey-session-client-test
    PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
target_compile_options(hazkey-session-client-test PRIVATE -Wall -Wextra -Werror)
target_link_libraries(hazkey-session-client-test PRIVATE hazkey-session-protocol)

add_test(NAME hazkey-session-client-test COMMAND hazkey-session-client-test)

add_executable(
    hazkey-socket-io-test
    hazkey_socket_io_test.cpp
    ../src/hazkey_socket_io.cpp
)
target_compile_features(hazkey-socket-io-test PRIVATE cxx_std_20)
target_compile_options(hazkey-socket-io-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-socket-io-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
add_test(NAME hazkey-socket-io-test COMMAND hazkey-socket-io-test)

add_executable(
    hazkey-server-connector-test
    hazkey_server_connector_test.cpp
    ../src/hazkey_server_connector.cpp
    ../src/hazkey_session_client.cpp
    ../src/hazkey_socket_io.cpp
    ../src/hazkey_effect_ledger.cpp
    ../src/hazkey_recovery_journal.cpp
)
target_compile_features(hazkey-server-connector-test PRIVATE cxx_std_20)
target_compile_definitions(
    hazkey-server-connector-test PRIVATE HAZKEY_ENABLE_TEST_HOOKS=1
)
target_compile_options(hazkey-server-connector-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-server-connector-test
    PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../src"
    "${CMAKE_CURRENT_SOURCE_DIR}/../../linux-shared"
)
target_link_libraries(
    hazkey-server-connector-test
    PRIVATE
    hazkey-session-protocol
    Fcitx5::Core
    Fcitx5::Utils
)
add_test(NAME hazkey-server-connector-test COMMAND hazkey-server-connector-test)

add_executable(
    grimodex-product-identity-test
    grimodex_product_identity_test.cpp
)
target_compile_features(grimodex-product-identity-test PRIVATE cxx_std_20)
target_compile_options(grimodex-product-identity-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    grimodex-product-identity-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../../linux-shared"
)
add_test(NAME grimodex-product-identity-test COMMAND grimodex-product-identity-test)

add_executable(
    hazkey-text-offset-test
    hazkey_text_offset_test.cpp
    ../src/hazkey_text_offset.cpp
)
target_compile_features(hazkey-text-offset-test PRIVATE cxx_std_20)
target_compile_options(hazkey-text-offset-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-text-offset-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
target_link_libraries(hazkey-text-offset-test PRIVATE Fcitx5::Utils)
add_test(NAME hazkey-text-offset-test COMMAND hazkey-text-offset-test)

add_executable(
    hazkey-v2-safety-test
    hazkey_v2_safety_test.cpp
    ../src/hazkey_effect_ledger.cpp
    ../src/hazkey_recovery_journal.cpp
)
target_compile_features(hazkey-v2-safety-test PRIVATE cxx_std_20)
target_compile_options(hazkey-v2-safety-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-v2-safety-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
add_test(NAME hazkey-v2-safety-test COMMAND hazkey-v2-safety-test)

add_test(
    NAME hazkey-contract-fixture-lock-test
    COMMAND
        "${CMAKE_COMMAND}"
        "-DFIXTURE_ROOT=${CMAKE_CURRENT_SOURCE_DIR}/../../hazkey-server/Tests/grimodex-spike/Fixtures/composition-behavior-v1"
        -P "${CMAKE_CURRENT_SOURCE_DIR}/verify_contract_fixture.cmake"
)

add_test(
    NAME hazkey-conversion-quality-self-test
    COMMAND
        "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tools/dictionary/evaluate_conversion_quality.py"
        --corpus
        "${CMAKE_CURRENT_SOURCE_DIR}/../../hazkey-server/Tests/grimodex-spike/Fixtures/quality-v1/conversion-quality-v1.tsv"
        --self-test
        --require-protected-input-surface
)

add_test(
    NAME hazkey-context-boundary-self-test
    COMMAND
        "${Python3_EXECUTABLE}"
        "${CMAKE_CURRENT_SOURCE_DIR}/../../tools/dictionary/evaluate_context_boundaries.py"
        --fixture
        "${CMAKE_CURRENT_SOURCE_DIR}/../../hazkey-server/Tests/grimodex-spike/Fixtures/quality-v1/context-boundary-v1.tsv"
        --self-test
)

add_executable(
    hazkey-action-mapper-test
    hazkey_action_mapper_test.cpp
    ../src/hazkey_action_mapper.cpp
)
target_compile_features(hazkey-action-mapper-test PRIVATE cxx_std_20)
target_compile_options(hazkey-action-mapper-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-action-mapper-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
target_link_libraries(hazkey-action-mapper-test PRIVATE Fcitx5::Utils)
add_test(NAME hazkey-action-mapper-test COMMAND hazkey-action-mapper-test)

add_executable(
    hazkey-candidate-interaction-test
    hazkey_candidate_interaction_test.cpp
    ../src/hazkey_candidate.cpp
)
target_compile_features(hazkey-candidate-interaction-test PRIVATE cxx_std_20)
target_compile_options(hazkey-candidate-interaction-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-candidate-interaction-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
target_link_libraries(
    hazkey-candidate-interaction-test
    PRIVATE hazkey-session-protocol Fcitx5::Core Fcitx5::Utils
)
add_test(NAME hazkey-candidate-interaction-test COMMAND hazkey-candidate-interaction-test)

add_executable(
    hazkey-snapshot-renderer-test
    hazkey_snapshot_renderer_test.cpp
    ../src/hazkey_snapshot_renderer.cpp
)
target_compile_features(hazkey-snapshot-renderer-test PRIVATE cxx_std_20)
target_compile_options(hazkey-snapshot-renderer-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-snapshot-renderer-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
target_link_libraries(
    hazkey-snapshot-renderer-test
    PRIVATE hazkey-session-protocol Fcitx5::Core Fcitx5::Utils
)
add_test(NAME hazkey-snapshot-renderer-test COMMAND hazkey-snapshot-renderer-test)

if(TARGET fcitx5-hazkey AND TARGET build_hazkey_server AND
   TARGET Fcitx5::Module::TestFrontend)
    add_executable(
        hazkey-fcitx-full-stack-test
        hazkey_fcitx_full_stack_test.cpp
    )
    target_compile_features(hazkey-fcitx-full-stack-test PRIVATE cxx_std_20)
    target_compile_options(
        hazkey-fcitx-full-stack-test PRIVATE -Wall -Wextra -Werror
    )
    target_link_libraries(
        hazkey-fcitx-full-stack-test
        PRIVATE Fcitx5::Core Fcitx5::Utils Fcitx5::Module::TestFrontend
    )
    add_dependencies(
        hazkey-fcitx-full-stack-test fcitx5-hazkey build_hazkey_server
    )

    add_test(
        NAME hazkey-fcitx-full-stack-test
        COMMAND
            "${Python3_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/run_fcitx_full_stack_test.py"
            --harness "$<TARGET_FILE:hazkey-fcitx-full-stack-test>"
            --addon "$<TARGET_FILE:fcitx5-hazkey>"
            --server
                "${CMAKE_BINARY_DIR}/hazkey-server/swift-build/${SWIFT_BUILD_TYPE}/hazkey-server"
            --dictionary
                "${CMAKE_SOURCE_DIR}/hazkey-server/azooKey_dictionary_storage/Dictionary"
            --addon-config
                "${CMAKE_BINARY_DIR}/fcitx5-hazkey/src/hazkey-addon.conf"
            --input-method-config
                "${CMAKE_BINARY_DIR}/fcitx5-hazkey/src/hazkey-im.conf"
            --system-test-addon-dir
                "${FCITX_INSTALL_PKGDATADIR}/testing/addon"
            --llama-lib-dir "${CMAKE_BINARY_DIR}/bin"
            --converter-backend hazkey
    )
    set_tests_properties(
        hazkey-fcitx-full-stack-test
        PROPERTIES TIMEOUT 60 SKIP_RETURN_CODE 77
    )
endif()

if(GRIMODEX_ENABLE_MOZC_INTEGRATION_TESTS)
    if(NOT TARGET fcitx5-hazkey OR NOT TARGET build_hazkey_server OR
       NOT TARGET Fcitx5::Module::TestFrontend OR
       NOT TARGET hazkey-fcitx-full-stack-test)
        message(FATAL_ERROR
            "Mozc integration tests require the product targets and the "
            "Fcitx TestFrontend module")
    endif()

    get_property(_GRIMODEX_MOZC_STAGED_GENERATION GLOBAL PROPERTY
        GRIMODEX_MOZC_STAGED_GENERATION)
    get_property(_GRIMODEX_MOZC_ARTIFACT_VERIFIER GLOBAL PROPERTY
        GRIMODEX_MOZC_ARTIFACT_VERIFIER)
    if(_GRIMODEX_MOZC_STAGED_GENERATION STREQUAL "" OR
       NOT IS_DIRECTORY "${_GRIMODEX_MOZC_STAGED_GENERATION}")
        message(FATAL_ERROR
            "Mozc integration tests require an exact verified artifact "
            "generation; configure HAZKEY_SERVER_MOZC_ARTIFACT_DIR")
    endif()
    if(_GRIMODEX_MOZC_ARTIFACT_VERIFIER STREQUAL "" OR
       NOT EXISTS "${_GRIMODEX_MOZC_ARTIFACT_VERIFIER}" OR
       IS_DIRECTORY "${_GRIMODEX_MOZC_ARTIFACT_VERIFIER}")
        message(FATAL_ERROR
            "Mozc integration tests require the artifact verifier used at "
            "configure time")
    endif()

    add_test(
        NAME hazkey-fcitx-mozc-full-stack-test
        COMMAND
            "${Python3_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/run_fcitx_full_stack_test.py"
            --harness "$<TARGET_FILE:hazkey-fcitx-full-stack-test>"
            --addon "$<TARGET_FILE:fcitx5-hazkey>"
            --server
                "${CMAKE_BINARY_DIR}/hazkey-server/swift-build/${SWIFT_BUILD_TYPE}/hazkey-server"
            --dictionary
                "${CMAKE_SOURCE_DIR}/hazkey-server/azooKey_dictionary_storage/Dictionary"
            --addon-config
                "${CMAKE_BINARY_DIR}/fcitx5-hazkey/src/hazkey-addon.conf"
            --input-method-config
                "${CMAKE_BINARY_DIR}/fcitx5-hazkey/src/hazkey-im.conf"
            --system-test-addon-dir
                "${FCITX_INSTALL_PKGDATADIR}/testing/addon"
            --llama-lib-dir "${CMAKE_BINARY_DIR}/bin"
            --converter-backend mozc
            --mozc-generation "${_GRIMODEX_MOZC_STAGED_GENERATION}"
            --mozc-verifier "${_GRIMODEX_MOZC_ARTIFACT_VERIFIER}"
            --cycles 1
            --soak-iterations 0
            --timeout 60
    )
    set_tests_properties(
        hazkey-fcitx-mozc-full-stack-test
        PROPERTIES TIMEOUT 130
    )

    add_test(
        NAME hazkey-fcitx-mozc-soak-test
        COMMAND
            "${Python3_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/run_fcitx_full_stack_test.py"
            --harness "$<TARGET_FILE:hazkey-fcitx-full-stack-test>"
            --addon "$<TARGET_FILE:fcitx5-hazkey>"
            --server
                "${CMAKE_BINARY_DIR}/hazkey-server/swift-build/${SWIFT_BUILD_TYPE}/hazkey-server"
            --dictionary
                "${CMAKE_SOURCE_DIR}/hazkey-server/azooKey_dictionary_storage/Dictionary"
            --addon-config
                "${CMAKE_BINARY_DIR}/fcitx5-hazkey/src/hazkey-addon.conf"
            --input-method-config
                "${CMAKE_BINARY_DIR}/fcitx5-hazkey/src/hazkey-im.conf"
            --system-test-addon-dir
                "${FCITX_INSTALL_PKGDATADIR}/testing/addon"
            --llama-lib-dir "${CMAKE_BINARY_DIR}/bin"
            --converter-backend mozc
            --mozc-generation "${_GRIMODEX_MOZC_STAGED_GENERATION}"
            --mozc-verifier "${_GRIMODEX_MOZC_ARTIFACT_VERIFIER}"
            --cycles "${GRIMODEX_MOZC_RESTART_CYCLES}"
            --soak-iterations "${GRIMODEX_MOZC_SOAK_ITERATIONS}"
            --timeout "${GRIMODEX_MOZC_SOAK_TIMEOUT_SECONDS}"
    )
    set_tests_properties(
        hazkey-fcitx-mozc-soak-test
        PROPERTIES TIMEOUT "${_GRIMODEX_MOZC_SOAK_CTEST_TIMEOUT}"
    )
endif()
