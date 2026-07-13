#include "controllers/ai_tab_controller.h"

#include <unistd.h>

#include <cstdlib>

#include <QComboBox>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QLabel>
#include <QLayoutItem>
#include <QMessageBox>
#include <QProcess>
#include <QPushButton>
#include <QSaveFile>
#include <QSignalBlocker>

#include "config_macros.h"
#include "constants.h"
#include "controllers/warning_widget_factory.h"
#include "settings_product_paths.h"
#include "ui_mainwindow.h"

namespace hazkey::settings {

AiTabController::AiTabController(Ui::MainWindow* ui, QWidget* window,
                                 QObject* parent)
    : QObject(parent),
      ui_(ui),
      window_(window),
      zenzaiDiagnosticsLabel_(new QLabel(window)),
      zenzaiDiagnosticsRefreshButton_(
          new QPushButton(tr("Refresh diagnostics"), window)),
      grimodexDiagnosticsLabel_(new QLabel(window)),
      context_(),
      isLoading_(false) {
    zenzaiDiagnosticsLabel_->setWordWrap(true);
    zenzaiDiagnosticsLabel_->setTextFormat(Qt::RichText);
    zenzaiDiagnosticsLabel_->setTextInteractionFlags(
        Qt::TextSelectableByMouse);
    zenzaiDiagnosticsLabel_->setObjectName("zenzaiRuntimeDiagnostics");
    ui_->zenzaiGrid->addWidget(zenzaiDiagnosticsLabel_, 5, 0, 1, 2);
    zenzaiDiagnosticsRefreshButton_->setObjectName(
        "refreshZenzaiRuntimeDiagnostics");
    ui_->zenzaiGrid->addWidget(zenzaiDiagnosticsRefreshButton_, 6, 0, 1, 2);

    grimodexDiagnosticsLabel_->setWordWrap(true);
    grimodexDiagnosticsLabel_->setTextFormat(Qt::RichText);
    grimodexDiagnosticsLabel_->setTextInteractionFlags(
        Qt::TextSelectableByMouse);
    grimodexDiagnosticsLabel_->setObjectName("grimodexDiagnostics");
    ui_->grimodexIntegrationGrid->addWidget(grimodexDiagnosticsLabel_, 1, 0, 1,
                                            2);
}

void AiTabController::setContext(const TabContext& context) {
    context_ = context;
}

void AiTabController::connectSignals() {
    connect(zenzaiDiagnosticsRefreshButton_, &QPushButton::clicked, this,
            &AiTabController::onRefreshZenzaiRuntimeDiagnostics);
    connect(ui_->grimodexScope, &QComboBox::currentIndexChanged, this,
            [this](int) {
                if (isLoading_.load()) {
                    return;
                }
                saveToConfig();
            });
    connect(ui_->zenzaiBackendDevice, &QComboBox::currentIndexChanged, this,
            [this](int) {
                if (isLoading_.load()) {
                    return;
                }
                saveToConfig();
            });
}

void AiTabController::onRefreshZenzaiRuntimeDiagnostics() {
    if (!context_.server || !context_.currentConfig) {
        zenzaiDiagnosticsLabel_->setText(
            tr("<b>Failed to refresh Zenzai runtime diagnostics.</b> "
               "The settings connection is unavailable."));
        return;
    }

    const auto latestConfig = context_.server->getConfig();
    if (!latestConfig.has_value()) {
        zenzaiDiagnosticsLabel_->setText(
            tr("<b>Failed to refresh Zenzai runtime diagnostics.</b> "
               "Check the connection to the Grimodex IME service and try "
               "again."));
        return;
    }

    // Update only the runtime diagnostic snapshot. In particular, keep the
    // profiles owned by the settings window intact so unsaved edits survive a
    // diagnostic refresh.
    if (latestConfig->has_zenzai_runtime_diagnostics()) {
        context_.currentConfig->mutable_zenzai_runtime_diagnostics()->CopyFrom(
            latestConfig->zenzai_runtime_diagnostics());
    } else {
        context_.currentConfig->clear_zenzai_runtime_diagnostics();
    }
    refreshZenzaiRuntimeDiagnostics();
}

void AiTabController::loadFromConfig() {
    if (!context_.currentProfile || !context_.currentConfig) return;

    isLoading_.store(true);
    const QSignalBlocker deviceBlocker(ui_->zenzaiBackendDevice);
    const QSignalBlocker scopeBlocker(ui_->grimodexScope);

    refreshWarnings();
    refreshZenzaiRuntimeDiagnostics();
    populateGrimodexScopeList();
    updateGrimodexScopeFromProfile();
    refreshGrimodexDiagnostics();
    populateDeviceList();
    updateSelectionFromProfile();

    SET_SPINBOX(ui_->zenzaiInferenceLimit,
                context_.currentProfile->zenzai_infer_limit(),
                ConfigDefs::SpinboxDefaults::ZENZAI_INFERENCE_LIMIT);
    SET_CHECKBOX(ui_->enableZenzai, context_.currentProfile->zenzai_enable(),
                 ConfigDefs::CheckboxDefaults::ENABLE_ZENZAI);
    SET_CHECKBOX(ui_->zenzaiContextualConversion,
                 context_.currentProfile->zenzai_contextual_mode(),
                 ConfigDefs::CheckboxDefaults::ZENZAI_CONTEXTUAL);

    SET_LINEEDIT(ui_->zenzaiUserPlofile,
                 context_.currentProfile->zenzai_profile(), "");

    isLoading_.store(false);
}

void AiTabController::saveToConfig() {
    if (!context_.currentProfile) return;

    context_.currentProfile->set_zenzai_infer_limit(
        GET_SPINBOX_INT(ui_->zenzaiInferenceLimit));
    context_.currentProfile->set_zenzai_enable(
        GET_CHECKBOX_BOOL(ui_->enableZenzai));
    context_.currentProfile->set_zenzai_contextual_mode(
        GET_CHECKBOX_BOOL(ui_->zenzaiContextualConversion));
    context_.currentProfile->set_zenzai_profile(
        GET_LINEEDIT_STRING(ui_->zenzaiUserPlofile));

    context_.currentProfile->set_grimodex_scope_mode(
        static_cast<::hazkey::config::Profile_GrimodexScopeMode>(
            ui_->grimodexScope->currentData().toInt()));

    const QString selectedDevice =
        ui_->zenzaiBackendDevice->currentData().toString();
    context_.currentProfile->set_zenzai_backend_device_name(
        selectedDevice.toStdString());
}

void AiTabController::onSelectLocalZenzaiModel() {
    const QString selectedPath = QFileDialog::getOpenFileName(
        window_, tr("Select a local Zenzai model"), QDir::homePath(),
        tr("GGUF model (*.gguf);;All files (*)"));
    if (selectedPath.isEmpty()) {
        return;
    }

    const QString targetPath = managedZenzaiModelPath();
    const QFileInfo targetInfo(targetPath);
    if (!QDir().mkpath(targetInfo.absolutePath())) {
        QMessageBox::critical(
            window_, tr("Model Import Error"),
            tr("Failed to create directory: %1")
                .arg(targetInfo.absolutePath()));
        return;
    }

    const QFileInfo selectedInfo(selectedPath);
    if (selectedInfo.canonicalFilePath() != targetInfo.canonicalFilePath()) {
        QFile source(selectedPath);
        if (!source.open(QIODevice::ReadOnly)) {
            QMessageBox::critical(
                window_, tr("Model Import Error"),
                tr("Failed to open model file: %1").arg(source.errorString()));
            return;
        }

        QSaveFile destination(targetPath);
        if (!destination.open(QIODevice::WriteOnly)) {
            QMessageBox::critical(
                window_, tr("Model Import Error"),
                tr("Failed to prepare model file: %1")
                    .arg(destination.errorString()));
            return;
        }

        while (!source.atEnd()) {
            const QByteArray chunk = source.read(1024 * 1024);
            if (chunk.isEmpty() && source.error() != QFileDevice::NoError) {
                destination.cancelWriting();
                QMessageBox::critical(
                    window_, tr("Model Import Error"),
                    tr("Failed to read model file: %1")
                        .arg(source.errorString()));
                return;
            }
            if (destination.write(chunk) != chunk.size()) {
                destination.cancelWriting();
                QMessageBox::critical(
                    window_, tr("Model Import Error"),
                    tr("Failed to write model file: %1")
                        .arg(destination.errorString()));
                return;
            }
        }

        if (!destination.commit()) {
            QMessageBox::critical(
                window_, tr("Model Import Error"),
                tr("Failed to install model file: %1")
                    .arg(destination.errorString()));
            return;
        }
    }

    const bool reloaded =
        context_.server != nullptr && context_.server->reloadZenzaiModel();
    if (!reloaded) {
        QMessageBox::warning(
            window_, tr("Model Installed"),
            tr("The local model was installed at %1, but the Grimodex IME "
               "service could not reload it. Restart the input method or "
               "select Reload after the service starts.")
                .arg(targetPath));
        return;
    }

    QMessageBox::information(
        window_, tr("Model Installed"),
        tr("The local Zenzai model was installed at %1. Select Reload to "
           "refresh this window.")
            .arg(targetPath));
}

void AiTabController::onDownloadZenzaiModel() {
    const QString helperPath = QString::fromLatin1(
        GRIMODEX_ZENZAI_MODEL_HELPER_PATH);
    if (!QFileInfo::exists(helperPath)) {
        QMessageBox::critical(
            window_, tr("Model Download Error"),
            tr("The Zenzai model downloader is not installed: %1")
                .arg(helperPath));
        return;
    }

    auto* process = new QProcess(this);
    connect(process,
            qOverload<int, QProcess::ExitStatus>(&QProcess::finished), this,
            [this, process](int exitCode, QProcess::ExitStatus) {
                const QString details = QString::fromLocal8Bit(
                    process->readAllStandardError());
                process->deleteLater();

                if (exitCode != 0) {
                    QMessageBox::critical(
                        window_, tr("Model Download Error"),
                        tr("Failed to download the tested Zenzai model.%1")
                            .arg(details.isEmpty() ? QString()
                                                   : "\n" + details));
                    return;
                }

                const bool serviceReloaded =
                    context_.server != nullptr &&
                    context_.server->reloadZenzaiModel();
                bool configurationReloaded = false;
                if (serviceReloaded) {
                    configurationReloaded = context_.reloadConfiguration
                                                ? context_.reloadConfiguration()
                                                : (loadFromConfig(), true);
                }
                if (!serviceReloaded || !configurationReloaded) {
                    QMessageBox::warning(
                        window_, tr("Model Downloaded"),
                        tr("The Zenzai model was downloaded, but the IME "
                           "service or settings window could not reload it. "
                           "Restart the input method or select Reload."));
                    return;
                }
                QMessageBox::information(
                    window_, tr("Model Downloaded"),
                    tr("The tested Zenzai model is ready to use."));
            });
    process->start(helperPath);
    if (!process->waitForStarted(1000)) {
        const QString error = process->errorString();
        process->deleteLater();
        QMessageBox::critical(
            window_, tr("Model Download Error"),
            tr("Failed to start the Zenzai model downloader: %1").arg(error));
        return;
    }

    QMessageBox::information(
        window_, tr("Downloading Zenzai Model"),
        tr("The tested Zenzai model is being downloaded. This window will "
           "show the result when it finishes."));
}

QString AiTabController::calculateFileSHA256(const QString& filePath) {
    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly)) {
        return QString();
    }

    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file)) {
        file.close();
        return QString();
    }

    file.close();
    return QString(hash.result().toHex());
}

