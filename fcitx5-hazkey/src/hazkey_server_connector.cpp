#include "hazkey_server_connector.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include <array>
#include <algorithm>
#include <atomic>
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

enum class HazkeyPendingPolicy {
    normal,
    bestEffort,
    lifecycle,
};

struct HazkeyPendingExchange {
    std::string payload;
    std::string originPayload;
    std::vector<char> outbound;
    std::size_t outboundOffset = 0;
    std::array<char, sizeof(uint32_t)> responseHeader{};
    std::size_t responseHeaderOffset = 0;
    std::vector<char> responseBody;
    std::size_t responseBodyOffset = 0;
    bool responseLengthDecoded = false;
    // These budgets belong to the head exchange, not to the caller currently
    // trying to use the stream. A later normal RPC must not turn a deferred
    // lifecycle/best-effort response into a ten-second read.
    HazkeyPendingPolicy policy = HazkeyPendingPolicy::normal;
    std::chrono::milliseconds resumeBudget{0};
    std::chrono::steady_clock::time_point createdAt =
        std::chrono::steady_clock::now();
    std::optional<std::chrono::steady_clock::time_point> expiresAt;
    uint32_t resumeAttempts = 0;
    uint32_t staleRetryCount = 0;
};

namespace {

std::timed_mutex transactMutex;

constexpr auto bestEffortBudget = std::chrono::milliseconds(250);
constexpr auto lifecycleBudget = std::chrono::milliseconds(10);
constexpr auto lifecycleTotalBudget = std::chrono::milliseconds(100);
constexpr uint32_t lifecycleMaximumAdvanceAttempts = 16;
constexpr uint32_t maximumResponseBytes = 2 * 1024 * 1024;
std::atomic<uint64_t> nextDeferredRequestID{1};

enum class ExchangeProgress {
    complete,
    pending,
    failed,
};

struct ExchangeResult {
    ExchangeProgress progress = ExchangeProgress::failed;
    std::optional<hazkey::ResponseEnvelope> response;
};

std::unique_ptr<HazkeyPendingExchange> makeExchange(
    std::string payload, std::string originPayload,
    HazkeyPendingPolicy policy) {
    if (payload.size() > std::numeric_limits<uint32_t>::max()) {
        return nullptr;
    }
    auto exchange = std::make_unique<HazkeyPendingExchange>();
    exchange->payload = std::move(payload);
    exchange->originPayload = std::move(originPayload);
    exchange->policy = policy;
    exchange->createdAt = std::chrono::steady_clock::now();
    if (policy == HazkeyPendingPolicy::bestEffort) {
        exchange->resumeBudget = bestEffortBudget;
        exchange->expiresAt =
            std::chrono::steady_clock::now() + bestEffortBudget;
    } else if (policy == HazkeyPendingPolicy::lifecycle) {
        exchange->resumeBudget = lifecycleBudget;
        exchange->expiresAt =
            std::chrono::steady_clock::now() + lifecycleTotalBudget;
    }
    exchange->outbound.resize(sizeof(uint32_t) + exchange->payload.size());
    const uint32_t networkLength =
        htonl(static_cast<uint32_t>(exchange->payload.size()));
    std::memcpy(exchange->outbound.data(), &networkLength,
                sizeof(networkLength));
    std::memcpy(exchange->outbound.data() + sizeof(networkLength),
                exchange->payload.data(), exchange->payload.size());
    return exchange;
}

enum class ReadyResult {
    ready,
    timeout,
    failed,
};

ReadyResult waitForSocket(
    int fd, bool readable,
    const grimodex::ime::socketio::Deadline& deadline) {
    using Clock = std::chrono::steady_clock;
    const auto normalTimeout =
        readable ? std::chrono::seconds(10) : std::chrono::seconds(2);
    while (true) {
        auto remaining = std::chrono::duration_cast<std::chrono::microseconds>(
            deadline.has_value() ? *deadline - Clock::now() : normalTimeout);
        if (remaining <= std::chrono::microseconds::zero()) {
            return ReadyResult::timeout;
        }
        timeval timeout{
            .tv_sec = static_cast<time_t>(remaining.count() / 1'000'000),
            .tv_usec =
                static_cast<suseconds_t>(remaining.count() % 1'000'000),
        };
        fd_set descriptors;
        FD_ZERO(&descriptors);
        FD_SET(fd, &descriptors);
        const int selected =
            readable ? select(fd + 1, &descriptors, nullptr, nullptr, &timeout)
                     : select(fd + 1, nullptr, &descriptors, nullptr, &timeout);
        if (selected > 0) {
            return ReadyResult::ready;
        }
        if (selected == 0) {
            return ReadyResult::timeout;
        }
        if (errno != EINTR) {
            return ReadyResult::failed;
        }
    }
}

ExchangeResult advanceExchange(
    int fd, HazkeyPendingExchange& exchange,
    const grimodex::ime::socketio::Deadline& deadline) {
    using Clock = std::chrono::steady_clock;
    const auto wouldBlock = [&](bool readable) {
        const auto result = waitForSocket(fd, readable, deadline);
        if (result == ReadyResult::ready) {
            return ExchangeProgress::complete;
        }
        if (result == ReadyResult::timeout && deadline.has_value()) {
            return ExchangeProgress::pending;
        }
        return ExchangeProgress::failed;
    };

    while (exchange.outboundOffset < exchange.outbound.size()) {
        if (deadline.has_value() && Clock::now() >= *deadline) {
            return {.progress = ExchangeProgress::pending,
                    .response = std::nullopt};
        }
#ifdef HAZKEY_ENABLE_TEST_HOOKS
        // The managed unit-test sandbox rejects send(MSG_NOSIGNAL) with EPERM;
        // its socketpair peer lifetime is controlled by the test.
        const auto count = write(
            fd, exchange.outbound.data() + exchange.outboundOffset,
            exchange.outbound.size() - exchange.outboundOffset);
#else
        const auto count = ::send(
            fd, exchange.outbound.data() + exchange.outboundOffset,
            exchange.outbound.size() - exchange.outboundOffset,
            MSG_NOSIGNAL);
#endif
        if (count > 0) {
            exchange.outboundOffset += static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            const auto progress = wouldBlock(false);
            if (progress == ExchangeProgress::complete) {
                continue;
            }
            return {.progress = progress, .response = std::nullopt};
        }
        return {.progress = ExchangeProgress::failed,
                .response = std::nullopt};
    }

    while (exchange.responseHeaderOffset < exchange.responseHeader.size()) {
        if (deadline.has_value() && Clock::now() >= *deadline) {
            return {.progress = ExchangeProgress::pending,
                    .response = std::nullopt};
        }
        const auto count = recv(
            fd, exchange.responseHeader.data() + exchange.responseHeaderOffset,
            exchange.responseHeader.size() - exchange.responseHeaderOffset, 0);
        if (count > 0) {
            exchange.responseHeaderOffset += static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            const auto progress = wouldBlock(true);
            if (progress == ExchangeProgress::complete) {
                continue;
            }
            return {.progress = progress, .response = std::nullopt};
        }
        return {.progress = ExchangeProgress::failed,
                .response = std::nullopt};
    }

    if (!exchange.responseLengthDecoded) {
        uint32_t networkLength = 0;
        std::memcpy(&networkLength, exchange.responseHeader.data(),
                    sizeof(networkLength));
        const uint32_t responseLength = ntohl(networkLength);
        if (responseLength > maximumResponseBytes) {
            return {.progress = ExchangeProgress::failed,
                    .response = std::nullopt};
        }
        exchange.responseBody.resize(responseLength);
        exchange.responseLengthDecoded = true;
    }

    while (exchange.responseBodyOffset < exchange.responseBody.size()) {
        if (deadline.has_value() && Clock::now() >= *deadline) {
            return {.progress = ExchangeProgress::pending,
                    .response = std::nullopt};
        }
        const auto count = recv(
            fd, exchange.responseBody.data() + exchange.responseBodyOffset,
            exchange.responseBody.size() - exchange.responseBodyOffset, 0);
        if (count > 0) {
            exchange.responseBodyOffset += static_cast<std::size_t>(count);
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            const auto progress = wouldBlock(true);
            if (progress == ExchangeProgress::complete) {
                continue;
            }
            return {.progress = progress, .response = std::nullopt};
        }
        return {.progress = ExchangeProgress::failed,
                .response = std::nullopt};
    }

    hazkey::ResponseEnvelope response;
    if (!response.ParseFromArray(
            exchange.responseBody.data(),
            static_cast<int>(exchange.responseBody.size()))) {
        return {.progress = ExchangeProgress::failed,
                .response = std::nullopt};
    }
    return {
        .progress = ExchangeProgress::complete,
        .response = std::move(response),
    };
}

std::optional<std::string> staleLifecycleRetry(
    const HazkeyPendingExchange& exchange,
    const hazkey::ResponseEnvelope& response) {
    if (exchange.policy != HazkeyPendingPolicy::lifecycle ||
        exchange.staleRetryCount >= 1 ||
        response.status() != hazkey::STALE_REVISION ||
        !response.has_handle_ime_action_result() ||
        response.handle_ime_action_result().status() != hazkey::STALE_REVISION ||
        !response.handle_ime_action_result().has_snapshot()) {
        return std::nullopt;
    }
    hazkey::RequestEnvelope request;
    if (!request.ParseFromString(exchange.payload) ||
        !request.has_handle_ime_action() ||
        !request.handle_ime_action().has_resolve_pending_learning()) {
        return std::nullopt;
    }
    auto* action = request.mutable_handle_ime_action();
    action->set_request_id(
        "fcitx-lifecycle-" +
        std::to_string(nextDeferredRequestID.fetch_add(1)));
    action->set_expected_revision(
        response.handle_ime_action_result().snapshot().revision());
    std::string payload;
    if (!request.SerializeToString(&payload)) {
        return std::nullopt;
    }
    return payload;
}

}  // namespace

