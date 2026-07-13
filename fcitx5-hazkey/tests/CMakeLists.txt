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
    )
    set_tests_properties(
        hazkey-fcitx-full-stack-test
        PROPERTIES TIMEOUT 60 SKIP_RETURN_CODE 77
    )
endif()
