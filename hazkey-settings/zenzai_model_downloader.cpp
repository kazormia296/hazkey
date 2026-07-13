#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QSaveFile>
#include <QTimer>
#include <QUrl>

#include <cstdio>
#include <memory>
#include <utility>

#include "constants.h"

namespace {

constexpr qint64 kMaximumModelBytes = 256LL * 1024LL * 1024LL;
constexpr int kDownloadTimeoutMilliseconds = 15 * 60 * 1000;

QString defaultModelPath() {
    const QString home = QDir::homePath();
    const QByteArray configuredDataHome = qgetenv("XDG_DATA_HOME");
    const QString dataHome = configuredDataHome.isEmpty()
                                 ? QDir(home).filePath(".local/share")
                                 : QString::fromUtf8(configuredDataHome);
    return QDir(dataHome).filePath(
        "fcitx5-grimodex/zenzai/zenzai.gguf");
}

QString requestedOutputPath(const QCoreApplication& application) {
    const QStringList arguments = application.arguments();
    if (arguments.size() == 1) {
        return defaultModelPath();
    }
    if (arguments.size() == 3 && arguments.at(1) == "--output" &&
        !arguments.at(2).isEmpty()) {
        return QFileInfo(arguments.at(2)).absoluteFilePath();
    }

    std::fprintf(stderr, "usage: %s [--output PATH]\n",
                 application.applicationName().toUtf8().constData());
    return QString();
}

bool hasExpectedModel(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        return false;
    }

    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&file)) {
        return false;
    }
    return QString::fromLatin1(hash.result().toHex()) ==
           QString::fromLatin1(GRIMODEX_ZENZAI_MODEL_SHA256);
}

void printError(const QString& message) {
    std::fprintf(stderr, "fcitx5-grimodex-model: %s\n",
                 message.toUtf8().constData());
}

class ModelDownloader final : public QObject {
   public:
    explicit ModelDownloader(QString outputPath, QObject* parent = nullptr)
        : QObject(parent), outputPath_(std::move(outputPath)) {
        urls_ = {
            QString::fromLatin1(GRIMODEX_ZENZAI_MODEL_URL),
            QString::fromLatin1(GRIMODEX_ZENZAI_MODEL_FALLBACK_URL),
        };
        timeout_.setSingleShot(true);
        connect(&timeout_, &QTimer::timeout, this,
                [this]() { onTimeout(); });
    }

    int run() {
        QTimer::singleShot(0, this, [this]() { startAttempt(); });
        return QCoreApplication::exec();
    }

