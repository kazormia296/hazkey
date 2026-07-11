cmake_minimum_required(VERSION 3.21)

project(hazkey-session-client-tests LANGUAGES CXX)

include(CTest)
find_package(Protobuf REQUIRED)
find_package(ICU REQUIRED COMPONENTS i18n)

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
target_link_libraries(hazkey-session-protocol PUBLIC ${Protobuf_LITE_LIBRARIES})

add_executable(
    hazkey-session-client-test
    hazkey_session_client_test.cpp
    ../src/hazkey_session_client.cpp
)
target_include_directories(
    hazkey-session-client-test
    PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
target_link_libraries(hazkey-session-client-test PRIVATE hazkey-session-protocol)

add_test(NAME hazkey-session-client-test COMMAND hazkey-session-client-test)

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
    hazkey-candidate-display-test
    hazkey_candidate_display_test.cpp
)
target_compile_features(hazkey-candidate-display-test PRIVATE cxx_std_20)
target_compile_options(hazkey-candidate-display-test PRIVATE -Wall -Wextra -Werror)
target_include_directories(
    hazkey-candidate-display-test
    PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src"
)
add_test(NAME hazkey-candidate-display-test COMMAND hazkey-candidate-display-test)

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
target_link_libraries(hazkey-text-offset-test PRIVATE ICU::i18n)
add_test(NAME hazkey-text-offset-test COMMAND hazkey-text-offset-test)
