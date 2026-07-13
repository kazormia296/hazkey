#ifndef HAZKEY_SERVER_CONNECTOR_H
#define HAZKEY_SERVER_CONNECTOR_H

#include <atomic>
#include <cstdint>
#include <optional>
#include <string>

#include "base.pb.h"
#include "commands.pb.h"
#include "hazkey_session_client.h"

class HazkeyServerConnector;

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
    bool supportsV2() const { return session_.capabilities().supportsV2(); }
    bool shouldApplyEffect(uint64_t effectID) {
        return session_.shouldApplyEffect(effectID);
    }
    std::optional<hazkey::ResponseEnvelope> transactV2(
        hazkey::commands::HandleImeAction action, bool tryConnect = true);

   private:
    HazkeyServerConnector& connector_;
    HazkeyClientSession session_;
};

class HazkeyServerConnector {
   public:
    HazkeyServerConnector();
    ~HazkeyServerConnector();

    HazkeyServerConnector(const HazkeyServerConnector&) = delete;
    HazkeyServerConnector& operator=(const HazkeyServerConnector&) = delete;

    std::string getSocketPath();
    void connectServer();
    void startHazkeyServer(bool forceRestart);
    std::optional<hazkey::ResponseEnvelope> transact(
        const hazkey::RequestEnvelope& request, bool tryConnect = true);

   private:
    friend class HazkeyServerSession;

    std::optional<hazkey::ResponseEnvelope> transactV2(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action,
        bool tryConnect = true);

    HazkeySessionClient sessionClient_;
    int sock_ = -1;
    std::atomic<int64_t> lastServerStartNanoseconds_{0};
};

#endif  // HAZKEY_SERVER_CONNECTOR_H
