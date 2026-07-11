if(NOT DEFINED SETTINGS_SOURCE_DIR)
    message(FATAL_ERROR "SETTINGS_SOURCE_DIR is required")
endif()

function(read_settings_file output file_name)
    file(READ "${SETTINGS_SOURCE_DIR}/${file_name}" contents)
    set(${output} "${contents}" PARENT_SCOPE)
endfunction()

function(require_contains contents expected message_text)
    string(FIND "${contents}" "${expected}" position)
    if(position EQUAL -1)
        message(FATAL_ERROR "${message_text}: missing '${expected}'")
    endif()
endfunction()

function(require_absent contents forbidden message_text)
    string(FIND "${contents}" "${forbidden}" position)
    if(NOT position EQUAL -1)
        message(FATAL_ERROR "${message_text}: found '${forbidden}'")
    endif()
endfunction()

read_settings_file(cmake_source "CMakeLists.txt")
require_contains("${cmake_source}" "project(fcitx5-grimodex-settings"
                 "settings project identity must be independent")
require_contains("${cmake_source}" "qt_add_executable(fcitx5-grimodex-settings"
                 "settings executable identity must be independent")
require_contains("${cmake_source}" "install(TARGETS fcitx5-grimodex-settings"
                 "installed settings executable must be independent")
require_absent("${cmake_source}" "Qt6::Network"
               "settings must not link the network stack")
require_absent("${cmake_source}" "LinguistTools Network"
               "settings must not require the Qt network component")

read_settings_file(mainwindow_header "mainwindow.h")
read_settings_file(mainwindow_source "mainwindow.cpp")
read_settings_file(ai_header "controllers/ai_tab_controller.h")
read_settings_file(ai_source "controllers/ai_tab_controller.cpp")
set(product_sources
    "${mainwindow_header}\n${mainwindow_source}\n${ai_header}\n${ai_source}")
foreach(forbidden IN ITEMS
        "QNetworkAccessManager"
        "QNetworkReply"
        "QNetworkRequest"
        "huggingface.co"
        "onDownloadZenzaiModel")
    require_absent("${product_sources}" "${forbidden}"
                   "settings product must not contain a downloader")
endforeach()
require_contains("${ai_source}" "QFileDialog"
                 "settings must support selecting a local model")
require_contains("${ai_source}" "zenzaiModel"
                 "local model selection must use the isolated product path")

read_settings_file(desktop_source "hazkey-settings.desktop.in")
require_contains("${desktop_source}" "Name=Grimodex IME Settings"
                 "desktop entry must use Grimodex branding")
require_contains("${desktop_source}" "Exec=fcitx5-grimodex-settings"
                 "desktop entry must launch the independent executable")

read_settings_file(ui_source "mainwindow.ui")
require_contains("${ui_source}" "Grimodex IME Settings"
                 "window title must use Grimodex branding")
require_contains("${ui_source}" "Grimodex IME"
                 "about page must use Grimodex branding")
require_contains("${ui_source}" "Hazkey"
                 "about page must preserve upstream attribution")
require_contains("${ui_source}" "$XDG_CONFIG_HOME/fcitx5-grimodex"
                 "custom settings paths must be product-scoped")
