#include "controllers/ai_tab_controller.h"

#include <unistd.h>

#include <cstdlib>

#include <QComboBox>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QLabel>
#include <QLayoutItem>
#include <QMessageBox>
#include <QSaveFile>
#include <QSignalBlocker>

#include "config_macros.h"
#include "controllers/warning_widget_factory.h"
#include "settings_product_paths.h"
#include "ui_mainwindow.h"

namespace hazkey::settings {

namespace {
constexpr char kZenzaiExpectedChecksum[] =
    "4de930c06bef8c263aa1aa40684af206db4ce1b96375b3b8ed0ea508e0b14f6c";
}  // namespace

AiTabController::AiTabController(Ui::MainWindow* ui, QWidget* window,
                                 QObject* parent)
    : QObject(parent),
      ui_(ui),
      window_(window),
      grimodexDiagnosticsLabel_(new QLabel(window)),
      context_(),
      isLoading_(false) {
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

void AiTabController::loadFromConfig() {
    if (!context_.currentProfile || !context_.currentConfig) return;

    isLoading_.store(true);
    const QSignalBlocker deviceBlocker(ui_->zenzaiBackendDevice);
    const QSignalBlocker scopeBlocker(ui_->grimodexScope);

    refreshWarnings();
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
               "GGUF file at <code>%1</code>, or select a local file.")
                .arg(managedZenzaiModelPath()),
            "yellow", tr("Select Local Model"),
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
            QString expectedChecksum = kZenzaiExpectedChecksum;

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
    if (!currentDevice.isEmpty()) {
        int index = ui_->zenzaiBackendDevice->findData(currentDevice);
        if (index >= 0) {
            ui_->zenzaiBackendDevice->setCurrentIndex(index);
        }
    }
}

}  // namespace hazkey::settings