QString AiTabController::managedZenzaiModelPath() const {
    using grimodex::ime::settings::SettingsEnvironment;
    using grimodex::ime::settings::resolveSettingsProductPaths;

    const SettingsEnvironment environment{
        .runtimeHome = std::getenv("XDG_RUNTIME_DIR"),
        .configHome = std::getenv("XDG_CONFIG_HOME"),
        .dataHome = std::getenv("XDG_DATA_HOME"),
        .stateHome = std::getenv("XDG_STATE_HOME"),
        .cacheHome = std::getenv("XDG_CACHE_HOME"),
    };
    const auto paths = resolveSettingsProductPaths(
        environment, QDir::homePath().toStdString(), getuid());
    return QString::fromStdString(paths.zenzaiModel);
}

void AiTabController::refreshWarnings() {
    if (ui_->aiTabScrollContentsLayout->count() > 1) {
        QLayoutItem* item = ui_->aiTabScrollContentsLayout->itemAt(1);
        if (item && item->widget()) {
            QWidget* widget = item->widget();
            if (widget->styleSheet().contains("background-color: yellow") ||
                widget->styleSheet().contains("background-color: lightblue")) {
                ui_->aiTabScrollContentsLayout->removeWidget(widget);
                widget->deleteLater();
            }
        }
    }

    if (context_.currentConfig->available_zenzai_backend_devices_size() <= 0) {
        ui_->enableZenzai->setEnabled(false);
        ui_->zenzaiContextualConversion->setEnabled(false);
        ui_->zenzaiInferenceLimit->setEnabled(false);
        ui_->zenzaiUserPlofile->setEnabled(false);
        ui_->zenzaiBackendDevice->setEnabled(false);

        QWidget* warningWidget = WarningWidgetFactory::create(
            tr("<b>Warning:</b> Zenzai support not installed."), "yellow");
        ui_->aiTabScrollContentsLayout->insertWidget(1, warningWidget);
    } else if (!context_.currentConfig->zenzai_model_available()) {
        ui_->enableZenzai->setEnabled(false);
        ui_->zenzaiContextualConversion->setEnabled(false);
        ui_->zenzaiInferenceLimit->setEnabled(false);
        ui_->zenzaiUserPlofile->setEnabled(false);
        ui_->zenzaiBackendDevice->setEnabled(false);

        QWidget* warningWidget = WarningWidgetFactory::create(
            tr("<b>Warning:</b> Zenzai model not found. Place a compatible "
               "GGUF file at <code>%1</code>, download the tested model, or "
               "select a local file.")
                .arg(managedZenzaiModelPath()),
            "yellow", tr("Download Tested Model"),
            [this]() { onDownloadZenzaiModel(); },
            tr("Select Local Model"),
            [this]() { onSelectLocalZenzaiModel(); });
        ui_->aiTabScrollContentsLayout->insertWidget(1, warningWidget);
    } else {
        ui_->enableZenzai->setEnabled(true);
        ui_->zenzaiContextualConversion->setEnabled(true);
        ui_->zenzaiInferenceLimit->setEnabled(true);
        ui_->zenzaiUserPlofile->setEnabled(true);
        ui_->zenzaiBackendDevice->setEnabled(true);

        QString modelPath =
            QString::fromStdString(context_.currentConfig->zenzai_model_path());
        if (!modelPath.isEmpty()) {
            QString currentChecksum = calculateFileSHA256(modelPath);
            const QString expectedChecksum =
                QString::fromLatin1(GRIMODEX_ZENZAI_MODEL_SHA256);

            if (!currentChecksum.isEmpty() &&
                currentChecksum != expectedChecksum) {
                QWidget* warningWidget = WarningWidgetFactory::create(
                    tr("The current model differs from the tested Zenzai "
                       "model."),
                    "lightblue", tr("Select Local Model"),
                    [this]() { onSelectLocalZenzaiModel(); });
                ui_->aiTabScrollContentsLayout->insertWidget(1, warningWidget);
            }
        }
    }
}

