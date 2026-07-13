#include "hazkey_server_connector.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <fcitx-utils/log.h>
#include <fcitx-utils/misc.h>

#include "base.pb.h"
#include "commands.pb.h"
#include "grimodex_product_identity.h"
#include "hazkey_socket_io.h"

namespace {

std::mutex transactMutex;

constexpr auto bestEffortBudget = std::chrono::milliseconds(250);

}  // namespace

HazkeyServerConnector::HazkeyServerConnector()
    : sessionClient_([this](const hazkey::RequestEnvelope& request,
                            bool tryConnect) {
          return transact(request, tryConnect, HazkeyTransportPolicy::normal);
      },
      [this](const hazkey::RequestEnvelope& request, bool tryConnect) {
          return transact(request, tryConnect,
                          HazkeyTransportPolicy::bestEffort);
      }) {}

HazkeyServerConnector::~HazkeyServerConnector() {
    if (sock_ >= 0) {
        close(sock_);
        sock_ = -1;
    }
}

HazkeyServerSession::HazkeyServerSession(
    HazkeyServerConnector& connector, HazkeyClientContext context,
    HazkeyClientSession::RecoveryHandler recoveryHandler)
    : connector_(connector),
      session_(std::move(context), std::move(recoveryHandler)) {
    (void)connector_.sessionClient_.open(session_);
}

HazkeyServerSession::~HazkeyServerSession() {
    (void)connector_.sessionClient_.close(session_, false);
}

bool HazkeyServerSession::updateClientContext(HazkeyClientContext context) {
    return connector_.sessionClient_.updateContext(session_, std::move(context));
}

void HazkeyServerSession::abandonUnconfirmedInput() {
    connector_.sessionClient_.abandonUnconfirmedInput(session_);
}

std::optional<hazkey::ResponseEnvelope> HazkeyServerSession::transactV2(
    hazkey::commands::HandleImeAction action, bool tryConnect) {
    return connector_.transactV2(session_, std::move(action), tryConnect);
}

std::optional<hazkey::ResponseEnvelope>
HazkeyServerSession::transactV2BestEffort(
    hazkey::commands::HandleImeAction action) {
    return connector_.transactV2BestEffort(session_, std::move(action));
}

std::string HazkeyServerConnector::getSocketPath() {
    return grimodex::ime::resolveRuntimePaths(
               std::getenv("XDG_RUNTIME_DIR"), getuid())
        .socket;
}

void HazkeyServerConnector::startHazkeyServer(bool forceRestart) {
    std::vector<std::string> arguments{
        std::string(grimodex::ime::kServerExecutable)};
    if (forceRestart) {
        arguments.emplace_back("-r");
    }
    fcitx::startProcess(arguments, "/");
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    lastServerStartNanoseconds_.store(
        std::chrono::duration_cast<std::chrono::nanoseconds>(now).count(),
        std::memory_order_release);
}

void HazkeyServerConnector::connectServer() {
    const std::string socketPath = getSocketPath();
    const auto now = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         std::chrono::steady_clock::now().time_since_epoch())
                         .count();
    const auto lastServerStart =
        lastServerStartNanoseconds_.load(std::memory_order_acquire);
    const bool serverWasJustStarted =
        lastServerStart != 0 && now >= lastServerStart &&
        now - lastServerStart <
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::seconds(2))
                .count();
    // reloadConfig may have just launched a replacement process. Give that
    // process time to publish its socket instead of racing it with a second
    // server process from this connection path.
    const int startAttempt = serverWasJustStarted ? 3 : 0;
    constexpr int forceRestartAttempt = 6;
    constexpr int maximumRetries = 10;
    constexpr int retryIntervalMilliseconds = 250;

    for (int attempt = 0; attempt < maximumRetries; ++attempt) {
        sock_ = socket(AF_UNIX, SOCK_STREAM, 0);
        if (sock_ < 0) {
            FCITX_ERROR() << "Failed to create Grimodex socket";
        } else {
            const int flags = fcntl(sock_, F_GETFL, 0);
            if (flags >= 0 && fcntl(sock_, F_SETFL, flags | O_NONBLOCK) == 0) {
                sockaddr_un address{};
                address.sun_family = AF_UNIX;
                std::strncpy(address.sun_path, socketPath.c_str(),
                             sizeof(address.sun_path) - 1);
                const int result = connect(
                    sock_, reinterpret_cast<sockaddr*>(&address),
                    sizeof(address));
                if (result == 0) {
                    return;
                }
                if (errno == EINPROGRESS) {
                    fd_set writable;
                    FD_ZERO(&writable);
                    FD_SET(sock_, &writable);
                    timeval timeout = {2, 0};
                    const int selected = select(
                        sock_ + 1, nullptr, &writable, nullptr, &timeout);
                    if (selected > 0 && FD_ISSET(sock_, &writable)) {
                        int socketError = 0;
                        socklen_t length = sizeof(socketError);
                        if (getsockopt(sock_, SOL_SOCKET, SO_ERROR,
                                       &socketError, &length) == 0 &&
                            socketError == 0) {
                            return;
                        }
                    }
                }
            }
            close(sock_);
            sock_ = -1;
        }

        FCITX_INFO() << "Failed to connect Grimodex IME server, retry "
                     << (attempt + 1);
        if (attempt == startAttempt) {
            startHazkeyServer(false);
        } else if (attempt == forceRestartAttempt) {
            startHazkeyServer(true);
        }
        std::this_thread::sleep_for(
            std::chrono::milliseconds(retryIntervalMilliseconds));
    }
    FCITX_ERROR() << "Failed to connect Grimodex IME server after "
                  << maximumRetries << " attempts";
}