HazkeyServerConnector::HazkeyServerConnector()
    : sessionClient_([this](const hazkey::RequestEnvelope& request,
                            bool tryConnect) {
          return transact(request, tryConnect, HazkeyTransportPolicy::normal);
      },
      [this](const hazkey::RequestEnvelope& request, bool tryConnect) {
          return transact(request, tryConnect,
                          HazkeyTransportPolicy::bestEffort);
      },
      [this](const hazkey::RequestEnvelope& request, bool tryConnect) {
          return transact(request, tryConnect,
                          HazkeyTransportPolicy::lifecycle);
      }) {}

#ifdef HAZKEY_ENABLE_TEST_HOOKS
HazkeyServerConnector::HazkeyServerConnector(int connectedSocketForTesting)
    : HazkeyServerConnector() {
    sock_ = connectedSocketForTesting;
}
#endif

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
    (void)close();
}

bool HazkeyServerSession::updateClientContext(HazkeyClientContext context) {
    return connector_.sessionClient_.updateContext(session_, std::move(context));
}

void HazkeyServerSession::abandonUnconfirmedInput() {
    connector_.sessionClient_.abandonUnconfirmedInput(session_);
}

void HazkeyServerSession::finalizeWithoutUITarget(bool preferredCommit) {
    connector_.sessionClient_.finalizeWithoutUITarget(session_,
                                                       preferredCommit);
}