void AiTabController::populateDeviceList() {
    ui_->zenzaiBackendDevice->clear();
    ui_->zenzaiBackendDevice->addItem(tr("Automatic (GPU preferred)"),
                                      QString());
    for (int i = 0;
         i < context_.currentConfig->available_zenzai_backend_devices_size();
         ++i) {
        const auto& device =
            context_.currentConfig->available_zenzai_backend_devices(i);
        QString deviceName = QString::fromStdString(device.name());
        QString deviceDesc = QString::fromStdString(device.desc());
        QString displayText = deviceName;
        if (!deviceDesc.isEmpty()) {
            displayText += " : " + deviceDesc;
        }
        ui_->zenzaiBackendDevice->addItem(displayText, deviceName);
    }
}

void AiTabController::populateGrimodexScopeList() {
    ui_->grimodexScope->clear();
    ui_->grimodexScope->addItem(
        tr("Grimodex only (recommended)"),
        ::hazkey::config::Profile_GrimodexScopeMode_GRIMODEX_ONLY);
    ui_->grimodexScope->addItem(
        tr("Off"), ::hazkey::config::Profile_GrimodexScopeMode_GRIMODEX_OFF);
    ui_->grimodexScope->addItem(
        tr("All applications"),
        ::hazkey::config::
            Profile_GrimodexScopeMode_GRIMODEX_ALL_APPLICATIONS);
}

