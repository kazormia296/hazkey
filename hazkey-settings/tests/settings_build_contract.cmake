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
require_contains("${cmake_source}" "Qt6::Network"
                 "the packaged model downloader must link the Qt network component")
require_contains("${cmake_source}" "fcitx5-grimodex-model"
                 "the packaged model downloader must be installed")

read_settings_file(mainwindow_header "mainwindow.h")
read_settings_file(mainwindow_source "mainwindow.cpp")
read_settings_file(server_connector_source "serverconnector.cpp")
read_settings_file(ai_header "controllers/ai_tab_controller.h")
read_settings_file(ai_source "controllers/ai_tab_controller.cpp")
read_settings_file(tab_context_header "controllers/tab_context.h")
read_settings_file(warning_widget_source "controllers/warning_widget_factory.cpp")
set(product_sources
    "${mainwindow_header}\n${mainwindow_source}\n${ai_header}\n${ai_source}")
foreach(forbidden IN ITEMS
        "QNetworkAccessManager"
        "QNetworkReply"
        "QNetworkRequest"
        "huggingface.co")
    require_absent("${product_sources}" "${forbidden}"
                   "settings product must not contain a downloader")
endforeach()
require_contains("${product_sources}" "onDownloadZenzaiModel"
                 "settings must expose a model download fallback")
require_contains("${product_sources}" "QProcess"
                 "settings must launch the isolated model downloader")
require_contains("${server_connector_source}" "throw std::runtime_error"
                 "settings save failures must reach the UI layer")
require_contains("${mainwindow_source}" "if (saveCurrentConfig())"
                 "the settings window must close only after a successful save")
require_contains("${mainwindow_source}" "QMessageBox::critical"
                 "the settings window must display save failures")
require_contains("${ai_source}" "QFileDialog"
                 "settings must support selecting a local model")
require_contains("${ai_source}" "zenzaiModel"
                 "local model selection must use the isolated product path")
require_contains("${ai_source}" "context_.reloadConfiguration"
                 "model download completion must refresh the settings UI")
require_contains("${tab_context_header}" "reloadConfiguration"
                 "tab context must expose settings refresh callback")
require_contains("${warning_widget_source}" "QPalette::WindowText"
                 "warning text must set an explicit readable palette color")
require_contains("${warning_widget_source}" "lightnessF"
                 "warning text color must adapt to the highlight background")
require_contains("${ai_header}" "refreshGrimodexDiagnostics"
                 "settings must expose a Grimodex diagnostics presenter")
require_contains("${ai_header}" "refreshZenzaiRuntimeDiagnostics"
                 "settings must expose a Zenzai runtime diagnostics presenter")
require_contains("${ai_source}" "Refresh diagnostics"
                 "settings must expose an explicit Zenzai diagnostics refresh action")
require_contains("${ai_source}" "context_.server->getConfig()"
                 "Zenzai diagnostics refresh must fetch a current server snapshot")
require_contains("${ai_source}" "mutable_zenzai_runtime_diagnostics()->CopyFrom"
                 "Zenzai diagnostics refresh must preserve unsaved profile edits")
require_contains("${ai_source}" "Failed to refresh Zenzai runtime diagnostics"
                 "Zenzai diagnostics refresh failures must be visible")
foreach(required IN ITEMS
        "has_zenzai_runtime_diagnostics"
        "model_load_verified"
        "zenzai_enabled_request_count"
        "model_load_failure_count"
        "last_zenzai_request_unix_millis"
        "does not expose per-candidate AI"
        "status")
    require_contains("${ai_source}" "${required}"
                     "settings Zenzai diagnostics must display ${required}")
endforeach()
foreach(required IN ITEMS
        "has_grimodex_diagnostics"
        "watcher_active"
        "consumer_registered"
        "snapshot_status"
        "active_project_id"
        "active_sessions"
        "program"
        "frontend"
        "scope_reason")
    require_contains("${ai_source}" "${required}"
                     "settings diagnostics must display ${required}")
endforeach()

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
