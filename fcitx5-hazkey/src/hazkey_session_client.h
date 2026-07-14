#ifndef HAZKEY_SESSION_CLIENT_H
#define HAZKEY_SESSION_CLIENT_H

#include <functional>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "base.pb.h"
#include "hazkey_effect_ledger.h"
#include "hazkey_recovery_journal.h"

struct HazkeyClientContext {
    std::string program;
    std::string frontend;
    bool secureInput = false;
};

struct HazkeyClientContextTransition {
    bool contextChanged = false;
    bool enteredSecure = false;
    bool clearPreedit = false;
    bool reopenSession = false;
    bool allowSurroundingText = true;
};

struct HazkeyProtocolCapabilities {
    uint32_t protocolVersion = 1;
    uint64_t featureBits = 0;
    uint32_t maxSnapshotVersion = 0;
    bool recoverySupport = false;
    bool idempotentRequestSupport = false;

    bool supportsV2() const {
        return protocolVersion >= 2 && idempotentRequestSupport;
    }
};

struct HazkeyFlushResult {
    bool completed = false;
    std::optional<hazkey::ResponseEnvelope> response;
};

HazkeyClientContextTransition evaluateHazkeyClientContextTransition(
    const HazkeyClientContext& previous, const HazkeyClientContext& next);

class HazkeyClientSession {
   public:
    using RecoveryHandler = std::function<void()>;

    explicit HazkeyClientSession(HazkeyClientContext context,
                                 RecoveryHandler recoveryHandler = {})
        : context_(std::move(context)),
          recoveryHandler_(std::move(recoveryHandler)) {}

    const HazkeyClientContext& context() const { return context_; }
    const std::string& id() const { return id_; }
    const HazkeyProtocolCapabilities& capabilities() const { return capabilities_; }
    uint64_t revision() const { return revision_; }
    void setRevision(uint64_t revision) { revision_ = revision; }

    bool shouldApplyEffect(uint64_t effectID) {
        return effectLedger_.claim(effectID);
    }

    void clearEffects() {
        hasEffectNamespaceAnchor_ = false;
        rawEffectAnchor_ = 0;
        globalEffectAnchor_ = 0;
        highestRawEffectID_ = 0;
        nextGlobalEffectID_ = 1;
        effectLedger_.clear();
        clearReplayedEffects();
    }
    void beginFreshEffectNamespace() {
        hasEffectNamespaceAnchor_ = false;
        rawEffectAnchor_ = 0;
        globalEffectAnchor_ = 0;
        highestRawEffectID_ = 0;
    }
    void clearReplayedEffects() {
        replayedEffects_.clear();
    }
    bool hasRecoveryCheckpoint() const {
        return !recoveryCheckpoint_.empty();
    }
    bool hasFallbackComposition() const { return !fallbackComposition_.empty(); }
    bool isLocalTextFallbackSemanticallySafe() const {
        return !reconversionFallbackUnsafe_ && !unicodeFallbackUnsafe_;
    }
    bool canUseStoredTextFallback() const {
        return !fallbackComposition_.empty() &&
               isLocalTextFallbackSemanticallySafe() &&
               !directFallbackUnsafe_ && !presentationFallbackUnsafe_;
    }
    std::size_t pendingActionCount() const { return journal_.pending().size(); }
    bool hasConfirmedSnapshot() const { return !journal_.lastSnapshot().empty(); }
    bool hasDeferredWork() const {
        return !journal_.pending().empty() || !replayedEffects_.empty();
    }

   private:
    friend class HazkeySessionClient;

    HazkeyClientContext context_;
    std::string id_;
    HazkeyProtocolCapabilities capabilities_;
    uint64_t revision_ = 0;
    HazkeyEffectLedger effectLedger_;
    // Server effect IDs are strictly monotonic inside a restored namespace.
    // An affine namespace anchor maps them into one client-global monotonic
    // sequence in O(1) memory; nextGlobalEffectID_ spans fresh namespaces.
    bool hasEffectNamespaceAnchor_ = false;
    uint64_t rawEffectAnchor_ = 0;
    uint64_t globalEffectAnchor_ = 0;
    uint64_t highestRawEffectID_ = 0;
    uint64_t nextGlobalEffectID_ = 1;
    HazkeyRecoveryJournal journal_;
    std::vector<hazkey::ClientEffect> replayedEffects_;
    std::string recoveryCheckpoint_;
    std::string fallbackComposition_;
    bool reconversionFallbackUnsafe_ = false;
    bool unicodeFallbackUnsafe_ = false;
    bool directFallbackUnsafe_ = false;
    bool presentationFallbackUnsafe_ = false;
    RecoveryHandler recoveryHandler_;
};

