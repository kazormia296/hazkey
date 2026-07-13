if(NOT SWIFT_SCRATCH_PATH)
    message(FATAL_ERROR "SWIFT_SCRATCH_PATH is required")
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
set(_MOCK_FILE
    "${_AZOOKEY_CHECKOUT}/Sources/KanaKanjiConverterModule/ConversionAlgorithms/Zenzai/Zenz/llama-mock.swift"
)
if(NOT EXISTS "${_MOCK_FILE}")
    message(FATAL_ERROR "AzooKey non-Zenzai mock is missing: ${_MOCK_FILE}")
endif()

# The pinned upstream revision declares these two functions twice.  Swift only
# compiles this file when the Zenzai trait is disabled, so the defect otherwise
# remains hidden.  Remove exactly the obsolete first pair in the ephemeral
# SwiftPM checkout; never mutate Package.swift sources or accept an unknown
# upstream shape silently.
set(_DUPLICATE_BLOCK
"package func ggml_backend_load_all() {}
package func ggml_backend_dev_count() {}

")
file(READ "${_MOCK_FILE}" _MOCK_CONTENT)
string(REGEX MATCHALL "package func ggml_backend_load_all\\(\\) \\{\\}" _LOAD_ALL_MATCHES
    "${_MOCK_CONTENT}"
)
list(LENGTH _LOAD_ALL_MATCHES _LOAD_ALL_COUNT)

if(_LOAD_ALL_COUNT EQUAL 2)
    string(FIND "${_MOCK_CONTENT}" "${_DUPLICATE_BLOCK}" _DUPLICATE_OFFSET)
    if(_DUPLICATE_OFFSET EQUAL -1)
        message(FATAL_ERROR "AzooKey mock duplicates have an unexpected shape")
    endif()
    string(REPLACE "${_DUPLICATE_BLOCK}" "" _PATCHED_CONTENT "${_MOCK_CONTENT}")
    file(WRITE "${_MOCK_FILE}" "${_PATCHED_CONTENT}")
elseif(NOT _LOAD_ALL_COUNT EQUAL 1)
    message(FATAL_ERROR
        "Expected one patched or two upstream ggml_backend_load_all mocks, found ${_LOAD_ALL_COUNT}"
    )
endif()
