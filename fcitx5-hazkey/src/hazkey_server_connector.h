#ifndef HAZKEY_SERVER_CONNECTOR_H
#define HAZKEY_SERVER_CONNECTOR_H

#include <atomic>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "base.pb.h"
#include "commands.pb.h"
#include "hazkey_session_client.h"

class HazkeyServerConnector;
struct HazkeyPendingExchange;

enum class HazkeyTransportPolicy {
    normal,
    bestEffort,
    lifecycle,
};

/// One Protocol-v2 conversion session owned by an Fcitx InputContext.
class HazkeyServerSession {
   public:
    HazkeyServerSession(HazkeyServerConnector& connector,
                        HazkeyClientContext context,
                        HazkeyClientSession::RecoveryHandler recoveryHandler = {});
    ~HazkeyServerSession();

    HazkeyServerSession(const HazkeyServerSession&) = delete;
    HazkeyServerSession& operator=(const HazkeyServerSession&) = delete;

    const HazkeyClientContext& context() const { return session_.context(); }
    bool updateClientContext(HazkeyClientContext context);
    void abandonUnconfirmedInput();
    void finalizeWithoutUITarget(bool preferredCommit);
    HazkeyFlushResult flushPendingV2(bool tryConnect = false);
    bool close();
    bool hasPendingV2() const { return session_.hasDeferredWork(); }
    bool supportsV2() const { return session_.capabilities().supportsV2(); }
    bool isLocalTextFallbackSemanticallySafe() const {
        return session_.isLocalTextFallbackSemanticallySafe();
    }
    bool shouldApplyEffect(uint64_t effectID) {
        return session_.shouldApplyEffect(effectID);
    }
    std::optional<hazkey::ResponseEnvelope> transactV2(
        hazkey::commands::HandleImeAction action, bool tryConnect = true);
    std::optional<hazkey::ResponseEnvelope> transactV2BestEffort(
        hazkey::commands::HandleImeAction action);
    std::optional<hazkey::ResponseEnvelope> transactV2DurableBestEffort(
        hazkey::commands::HandleImeAction action);

   private:
    HazkeyServerConnector& connector_;
    HazkeyClientSession session_;
    bool closed_ = false;
};

class HazkeyServerConnector {
   public:
    HazkeyServerConnector();
#ifdef HAZKEY_ENABLE_TEST_HOOKS
    explicit HazkeyServerConnector(int connectedSocketForTesting);
#endif
    ~HazkeyServerConnector();

    HazkeyServerConnector(const HazkeyServerConnector&) = delete;
    HazkeyServerConnector& operator=(const HazkeyServerConnector&) = delete;

    std::string getSocketPath();
    void connectServer();
    void startHazkeyServer(bool forceRestart);
    std::optional<hazkey::ResponseEnvelope> transact(
        const hazkey::RequestEnvelope& request, bool tryConnect = true,
        HazkeyTransportPolicy policy = HazkeyTransportPolicy::normal);

   private:
    friend class HazkeyServerSession;

    std::optional<hazkey::ResponseEnvelope> transactV2(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action,
        bool tryConnect = true);
    std::optional<hazkey::ResponseEnvelope> transactV2BestEffort(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action);
    std::optional<hazkey::ResponseEnvelope> transactV2DurableBestEffort(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action);

    HazkeySessionClient sessionClient_;
    int sock_ = -1;
    std::atomic<int64_t> lastServerStartNanoseconds_{0};
    // Lifecycle requests are first copied into this connector-owned queue.
    // This lets an InputContext hand off a discard/close disposition without
    // waiting behind an unrelated session currently using the shared stream.
    std::mutex lifecycleQueueMutex_;
    std::deque<std::string> lifecycleQueue_;
    std::unique_ptr<HazkeyPendingExchange> pendingExchange_;
};

#endif  // HAZKEY_SERVER_CONNECTOR_H