void AiTabController::updateGrimodexScopeFromProfile() {
    const int index = ui_->grimodexScope->findData(
        context_.currentProfile->grimodex_scope_mode());
    ui_->grimodexScope->setCurrentIndex(index >= 0 ? index : 0);
}

QString AiTabController::zenzaiRuntimeStatusText(
    ::hazkey::config::ZenzaiRuntimeDiagnostics_Status status) const {
    using Diagnostics = ::hazkey::config::ZenzaiRuntimeDiagnostics;
    switch (status) {
        case Diagnostics::READY:
            return tr("Ready; no Zenzai-enabled conversion request has been observed yet");
        case Diagnostics::MODEL_LOAD_VERIFIED:
            return tr("Model loaded; the latest Zenzai-enabled request reached the converter");
        case Diagnostics::PROFILE_DISABLED:
            return tr("Disabled in the current profile");
        case Diagnostics::POLICY_DISABLED:
            return tr("Disabled by input security policy");
        case Diagnostics::BACKEND_UNAVAILABLE:
            return tr("Zenzai backend is unavailable");
        case Diagnostics::MODEL_MISSING:
            return tr("Zenzai model file is missing");
        case Diagnostics::MODEL_LOAD_FAILED:
            return tr("Model loading failed for the latest request");
        case Diagnostics::STATUS_UNSPECIFIED:
        default:
            return tr("Unknown");
    }
}