   private:
    void startAttempt() {
        if (attempt_ >= urls_.size()) {
            finish(1, "no model download source remains");
            return;
        }

        attemptFailed_ = false;
        failureReason_.clear();
        receivedBytes_ = 0;
        hash_.reset();
        destination_ = std::make_unique<QSaveFile>(outputPath_);
        if (!destination_->open(QIODevice::WriteOnly)) {
            finish(1, QString("failed to open %1: %2")
                              .arg(outputPath_, destination_->errorString()));
            return;
        }

        QNetworkRequest request(QUrl(urls_.at(attempt_)));
        request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                             QNetworkRequest::NoLessSafeRedirectPolicy);
        reply_ = networkManager_.get(request);
        connect(reply_, &QNetworkReply::readyRead, this,
                [this]() { onReadyRead(); });
        connect(reply_, &QNetworkReply::finished, this,
                [this]() { onFinished(); });
        timeout_.start(kDownloadTimeoutMilliseconds);
    }

    void onReadyRead() {
        if (reply_ == nullptr || attemptFailed_) {
            return;
        }
        appendChunk(reply_->readAll());
    }

    void onFinished() {
        if (reply_ == nullptr) {
            return;
        }

        QNetworkReply* reply = reply_;
        timeout_.stop();
        if (!attemptFailed_) {
            appendChunk(reply->readAll());
        }

        const int statusCode =
            reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (!attemptFailed_ && statusCode == 404 &&
            attempt_ + 1 < urls_.size()) {
            retryWithFallback(reply);
            return;
        }

        if (!attemptFailed_ && reply->error() != QNetworkReply::NoError) {
            recordFailure(reply->errorString());
        }
        if (!attemptFailed_ && (statusCode < 200 || statusCode >= 300)) {
            recordFailure(QString("server returned HTTP %1").arg(statusCode));
        }
        if (!attemptFailed_ &&
            QString::fromLatin1(hash_.result().toHex()) !=
                QString::fromLatin1(GRIMODEX_ZENZAI_MODEL_SHA256)) {
            recordFailure("downloaded model checksum did not match");
        }

        if (attemptFailed_) {
            finish(1, failureReason_);
            return;
        }
        if (!destination_->commit()) {
            finish(1, destination_->errorString().isEmpty()
                         ? "failed to install downloaded model"
                         : destination_->errorString());
            return;
        }

        reply_ = nullptr;
        reply->deleteLater();
        destination_.reset();
        completed_ = true;
        QCoreApplication::exit(0);
    }

    void appendChunk(const QByteArray& chunk) {
        if (attemptFailed_ || chunk.isEmpty()) {
            return;
        }
        receivedBytes_ += chunk.size();
        if (receivedBytes_ > kMaximumModelBytes) {
            failCurrent("download exceeded the maximum model size");
            return;
        }
        if (destination_->write(chunk) != chunk.size()) {
            failCurrent(destination_->errorString().isEmpty()
                            ? "failed to write downloaded model"
                            : destination_->errorString());
            return;
        }
        hash_.addData(chunk);
    }

    void failCurrent(const QString& reason) {
        recordFailure(reason);
        if (reply_ != nullptr) {
            reply_->abort();
        }
    }

    void recordFailure(const QString& reason) {
        if (!attemptFailed_) {
            attemptFailed_ = true;
            failureReason_ = reason;
        }
    }

    void retryWithFallback(QNetworkReply* reply) {
        reply_ = nullptr;
        reply->deleteLater();
        destination_->cancelWriting();
        destination_.reset();
        ++attempt_;
        QTimer::singleShot(0, this, [this]() { startAttempt(); });
    }

    void onTimeout() {
        if (reply_ != nullptr && !attemptFailed_) {
            failCurrent("download timed out");
        }
    }

    void finish(int exitCode, const QString& error) {
        if (completed_) {
            return;
        }
        completed_ = true;
        timeout_.stop();
        if (reply_ != nullptr) {
            reply_->deleteLater();
            reply_ = nullptr;
        }
        if (destination_ != nullptr) {
            destination_->cancelWriting();
            destination_.reset();
        }
        if (!error.isEmpty()) {
            printError(error);
        }
        QCoreApplication::exit(exitCode);
    }

    QString outputPath_;
    QStringList urls_;
    int attempt_ = 0;
    QNetworkAccessManager networkManager_;
    QNetworkReply* reply_ = nullptr;
    std::unique_ptr<QSaveFile> destination_;
    QCryptographicHash hash_{QCryptographicHash::Sha256};
    QTimer timeout_;
    qint64 receivedBytes_ = 0;
    bool attemptFailed_ = false;
    bool completed_ = false;
    QString failureReason_;
};

}  // namespace

int main(int argc, char* argv[]) {
    QCoreApplication application(argc, argv);
    application.setApplicationName("fcitx5-grimodex-model");

    const QString outputPath = requestedOutputPath(application);
    if (outputPath.isEmpty()) {
        return 2;
    }
    if (hasExpectedModel(outputPath)) {
        return 0;
    }

    const QFileInfo outputInfo(outputPath);
    if (!QDir().mkpath(outputInfo.absolutePath())) {
        printError(QString("failed to create %1").arg(outputInfo.absolutePath()));
        return 1;
    }

    ModelDownloader downloader(outputPath, &application);
    return downloader.run();
}
