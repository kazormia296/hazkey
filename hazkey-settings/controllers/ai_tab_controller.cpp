#include "controllers/ai_tab_controller.h"

#include <QByteArray>
#include <QComboBox>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QLayoutItem>
#include <QMessageBox>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QProgressDialog>
#include <QSignalBlocker>
#include <QUrl>

#include "config_macros.h"
#include "controllers/warning_widget_factory.h"
#include "ui_mainwindow.h"

namespace hazkey::settings {

namespace {
constexpr char kZenzaiExpectedChecksum[] =
    "4de930c06bef8c263aa1aa40684af206db4ce1b96375b3b8ed0ea508e0b14f6c";
}  // namespace

AiTabController::AiTabController(Ui::MainWindow* ui, QWidget* window,
                                 QNetworkAccessManager* networkManager,
                                 QObject* parent)
    : QObject(parent),
      ui_(ui),
      window_(window),
      context_(),
      networkManager_(networkManager),
      currentDownload_(nullptr),
      downloadProgressDialog_(nullptr),
      isLoading_(false) {}

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

void AiTabController::onDownloadZenzaiModel() {
    QString dataHome = qEnvironmentVariable("XDG_DATA_HOME");
    if (dataHome.isEmpty()) {
        dataHome = QDir::homePath() + "/.local/share";
    }

    QString zenzaiDir = dataHome + "/hazkey/zenzai";
    zenzaiModelPath_ = zenzaiDir + "/zenzai.gguf";

    QDir dir;
    if (!dir.mkpath(zenzaiDir)) {
        QMessageBox::critical(
            window_, tr("Download Error"),
            tr("Failed to create directory: %1").arg(zenzaiDir));
        return;
    }

    if (QFile::exists(zenzaiModelPath_)) {
        QMessageBox::StandardButton reply =
            QMessageBox::question(window_, tr("File Exists"),
                                  tr("Overwrite the existing Zenzai model?"),
                                  QMessageBox::Yes | QMessageBox::No);

        if (reply == QMessageBox::No) {
            return;
        }
    }

    downloadProgressDialog_ = new QProgressDialog(
        tr("Downloading Zenzai model..."), tr("Cancel"), 0, 100, window_);
    downloadProgressDialog_->setWindowModality(Qt::WindowModal);
    downloadProgressDialog_->setMinimumDuration(0);
    downloadProgressDialog_->setValue(0);

    connect(downloadProgressDialog_, &QProgressDialog::canceled, this,
            [this]() {
                if (currentDownload_) {
                    currentDownload_->abort();
                }
            });

    QUrl url(
        "https://huggingface.co/Miwa-Keita/zenz-v3.1-small-gguf/resolve/main/"
        "ggml-model-Q5_K_M.gguf");
    QNetworkRequest request(url);

    currentDownload_ = networkManager_->get(request);

    connect(currentDownload_, &QNetworkReply::downloadProgress, this,
            &AiTabController::onDownloadProgress);
    connect(currentDownload_, &QNetworkReply::finished, this,
            &AiTabController::onDownloadFinished);
    connect(currentDownload_,
            QOverload<QNetworkReply::NetworkError>::of(
                &QNetworkReply::errorOccurred),
            this, &AiTabController::onDownloadError);
}

void AiTabController::onDownloadProgress(qint64 bytesReceived,
                                         qint64 bytesTotal) {
    if (downloadProgressDialog_ && bytesTotal > 0) {
        int progress = static_cast<int>((bytesReceived * 100) / bytesTotal);
        downloadProgressDialog_->setValue(progress);

        double receivedMB = bytesReceived / 1024.0 / 1024.0;
        double totalMB = bytesTotal / 1024.0 / 1024.0;
        downloadProgressDialog_->setLabelText(
            tr("Downloading Zenzai model... %1 MB / %2 MB")
                .arg(receivedMB, 0, 'f', 2)
                .arg(totalMB, 0, 'f', 2));
    }
}

void AiTabController::onDownloadFinished() {
    if (!currentDownload_) {
        return;
    }

    if (downloadProgressDialog_) {
        downloadProgressDialog_->deleteLater();
        downloadProgressDialog_ = nullptr;
    }

    if (currentDownload_->error() != QNetworkReply::NoError) {
        currentDownload_->deleteLater();
        currentDownload_ = nullptr;
        return;
    }

    QByteArray downloadedData = currentDownload_->readAll();
    currentDownload_->deleteLater();
    currentDownload_ = nullptr;

    QCryptographicHash hash(QCryptographicHash::Sha256);
    hash.addData(downloadedData);
    QByteArray calculatedHash = hash.result();
    QString calculatedHashHex = calculatedHash.toHex();

    QString expectedHash = kZenzaiExpectedChecksum;

    if (calculatedHashHex != expectedHash) {
        QMessageBox::critical(
            window_, tr("Download Error"),
            tr("Downloaded file verification failed. Checksum mismatch.\n"
               "Expected: %1\nGot: %2")
                .arg(expectedHash)
                .arg(calculatedHashHex));
        return;
    }

    QString tempPath = zenzaiModelPath_ + ".tmp";
    QFile tempFile(tempPath);
    if (!tempFile.open(QIODevice::WriteOnly)) {
        QMessageBox::critical(
            window_, tr("Download Error"),
            tr("Failed to save model file: %1").arg(tempFile.errorString()));
        return;
    }

    if (tempFile.write(downloadedData) == -1) {
        QMessageBox::critical(
            window_, tr("Download Error"),
            tr("Failed to write model file: %1").arg(tempFile.errorString()));
        tempFile.close();
        QFile::remove(tempPath);
        return;
    }

    tempFile.close();

    if (QFile::exists(zenzaiModelPath_)) {
        if (!QFile::remove(zenzaiModelPath_)) {
            QMessageBox::critical(window_, tr("Download Error"),
                                  tr("Failed to remove old model file."));
            QFile::remove(tempPath);
            return;
        }
    }

    if (!QFile::rename(tempPath, zenzaiModelPath_)) {
        QMessageBox::critical(window_, tr("Download Error"),
                              tr("Failed to rename model file."));
        QFile::remove(tempPath);
        return;
    }

    if (context_.server) {
        context_.server->reloadZenzaiModel();
    }

    QMessageBox::information(
        window_, tr("Download Complete"),
        tr("Zenzai model has been downloaded successfully.\n"
           "Please push 'Reload' to refresh the UI."));
}

void AiTabController::onDownloadError(QNetworkReply::NetworkError error) {
    if (downloadProgressDialog_) {
        downloadProgressDialog_->deleteLater();
        downloadProgressDialog_ = nullptr;
    }

    if (!currentDownload_) {
        return;
    }

    QString errorString = currentDownload_->errorString();
    currentDownload_->deleteLater();
    currentDownload_ = nullptr;

    if (error != QNetworkReply::OperationCanceledError) {
        QMessageBox::critical(
            window_, tr("Download Error"),
            tr("Failed to download Zenzai model: %1").arg(errorString));
    }
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
            tr("<b>Warning:</b> Zenzai model not found."), "yellow",
            tr("Download Model"), [this]() { onDownloadZenzaiModel(); });
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
                    tr("The current model is not the latest version."),
                    "lightblue", tr("Download Update"),
                    [this]() { onDownloadZenzaiModel(); });
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