void AiTabController::refreshZenzaiRuntimeDiagnostics() {
    if (!context_.currentConfig ||
        !context_.currentConfig->has_zenzai_runtime_diagnostics()) {
        zenzaiDiagnosticsLabel_->setText(
            tr("<b>Zenzai runtime diagnostics unavailable.</b> Restart the "
               "Grimodex IME service after updating it."));
        return;
    }

    const auto& diagnostics =
        context_.currentConfig->zenzai_runtime_diagnostics();
    const QString status =
        zenzaiRuntimeStatusText(diagnostics.status()).toHtmlEscaped();
    const QString loaded =
        diagnostics.model_load_verified() ? tr("yes") : tr("no");
    const QString requests = QString::number(
        static_cast<qulonglong>(diagnostics.zenzai_enabled_request_count()));
    const QString loadFailures = QString::number(
        static_cast<qulonglong>(diagnostics.model_load_failure_count()));
    const QString last = diagnostics.has_last_zenzai_request_unix_millis()
        ? QDateTime::fromMSecsSinceEpoch(static_cast<qint64>(
              diagnostics.last_zenzai_request_unix_millis()))
              .toLocalTime()
              .toString(Qt::ISODate)
        : tr("never");
    const QString detail = diagnostics.detail().empty()
        ? tr("none")
        : QString::fromStdString(diagnostics.detail()).toHtmlEscaped();

    QString text = tr("<b>Zenzai runtime diagnostics</b><br/>"
                      "Status: %1<br/>"
                      "Model load verified: %2 / Zenzai-enabled requests: %3 / "
                      "Model-load failures: %4<br/>"
                      "Last Zenzai-enabled request: %5<br/>Detail: %6<br/>"
                      "Note: the converter does not expose per-candidate AI "
                      "evaluation results.");
    text = text.arg(status)
               .arg(loaded.toHtmlEscaped())
               .arg(requests)
               .arg(loadFailures)
               .arg(last.toHtmlEscaped())
               .arg(detail);
    zenzaiDiagnosticsLabel_->setText(text);
}