class HazkeySessionClient {
   public:
    using Transport = std::function<std::optional<hazkey::ResponseEnvelope>(
        const hazkey::RequestEnvelope&, bool)>;

    explicit HazkeySessionClient(
        Transport transport,
        Transport bestEffortTransport = {},
        Transport lifecycleTransport = {})
        : transport_(std::move(transport)),
          bestEffortTransport_(bestEffortTransport
                                   ? std::move(bestEffortTransport)
                                   : transport_),
          lifecycleTransport_(lifecycleTransport
                                  ? std::move(lifecycleTransport)
                                  : transport_) {}

    bool open(HazkeyClientSession& session, bool tryConnect = true);
    bool close(HazkeyClientSession& session, bool tryConnect = false);
    bool updateContext(HazkeyClientSession& session, HazkeyClientContext context);
    void abandonUnconfirmedInput(HazkeyClientSession& session);
    void finalizeWithoutUITarget(HazkeyClientSession& session,
                                 bool preferredCommit);

    // Sends one semantic v2 action.  The request ID is generated by the
    // client when the caller leaves it empty; retrying the same envelope is
    // therefore safe for a server implementing the v2 cache contract.
    std::optional<hazkey::ResponseEnvelope> transactV2(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action,
        bool tryConnect = true);

    // Sends an opportunistic action without replaying or joining the recovery
    // journal. It uses only the currently open transport/session; a later
    // semantic key remains responsible for recovery.
    std::optional<hazkey::ResponseEnvelope> transactV2BestEffort(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action);

    // Records a semantic action in the recovery journal before attempting a
    // bounded, non-reconnecting send. A failed send is replayed in order by
    // the next normal transaction.
    std::optional<hazkey::ResponseEnvelope> transactV2DurableBestEffort(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action);

    // Replays every deferred semantic action and returns one synthetic response
    // containing the newest snapshot plus all replayed client effects. Callers
    // must apply the response before closing or crossing a security boundary.
    HazkeyFlushResult flushPendingV2(HazkeyClientSession& session,
                                     bool tryConnect = false);

   private:
    std::optional<hazkey::ResponseEnvelope> executeV2(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction& action,
        bool tryConnect,
        bool allowSessionRecovery = true,
        bool bestEffort = false,
        bool lifecycle = false);
    bool replayPendingV2(
        HazkeyClientSession& session, bool tryConnect,
        bool lifecycle = false,
        std::optional<hazkey::ResponseEnvelope>* terminalFailure = nullptr,
        bool* confirmedAfterTerminalFailure = nullptr);
    std::optional<hazkey::ResponseEnvelope> executeJournaledV2(
        HazkeyClientSession& session,
        hazkey::commands::HandleImeAction action,
        bool tryConnect,
        bool collectEffects,
        bool lifecycle = false);
    bool hasPendingLearningResolution(const HazkeyClientSession& session) const;
    void discardServerNamespacePreservingRecovery(
        HazkeyClientSession& session);
    std::optional<hazkey::ResponseEnvelope> makeFlushResponse(
        HazkeyClientSession& session);
    std::optional<hazkey::ResponseEnvelope> makeEffectHandoffResponse(
        HazkeyClientSession& session, hazkey::StatusCode status);
    void normalizeEffectIDs(HazkeyClientSession& session,
                            hazkey::ResponseEnvelope& response);
    void collectResponseEffects(HazkeyClientSession& session,
                                const hazkey::ResponseEnvelope& response);
    void attachReplayedEffects(HazkeyClientSession& session,
                               hazkey::ResponseEnvelope& response);

    Transport transport_;
    Transport bestEffortTransport_;
    Transport lifecycleTransport_;
};

#endif  // HAZKEY_SESSION_CLIENT_H