HazkeyFlushResult HazkeyServerSession::flushPendingV2(bool tryConnect) {
    return connector_.sessionClient_.flushPendingV2(session_, tryConnect);
}

bool HazkeyServerSession::close() {
    if (closed_) {
        return true;
    }
    const bool result = connector_.sessionClient_.close(session_, false);
    if (result) {
        closed_ = true;
    }
    return result;
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

std::optional<hazkey::ResponseEnvelope>
HazkeyServerSession::transactV2DurableBestEffort(
    hazkey::commands::HandleImeAction action) {
    return connector_.transactV2DurableBestEffort(session_, std::move(action));
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
    std::string message;
    if (!request.SerializeToString(&message) ||
        message.size() > std::numeric_limits<uint32_t>::max()) {
        FCITX_ERROR() << "Failed to serialize Grimodex protobuf request";
        return std::nullopt;
    }

    using Clock = std::chrono::steady_clock;
    grimodex::ime::socketio::Deadline deadline;
    if (policy == HazkeyTransportPolicy::bestEffort) {
        deadline = Clock::now() + bestEffortBudget;
    } else if (policy == HazkeyTransportPolicy::lifecycle) {
        deadline = Clock::now() + lifecycleBudget;
        // Take ownership before contending on the shared stream. If another
        // session is in flight, the next ordinary RPC drains this disposition
        // first; the InputContext may therefore abandon its old local state
        // without losing a queued discard decision.
        std::lock_guard<std::mutex> queueLock(lifecycleQueueMutex_);
        if (std::find(lifecycleQueue_.begin(), lifecycleQueue_.end(), message) ==
            lifecycleQueue_.end()) {
            lifecycleQueue_.push_back(message);
        }
    }

    std::unique_lock<std::timed_mutex> lock(transactMutex, std::defer_lock);
    if (policy == HazkeyTransportPolicy::lifecycle) {
        if (!lock.try_lock_until(*deadline)) {
            return std::nullopt;
        }
    } else if (policy == HazkeyTransportPolicy::bestEffort) {
        if (!lock.try_lock()) {
            return std::nullopt;
        }
    } else {
        lock.lock();
    }

    const auto clearQueuedLifecycle = [&] {
        std::lock_guard<std::mutex> queueLock(lifecycleQueueMutex_);
        lifecycleQueue_.clear();
    };
    const auto closeBrokenSocket = [&] {
        if (sock_ >= 0) {
            close(sock_);
            sock_ = -1;
        }
        pendingExchange_.reset();
        // Disconnecting the owning fd makes the server discard staged
        // learning for all of its sessions. Queued old-session dispositions
        // are therefore no longer needed and must not cross to a new socket.
        clearQueuedLifecycle();
    };

    if (sock_ < 0) {
        pendingExchange_.reset();
        clearQueuedLifecycle();
        if (!tryConnect || policy != HazkeyTransportPolicy::normal) {
            return std::nullopt;
        }
        connectServer();
        if (sock_ < 0) {
            return std::nullopt;
        }
    }

    // Advances the exchange currently at the head of the byte stream. A
    // bounded timeout deliberately leaves its exact offsets intact rather
    // than closing the shared fd and invalidating unrelated sessions.
    const auto advancePending = [&]() -> ExchangeResult {
        while (pendingExchange_) {
            ++pendingExchange_->resumeAttempts;
            if (pendingExchange_->policy == HazkeyPendingPolicy::lifecycle &&
                pendingExchange_->resumeAttempts >
                    lifecycleMaximumAdvanceAttempts) {
                return {.progress = ExchangeProgress::failed,
                        .response = std::nullopt};
            }
            auto headDeadline = deadline;
            if (pendingExchange_->expiresAt.has_value() &&
                Clock::now() >= *pendingExchange_->expiresAt) {
                return {.progress = ExchangeProgress::failed,
                        .response = std::nullopt};
            }
            if (pendingExchange_->resumeBudget.count() > 0) {
                const auto resumeDeadline =
                    Clock::now() + pendingExchange_->resumeBudget;
                if (!headDeadline.has_value() ||
                    resumeDeadline < *headDeadline) {
                    headDeadline = resumeDeadline;
                }
            }
            if (pendingExchange_->expiresAt.has_value() &&
                (!headDeadline.has_value() ||
                 *pendingExchange_->expiresAt < *headDeadline)) {
                headDeadline = pendingExchange_->expiresAt;
            }
            auto result =
                advanceExchange(sock_, *pendingExchange_, headDeadline);
            if (result.progress != ExchangeProgress::complete) {
                return result;
            }
            if (result.response.has_value()) {
                const auto retry =
                    staleLifecycleRetry(*pendingExchange_, *result.response);
                if (retry.has_value()) {
                    const auto origin = pendingExchange_->originPayload;
                    const auto createdAt = pendingExchange_->createdAt;
                    const auto expiresAt = pendingExchange_->expiresAt;
                    const auto staleRetryCount =
                        pendingExchange_->staleRetryCount;
                    pendingExchange_ =
                        makeExchange(*retry, origin,
                                     HazkeyPendingPolicy::lifecycle);
                    if (!pendingExchange_) {
                        return {.progress = ExchangeProgress::failed,
                                .response = std::nullopt};
                    }
                    pendingExchange_->createdAt = createdAt;
                    pendingExchange_->expiresAt = expiresAt;
                    pendingExchange_->staleRetryCount = staleRetryCount + 1;
                    continue;
                }
            }
            return result;
        }
        return {.progress = ExchangeProgress::failed,
                .response = std::nullopt};
    };

    const auto finishHead = [&]()
        -> std::optional<std::optional<hazkey::ResponseEnvelope>> {
        if (!pendingExchange_) {
            return std::optional<hazkey::ResponseEnvelope>{};
        }
        const std::string origin = pendingExchange_->originPayload;
        auto result = advancePending();
        if (result.progress == ExchangeProgress::pending) {
            return std::nullopt;
        }
        if (result.progress == ExchangeProgress::failed) {
            closeBrokenSocket();
            return std::nullopt;
        }
        pendingExchange_.reset();
        if (origin == message) {
            return std::move(result.response);
        }
        return std::optional<hazkey::ResponseEnvelope>{};
    };

    const auto handOffNewLifecycleQueue = [&] {
        while (!pendingExchange_) {
            std::string deferred;
            {
                std::lock_guard<std::mutex> queueLock(lifecycleQueueMutex_);
                if (lifecycleQueue_.empty()) {
                    return;
                }
                deferred = std::move(lifecycleQueue_.front());
                lifecycleQueue_.pop_front();
            }
            pendingExchange_ = makeExchange(
                deferred, deferred, HazkeyPendingPolicy::lifecycle);
            if (!pendingExchange_) {
                closeBrokenSocket();
                return;
            }
            const auto finished = finishHead();
            if (!finished.has_value()) {
                // Either the bounded response is still at the head (the frame
                // itself has been handed off), or stream recovery disconnected
                // the old fd. Both satisfy disposition durability.
                return;
            }
        }
    };

    if (pendingExchange_) {
        const auto matched = finishHead();
        if (!matched.has_value()) {
            return std::nullopt;
        }
        if (matched->has_value()) {
            {
                std::lock_guard<std::mutex> queueLock(lifecycleQueueMutex_);
                std::erase(lifecycleQueue_, message);
            }
            auto response = *matched;
            handOffNewLifecycleQueue();
            return response;
        }
    }

    while (true) {
        std::string deferred;
        {
            std::lock_guard<std::mutex> queueLock(lifecycleQueueMutex_);
            if (lifecycleQueue_.empty()) {
                break;
            }
            deferred = std::move(lifecycleQueue_.front());
            lifecycleQueue_.pop_front();
        }
        pendingExchange_ = makeExchange(
            deferred, deferred, HazkeyPendingPolicy::lifecycle);
        if (!pendingExchange_) {
            closeBrokenSocket();
            return std::nullopt;
        }
        const auto matched = finishHead();
        if (!matched.has_value()) {
            return std::nullopt;
        }
        if (matched->has_value()) {
            {
                std::lock_guard<std::mutex> queueLock(lifecycleQueueMutex_);
                std::erase(lifecycleQueue_, message);
            }
            auto response = *matched;
            handOffNewLifecycleQueue();
            return response;
        }
    }

    if (policy == HazkeyTransportPolicy::lifecycle) {
        // The current request was connector-owned and processed in the queue
        // above. Reaching here means its response was intentionally consumed
        // while finishing an earlier equivalent handoff.
        return std::nullopt;
    }

    pendingExchange_ = makeExchange(
        message, message,
        policy == HazkeyTransportPolicy::bestEffort
            ? HazkeyPendingPolicy::bestEffort
            : HazkeyPendingPolicy::normal);
    if (!pendingExchange_) {
        return std::nullopt;
    }
    const auto result = finishHead();
    if (!result.has_value()) {
        if (policy == HazkeyTransportPolicy::bestEffort &&
            pendingExchange_ &&
            pendingExchange_->policy == HazkeyPendingPolicy::bestEffort) {
            // A best-effort callback already owns the full 250ms budget. Once
            // exhausted, keeping its unknown response at the stream head would
            // only turn the next normal key into an unbounded wait. Disconnect
            // is the safe recovery boundary; the server discards staged
            // learning for this fd and normal journal replay opens a new one.
            closeBrokenSocket();
        }
        if (sock_ < 0 && tryConnect &&
            policy == HazkeyTransportPolicy::normal) {
            connectServer();
        }
        return std::nullopt;
    }
    auto response = *result;
    handOffNewLifecycleQueue();
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

std::optional<hazkey::ResponseEnvelope>
HazkeyServerConnector::transactV2DurableBestEffort(
    HazkeyClientSession& session,
    hazkey::commands::HandleImeAction action) {
    return sessionClient_.transactV2DurableBestEffort(session,
                                                      std::move(action));
}
