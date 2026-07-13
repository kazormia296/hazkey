if(NOT SWIFT_SCRATCH_PATH)
    message(FATAL_ERROR "SWIFT_SCRATCH_PATH is required")
endif()

set(_AZOOKEY_EXPECTED_REVISION_DEFAULT
    "8b4befc273baafea5964ecf87d3bc36f2bbef68b"
)
if(NOT AZOOKEY_EXPECTED_REVISION)
    set(AZOOKEY_EXPECTED_REVISION "${_AZOOKEY_EXPECTED_REVISION_DEFAULT}")
endif()

file(GLOB _AZOOKEY_CHECKOUTS LIST_DIRECTORIES true
    "${SWIFT_SCRATCH_PATH}/checkouts/AzooKeyKanaKanjiConverter*"
)
list(LENGTH _AZOOKEY_CHECKOUTS _AZOOKEY_CHECKOUT_COUNT)
if(NOT _AZOOKEY_CHECKOUT_COUNT EQUAL 1)
    message(FATAL_ERROR
        "Expected one AzooKeyKanaKanjiConverter checkout, found ${_AZOOKEY_CHECKOUT_COUNT}"
    )
endif()
list(GET _AZOOKEY_CHECKOUTS 0 _AZOOKEY_CHECKOUT)

find_program(GIT_EXECUTABLE NAMES git REQUIRED)
execute_process(
    COMMAND "${GIT_EXECUTABLE}" -C "${_AZOOKEY_CHECKOUT}" rev-parse HEAD
    RESULT_VARIABLE _REVISION_RESULT
    OUTPUT_VARIABLE _ACTUAL_REVISION
    ERROR_VARIABLE _REVISION_ERROR
    OUTPUT_STRIP_TRAILING_WHITESPACE
)
if(NOT _REVISION_RESULT EQUAL 0)
    message(FATAL_ERROR
        "Could not inspect the AzooKey checkout revision: ${_REVISION_ERROR}"
    )
endif()
if(NOT "${_ACTUAL_REVISION}" STREQUAL "${AZOOKEY_EXPECTED_REVISION}")
    message(FATAL_ERROR
        "Refusing to patch unexpected AzooKey revision ${_ACTUAL_REVISION}; "
        "expected ${AZOOKEY_EXPECTED_REVISION}"
    )
endif()

# SwiftPM does not support source patches in Package.swift. Keep the remote
# dependency pinned, and apply these repository-owned patches only to its
# ephemeral checkout after package resolution and immediately before compile.
# Each patch is idempotent and fails closed when the pinned upstream shape no
# longer matches.
set(_AZOOKEY_PATCHES
    "${CMAKE_CURRENT_LIST_DIR}/patches/AzooKeyKanaKanjiConverter/0001-fix-non-zenzai-mock-duplicates.patch"
    "${CMAKE_CURRENT_LIST_DIR}/patches/AzooKeyKanaKanjiConverter/0002-reuse-zenzai-context.patch"
)
foreach(_PATCH IN LISTS _AZOOKEY_PATCHES)
    if(NOT EXISTS "${_PATCH}")
        message(FATAL_ERROR "AzooKey patch is missing: ${_PATCH}")
    endif()

    execute_process(
        COMMAND "${GIT_EXECUTABLE}" -C "${_AZOOKEY_CHECKOUT}"
            apply --reverse --check "${_PATCH}"
        RESULT_VARIABLE _ALREADY_APPLIED
        OUTPUT_QUIET
        ERROR_QUIET
    )
    if(_ALREADY_APPLIED EQUAL 0)
        message(STATUS "AzooKey patch already applied: ${_PATCH}")
        continue()
    endif()

    execute_process(
        COMMAND "${GIT_EXECUTABLE}" -C "${_AZOOKEY_CHECKOUT}"
            apply --check "${_PATCH}"
        RESULT_VARIABLE _CHECK_RESULT
        OUTPUT_VARIABLE _CHECK_OUTPUT
        ERROR_VARIABLE _CHECK_ERROR
    )
    if(NOT _CHECK_RESULT EQUAL 0)
        message(FATAL_ERROR
            "AzooKey patch does not match the pinned checkout: ${_PATCH}\n"
            "${_CHECK_OUTPUT}${_CHECK_ERROR}"
        )
    endif()

    execute_process(
        COMMAND "${GIT_EXECUTABLE}" -C "${_AZOOKEY_CHECKOUT}"
            apply "${_PATCH}"
        RESULT_VARIABLE _APPLY_RESULT
        OUTPUT_VARIABLE _APPLY_OUTPUT
        ERROR_VARIABLE _APPLY_ERROR
    )
    if(NOT _APPLY_RESULT EQUAL 0)
        message(FATAL_ERROR
            "Failed to apply AzooKey patch: ${_PATCH}\n"
            "${_APPLY_OUTPUT}${_APPLY_ERROR}"
        )
    endif()
endforeach()

set(_ZENZ_CONTEXT
    "${_AZOOKEY_CHECKOUT}/Sources/KanaKanjiConverterModule/ConversionAlgorithms/Zenzai/Zenz/ZenzContext.swift"
)
set(_LLAMA_MOCK
    "${_AZOOKEY_CHECKOUT}/Sources/KanaKanjiConverterModule/ConversionAlgorithms/Zenzai/Zenz/llama-mock.swift"
)
file(READ "${_ZENZ_CONTEXT}" _ZENZ_CONTEXT_CONTENT)
file(READ "${_LLAMA_MOCK}" _LLAMA_MOCK_CONTENT)

set(_EXPECTED_RESET_BLOCK
"func reset_context() throws {
        llama_kv_cache_clear(self.context)
        self.prevInput = []
        self.prevPrompt = []
    }")
string(FIND "${_ZENZ_CONTEXT_CONTENT}" "${_EXPECTED_RESET_BLOCK}" _RESET_OFFSET)
if(_RESET_OFFSET EQUAL -1)
    message(FATAL_ERROR "Patched AzooKey reset_context has an unexpected shape")
endif()

string(REGEX MATCHALL
    "package func llama_kv_cache_clear\\(_: llama_context\\) \\{\\}"
    _KV_CLEAR_MOCKS
    "${_LLAMA_MOCK_CONTENT}"
)
list(LENGTH _KV_CLEAR_MOCKS _KV_CLEAR_MOCK_COUNT)
if(NOT _KV_CLEAR_MOCK_COUNT EQUAL 1)
    message(FATAL_ERROR
        "Expected exactly one llama_kv_cache_clear mock, found ${_KV_CLEAR_MOCK_COUNT}"
    )
endif()

foreach(_BACKEND_FUNCTION IN ITEMS ggml_backend_load_all ggml_backend_dev_count)
    string(REGEX MATCHALL
        "package func ${_BACKEND_FUNCTION}\\(\\)"
        _BACKEND_FUNCTION_MATCHES
        "${_LLAMA_MOCK_CONTENT}"
    )
    list(LENGTH _BACKEND_FUNCTION_MATCHES _BACKEND_FUNCTION_COUNT)
    if(NOT _BACKEND_FUNCTION_COUNT EQUAL 1)
        message(FATAL_ERROR
            "Expected exactly one ${_BACKEND_FUNCTION} mock, found ${_BACKEND_FUNCTION_COUNT}"
        )
    endif()
endforeach()