QString AiTabController::grimodexScopeReasonText(
    ::hazkey::config::GrimodexDiagnostics_ScopeReason reason) const {
    using Diagnostics = ::hazkey::config::GrimodexDiagnostics;
    switch (reason) {
        case Diagnostics::ALLOWED_GRIMODEX:
            return tr("Allowed: current application is Grimodex");
        case Diagnostics::ALLOWED_ALL_APPLICATIONS:
            return tr("Allowed: all-applications mode is enabled");
        case Diagnostics::DISABLED:
            return tr("Blocked: Grimodex integration is off");
        case Diagnostics::SECURE_INPUT:
            return tr("Blocked: secure input is active");
        case Diagnostics::UNKNOWN_PROGRAM:
            return tr("Blocked: application identity is unavailable");
        case Diagnostics::OTHER_PROGRAM:
            return tr("Blocked: current application is not Grimodex");
        case Diagnostics::SCOPE_REASON_UNSPECIFIED:
        default:
            return tr("No active input context");
    }
}

void AiTabController::refreshGrimodexDiagnostics() {
    if (!context_.currentConfig ||
        !context_.currentConfig->has_grimodex_diagnostics()) {
        grimodexDiagnosticsLabel_->setText(
            tr("<b>Diagnostics unavailable.</b> Restart the Grimodex IME "
               "service after updating it."));
        return;
    }

    const auto& diagnostics = context_.currentConfig->grimodex_diagnostics();
    const QString watcher =
        diagnostics.watcher_active() ? tr("active") : tr("stopped");
    const QString consumer =
        diagnostics.consumer_registered() ? tr("registered")
                                          : tr("not registered");
    const QString project = diagnostics.has_active_project_id()
        ? QString::fromStdString(diagnostics.active_project_id()).toHtmlEscaped()
        : tr("none");
    const QString program = diagnostics.has_program()
        ? QString::fromStdString(diagnostics.program()).toHtmlEscaped()
        : tr("unknown");
    const QString frontend = diagnostics.has_frontend()
        ? QString::fromStdString(diagnostics.frontend()).toHtmlEscaped()
        : tr("unknown");
    const QString snapshot =
        QString::fromStdString(diagnostics.snapshot_status()).toHtmlEscaped();
    const QString integration = diagnostics.integration_allowed()
        ? tr("enabled for current context")
        : tr("disabled for current context");
    const QString secure = diagnostics.secure_input() ? tr("yes") : tr("no");
    const QString reason =
        grimodexScopeReasonText(diagnostics.scope_reason()).toHtmlEscaped();

    QString text = tr("<b>Runtime diagnostics</b><br/>"
                      "Watcher: %1 / Consumer: %2<br/>"
                      "Snapshot: %3 / Generation: %4 / Project: %5<br/>"
                      "Sessions: %6 / Program: %7 / Frontend: %8 / Secure: %9<br/>"
                      "Integration: %10<br/>Reason: %11");
    text = text.arg(watcher.toHtmlEscaped())
               .arg(consumer.toHtmlEscaped())
               .arg(snapshot)
               .arg(QString::number(diagnostics.generation()))
               .arg(project)
               .arg(QString::number(diagnostics.active_sessions()))
               .arg(program)
               .arg(frontend)
               .arg(secure.toHtmlEscaped())
               .arg(integration.toHtmlEscaped())
               .arg(reason);
    grimodexDiagnosticsLabel_->setText(text);
}

void AiTabController::updateSelectionFromProfile() {
    QString currentDevice = QString::fromStdString(
        context_.currentProfile->zenzai_backend_device_name());
    int index = ui_->zenzaiBackendDevice->findData(currentDevice);
    if (index < 0) {
        // An explicitly configured device disappeared. Runtime selection also
        // falls back to CPU rather than silently choosing a different GPU.
        index = ui_->zenzaiBackendDevice->findData(QStringLiteral("CPU"));
    }
    if (index >= 0) {
        ui_->zenzaiBackendDevice->setCurrentIndex(index);
    }
}

}  // namespace hazkey::settings