std::optional<hazkey::ResponseEnvelope> HazkeyServerConnector::transact(
    const hazkey::RequestEnvelope& request, bool tryConnect,
    HazkeyTransportPolicy policy) {
    std::unique_lock<std::mutex> lock(transactMutex, std::defer_lock);
    if (policy == HazkeyTransportPolicy::bestEffort) {
        if (!lock.try_lock()) {
            return std::nullopt;
        }
    } else {
        lock.lock();
    }

    grimodex::ime::socketio::Deadline deadline;
    if (policy == HazkeyTransportPolicy::bestEffort) {
        deadline = std::chrono::steady_clock::now() + bestEffortBudget;
    }

    if (sock_ < 0) {
        if (!tryConnect) {
            return std::nullopt;
        }
        connectServer();
        if (sock_ < 0) {
            return std::nullopt;
        }
    }

    std::string message;
    if (!request.SerializeToString(&message) ||
        message.size() > std::numeric_limits<uint32_t>::max()) {
        FCITX_ERROR() << "Failed to serialize Grimodex protobuf request";
        return std::nullopt;
    }

    uint32_t networkLength = htonl(static_cast<uint32_t>(message.size()));
    if (!grimodex::ime::socketio::writeAll(
            sock_, &networkLength, sizeof(networkLength), deadline) ||
        !grimodex::ime::socketio::writeAll(
            sock_, message.data(), message.size(), deadline)) {
        FCITX_ERROR() << "Grimodex socket write timeout or failure";
        close(sock_);
        sock_ = -1;
        if (tryConnect) {
            connectServer();
        }
        return std::nullopt;
    }

    uint32_t responseLengthBuffer = 0;
    if (!grimodex::ime::socketio::readAll(
            sock_, &responseLengthBuffer, sizeof(responseLengthBuffer),
            deadline)) {
        FCITX_ERROR() << "Grimodex socket response-header timeout or failure";
        close(sock_);
        sock_ = -1;
        return std::nullopt;
    }
    const uint32_t responseLength = ntohl(responseLengthBuffer);
    constexpr uint32_t maximumResponseBytes = 2 * 1024 * 1024;
    if (responseLength > maximumResponseBytes) {
        FCITX_ERROR() << "Grimodex response exceeds frame limit";
        close(sock_);
        sock_ = -1;
        return std::nullopt;
    }

    std::vector<char> buffer(responseLength);
    if (!grimodex::ime::socketio::readAll(
            sock_, buffer.data(), buffer.size(), deadline)) {
        FCITX_ERROR() << "Grimodex socket response-body timeout or failure";
        close(sock_);
        sock_ = -1;
        return std::nullopt;
    }

    hazkey::ResponseEnvelope response;
    if (!response.ParseFromArray(buffer.data(),
                                 static_cast<int>(buffer.size()))) {
        FCITX_ERROR() << "Failed to parse Grimodex protobuf response";
        return std::nullopt;
    }
    return response;
}

std::optional<hazkey::ResponseEnvelope> HazkeyServerConnector::transactV2(
    HazkeyClientSession& session,
    hazkey::commands::HandleImeAction action,
    bool tryConnect) {
    return sessionClient_.transactV2(session, std::move(action), tryConnect);
}

std::optional<hazkey::ResponseEnvelope>
HazkeyServerConnector::transactV2BestEffort(
    HazkeyClientSession& session,
    hazkey::commands::HandleImeAction action) {
    return sessionClient_.transactV2BestEffort(session, std::move(action));
}
