#include "hazkey_session_client.h"

#include <algorithm>
#include <utility>

#include <atomic>
#include <limits>

namespace {
std::atomic<uint64_t> nextRequestID{1};
constexpr uint64_t scheduleLiveConversionClientFeature = 1ULL << 0;
constexpr uint64_t stagedLearningClientFeature = 1ULL << 1;
constexpr uint64_t currentClientFeatures =
    scheduleLiveConversionClientFeature | stagedLearningClientFeature;

bool isStaleImeResponse(
    const std::optional<hazkey::ResponseEnvelope>& response) {
    return response.has_value() &&
           response->status() == hazkey::STALE_REVISION &&
           response->has_handle_ime_action_result() &&
           response->handle_ime_action_result().status() ==
               hazkey::STALE_REVISION;
}

bool isSuccessfulImeResponse(
    const std::optional<hazkey::ResponseEnvelope>& response) {
    return response.has_value() && response->status() == hazkey::SUCCESS &&
           response->has_handle_ime_action_result() &&
           response->handle_ime_action_result().status() == hazkey::SUCCESS &&
           response->handle_ime_action_result().has_snapshot();
}

bool isUncertainImeResponse(
    const std::optional<hazkey::ResponseEnvelope>& response) {
    if (!response.has_value() ||
        response->status() == hazkey::UNSPECIFIED ||
        response->status() == hazkey::SESSION_NOT_FOUND ||
        response->status() == hazkey::RETRYABLE_TRANSPORT_ERROR) {
        return true;
    }
    if (response->status() != hazkey::SUCCESS) {
        return false;
    }
    // Outer SUCCESS without a complete inner result cannot prove whether the
    // mutation was applied. Likewise, an inner transport/session status is not
    // a semantic rejection. Neither permits fallback nor journal ack.
    if (!response->has_handle_ime_action_result()) {
        return true;
    }
    const auto inner = response->handle_ime_action_result().status();
    return inner == hazkey::UNSPECIFIED ||
           inner == hazkey::SESSION_NOT_FOUND ||
           inner == hazkey::RETRYABLE_TRANSPORT_ERROR ||
           (inner == hazkey::SUCCESS &&
            !response->handle_ime_action_result().has_snapshot());
}
}

HazkeyClientContextTransition evaluateHazkeyClientContextTransition(
    const HazkeyClientContext& previous, const HazkeyClientContext& next) {
    const bool contextChanged =
        previous.program != next.program || previous.frontend != next.frontend ||
        previous.secureInput != next.secureInput;
    const bool enteredSecure = !previous.secureInput && next.secureInput;
    const bool crossedSecureBoundary = previous.secureInput != next.secureInput;
    const bool crossedProgramBoundary = previous.program != next.program;
    return HazkeyClientContextTransition{
        .contextChanged = contextChanged,
        .enteredSecure = enteredSecure,
        .clearPreedit = crossedSecureBoundary || crossedProgramBoundary,
        .reopenSession = contextChanged,
        .allowSurroundingText = !next.secureInput,
    };
}

bool HazkeySessionClient::open(HazkeyClientSession& session, bool tryConnect) {
    hazkey::RequestEnvelope request;
    auto* openSession = request.mutable_open_session();
    openSession->set_client_feature_bits(currentClientFeatures);
    auto* client = openSession->mutable_client();
    client->set_program(session.context_.program);
    client->set_frontend(session.context_.frontend);
    client->set_secure_input(session.context_.secureInput);

    const auto response = transport_(request, tryConnect);
    if (!response.has_value() || response->status() != hazkey::SUCCESS ||
        !response->has_open_session_result() ||
        response->open_session_result().session_id().empty()) {
        return false;
    }
    session.id_ = response->open_session_result().session_id();
    const auto& result = response->open_session_result();
    session.capabilities_.protocolVersion =
        result.protocol_version() == 0 ? 1 : result.protocol_version();
    session.capabilities_.featureBits = result.feature_bits();
    session.capabilities_.maxSnapshotVersion = result.max_snapshot_version();
    session.capabilities_.recoverySupport = result.recovery_support();
    session.capabilities_.idempotentRequestSupport =
        result.idempotent_request_support();
    session.capabilities_.persistentLearningAvailable =
        result.has_persistent_learning_available()
            ? std::optional<bool>(result.persistent_learning_available())
            : std::nullopt;
    session.revision_ = 0;
    return true;
}

bool HazkeySessionClient::close(HazkeyClientSession& session,
                                bool tryConnect) {
    if (session.id_.empty()) {
        return session.journal_.pending().empty() &&
               session.replayedEffects_.empty();
    }
    // A close response has no SessionSnapshot to which replayed effects can be
    // attached. Refuse to tear the session down until its owner has flushed and
    // applied every deferred action/effect.
    if (!session.journal_.pending().empty() ||
        !session.replayedEffects_.empty()) {
        return false;
    }
    hazkey::RequestEnvelope request;
    request.mutable_close_session()->set_session_id(session.id_);
    // Session teardown is a lifecycle operation. The production connector
    // gives this callback a short deadline and keeps any partial exchange
    // synchronized for the next ordinary request.
    const auto response = lifecycleTransport_(request, tryConnect);
    if (!response.has_value()) {
        // lifecycleTransport takes connector ownership before it returns null;
        // alternatively the shared socket has already been disconnected, which
        // also removes this server session. Never leave a dead local namespace
        // that can make every later context replacement fail forever.
        session.id_.clear();
        session.revision_ = 0;
        return false;
    }
    const bool closed = response.has_value() &&
                        (response->status() == hazkey::SUCCESS ||
                         response->status() == hazkey::SESSION_NOT_FOUND);
    if (closed) {
        session.id_.clear();
        session.revision_ = 0;
    }
    return closed;
}

void HazkeySessionClient::abandonUnconfirmedInput(HazkeyClientSession& session) {
    finalizeWithoutUITarget(session, false);
}

void HazkeySessionClient::discardServerNamespacePreservingRecovery(
    HazkeyClientSession& session) {
    if (session.id_.empty()) {
        return;
    }
    // A restore/fallback response can be transport-uncertain even though the
    // server processed it. Dispose that replacement namespace so hidden
    // composition or direct-commit learning cannot survive. Unlike a true UI
    // abandonment, retain the old checkpoint, visible fallback, journal,
    // confirmed effects, and global ledger for a later recovery attempt.
    if (!session.context_.secureInput) {
        hazkey::RequestEnvelope discard;
        discard.set_session_id(session.id_);
        auto* action = discard.mutable_handle_ime_action();
        action->set_request_id(
            "fcitx-uncertain-recovery-discard-" +
            std::to_string(nextRequestID.fetch_add(1)));
        action->set_expected_revision(session.revision_);
        action->mutable_resolve_pending_learning()->set_commit(false);
        (void)lifecycleTransport_(discard, false);
    }
    hazkey::RequestEnvelope closeRequest;
    closeRequest.mutable_close_session()->set_session_id(session.id_);
    (void)lifecycleTransport_(closeRequest, false);
    session.id_.clear();
    session.revision_ = 0;
}

void HazkeySessionClient::finalizeWithoutUITarget(
    HazkeyClientSession& session, bool preferredCommit) {
    bool commit = preferredCommit;
    // A decision already journaled by a user boundary (notably Backspace)
    // precedes later lifecycle defaults and must remain authoritative.
    for (const auto& entry : session.journal_.pending()) {
        hazkey::commands::HandleImeAction action;
        if (action.ParseFromString(entry.serializedAction) &&
            action.has_resolve_pending_learning()) {
            commit = action.resolve_pending_learning().commit();
            break;
        }
    }
    if (!session.id_.empty()) {
        // Never send an ordinary explicit close first: the server intentionally
        // commits staged learning on that path. Hand off discard then close as
        // two connector-owned lifecycle frames. They remain ordered behind any
        // partially written request, while their responses/effects are consumed
        // outside the abandoned InputContext and cannot leak into a new field.
        if (!session.context_.secureInput) {
            hazkey::RequestEnvelope discard;
            discard.set_session_id(session.id_);
            auto* action = discard.mutable_handle_ime_action();
            action->set_request_id(
                "fcitx-abandon-" +
                std::to_string(nextRequestID.fetch_add(1)));
            action->set_expected_revision(session.revision_);
            action->mutable_resolve_pending_learning()->set_commit(commit);
            (void)lifecycleTransport_(discard, false);
        }
        hazkey::RequestEnvelope closeRequest;
        closeRequest.mutable_close_session()->set_session_id(session.id_);
        (void)lifecycleTransport_(closeRequest, false);
    }
    // Whether close was confirmed, queued, or the session required a discard
    // handoff, this local object must never reuse the old namespace after an
    // abandonment boundary.
    session.id_.clear();
    session.revision_ = 0;
    session.recoveryCheckpoint_.clear();
    session.fallbackComposition_.clear();
    session.reconversionFallbackUnsafe_ = false;
    session.unicodeFallbackUnsafe_ = false;
    session.directFallbackUnsafe_ = false;
    session.presentationFallbackUnsafe_ = false;
    session.journal_.clear();
    session.clearEffects();
}

bool HazkeySessionClient::updateContext(HazkeyClientSession& session,
                                        HazkeyClientContext context) {
    const auto transition =
        evaluateHazkeyClientContextTransition(session.context_, context);
    if (!transition.contextChanged && !session.id_.empty()) {
        if (session.capabilities_.supportsV2()) {
            return true;
        }
        if (!close(session, false)) {
            return false;
        }
    }
    if (transition.contextChanged) {
        if (!close(session, false)) {
            return false;
        }
        if (transition.clearPreedit) {
            session.recoveryCheckpoint_.clear();
            session.fallbackComposition_.clear();
            session.reconversionFallbackUnsafe_ = false;
            session.unicodeFallbackUnsafe_ = false;
            session.directFallbackUnsafe_ = false;
            session.presentationFallbackUnsafe_ = false;
            session.journal_.clear();
            session.clearEffects();
        }
        session.context_ = std::move(context);
    }
    if (!open(session, true)) {
        return false;
    }
    if (!session.capabilities_.supportsV2() || session.context_.secureInput ||
        transition.clearPreedit) {
        return true;
    }

    const bool hadRecoveryMaterial = !session.recoveryCheckpoint_.empty() ||
                                     !session.fallbackComposition_.empty();
    bool restored = false;
    bool explicitlyRejected = !session.fallbackComposition_.empty() &&
                              !session.canUseStoredTextFallback();
    if (!session.recoveryCheckpoint_.empty()) {
        hazkey::commands::HandleImeAction restore;
        restore.set_request_id(
            "fcitx-context-restore-" +
            std::to_string(nextRequestID.fetch_add(1)));
        restore.set_expected_revision(session.revision());
        restore.mutable_restore_checkpoint()->set_opaque_state(
            session.recoveryCheckpoint_);
        const auto response = executeV2(session, restore, true, false);
        restored = isSuccessfulImeResponse(response);
        if (restored) {
            collectResponseEffects(session, *response);
        } else if (isUncertainImeResponse(response)) {
            // Unknown restore completion must never be followed by fallback
            // insertion. Dispose only this replacement server namespace while
            // retaining the old recovery/UI state for the next activation.
            discardServerNamespacePreservingRecovery(session);
            return false;
        } else {
            explicitlyRejected = true;
        }
    }
    bool freshNamespace = false;
    const auto beginFreshNamespace = [&] {
        if (!freshNamespace) {
            session.beginFreshEffectNamespace();
            freshNamespace = true;
        }
    };
    if (!restored && session.canUseStoredTextFallback()) {
        const bool previousHasEffectNamespaceAnchor =
            session.hasEffectNamespaceAnchor_;
        const uint64_t previousRawEffectAnchor = session.rawEffectAnchor_;
        const uint64_t previousGlobalEffectAnchor = session.globalEffectAnchor_;
        const uint64_t previousHighestRawEffectID = session.highestRawEffectID_;
        beginFreshNamespace();
        hazkey::commands::HandleImeAction fallback;
        fallback.set_request_id(
            "fcitx-context-fallback-" +
            std::to_string(nextRequestID.fetch_add(1)));
        fallback.set_expected_revision(session.revision());
        fallback.mutable_insert_text()->set_text(session.fallbackComposition_);
        const auto response = executeV2(session, fallback, true, false);
        restored = isSuccessfulImeResponse(response);
        if (restored) {
            session.reconversionFallbackUnsafe_ = false;
            session.unicodeFallbackUnsafe_ = false;
            session.directFallbackUnsafe_ = false;
            session.presentationFallbackUnsafe_ = false;
            collectResponseEffects(session, *response);
        } else if (isUncertainImeResponse(response)) {
            // No fallback effect was normalized on an uncertain response, so
            // restoring this raw-ID map preserves the prior namespace exactly.
            session.hasEffectNamespaceAnchor_ =
                previousHasEffectNamespaceAnchor;
            session.rawEffectAnchor_ = previousRawEffectAnchor;
            session.globalEffectAnchor_ = previousGlobalEffectAnchor;
            session.highestRawEffectID_ = previousHighestRawEffectID;
            discardServerNamespacePreservingRecovery(session);
            return false;
        } else {
            explicitlyRejected = true;
        }
    }
    if (!restored && hadRecoveryMaterial && explicitlyRejected) {
        beginFreshNamespace();
        // The server definitively rejected every available recovery form.
        // Dispose and detach the replacement namespace before the owner clears
        // its UI, so rejected state cannot reappear on a later activation.
        auto recoveryHandler = session.recoveryHandler_;
        finalizeWithoutUITarget(session, false);
        if (recoveryHandler) {
            recoveryHandler();
        }
        return false;
    }
    if (!restored && session.recoveryCheckpoint_.empty()) {
        // No checkpoint means the new server session starts a fresh Effect-ID
        // namespace, even when pending semantic actions can reconstruct text.
        beginFreshNamespace();
    }
    return restored || (session.recoveryCheckpoint_.empty() &&
                        session.fallbackComposition_.empty());
}

std::optional<hazkey::ResponseEnvelope> HazkeySessionClient::executeV2(
    HazkeyClientSession& session, hazkey::commands::HandleImeAction& action,
    bool tryConnect, bool allowSessionRecovery, bool bestEffort,
    bool lifecycle) {
    if (session.id_.empty() && !open(session, tryConnect)) {
        return std::nullopt;
    }

    const auto send = [&](const hazkey::commands::HandleImeAction& value) {
        hazkey::RequestEnvelope request;
        request.set_session_id(session.id_);
        *request.mutable_handle_ime_action() = value;
        const auto& selectedTransport =
            lifecycle ? lifecycleTransport_
                      : (bestEffort ? bestEffortTransport_ : transport_);
        return selectedTransport(request, tryConnect);
    };
    const auto updateSnapshot = [&](
                                    const hazkey::ResponseEnvelope& response,
                                    const hazkey::commands::HandleImeAction&
                                        snapshotAction) {
        // executeV2 is exclusively a HandleImeAction path. A top-level
        // SessionSnapshot or incomplete inner result is protocol uncertainty,
        // not confirmation of this mutation, and must not overwrite recovery
        // material or the journal checkpoint.
        if (!response.has_handle_ime_action_result() ||
            !response.handle_ime_action_result().has_snapshot()) {
            return;
        }
        const auto* snapshot =
            &response.handle_ime_action_result().snapshot();
        session.setRevision(snapshot->revision());
        const bool trustedRecoveryMaterial =
            response.status() == hazkey::SUCCESS &&
            response.handle_ime_action_result().status() == hazkey::SUCCESS;
        if (!trustedRecoveryMaterial) {
            // Failure snapshots may advance the authoritative revision, but
            // must not erase the last confirmed checkpoint/visible fallback.
            return;
        }
        if (snapshot->phase() == hazkey::IDLE) {
            session.reconversionFallbackUnsafe_ = false;
            session.directFallbackUnsafe_ = false;
        } else if (snapshotAction.has_reconvert() ||
                   snapshot->phase() == hazkey::RECONVERTING) {
            // reconvert normally returns SELECTING immediately, so the
            // originating action—not only the rendered phase—must mark
            // text-only fallback unsafe until the replacement commits or
            // cancels back to IDLE.
            session.reconversionFallbackUnsafe_ = true;
        }
        if (snapshot->phase() != hazkey::IDLE &&
            (snapshotAction.has_commit_unicode_input() ||
             snapshotAction.has_transform_active_segment())) {
            // These actions create direct surfaces that cannot be faithfully
            // reconstructed by ordinary insert_text. Retain the guard until
            // commit/cancel returns the composition to IDLE.
            session.directFallbackUnsafe_ = true;
        }
        // Unicode marker text is unsafe only while that semantic phase is
        // active. Track it even in secure contexts, where persistent fallback
        // text is intentionally absent but local deactivation still needs the
        // semantic guard.
        session.unicodeFallbackUnsafe_ =
            snapshot->phase() == hazkey::UNICODE_INPUT;
        bool hasActivePresentation = false;
        for (const auto& span : snapshot->preedit()) {
            if (span.style() == hazkey::PreeditSpan::ACTIVE) {
                hasActivePresentation = true;
                break;
            }
        }
        session.presentationFallbackUnsafe_ =
            snapshot->phase() != hazkey::COMPOSING ||
            snapshot->candidate_window().items_size() != 0 ||
            hasActivePresentation;
        if (session.context_.secureInput) {
            session.recoveryCheckpoint_.clear();
            session.fallbackComposition_.clear();
            session.journal_.confirmSnapshot("");
        } else if (snapshot->has_recovery()) {
            session.recoveryCheckpoint_ = snapshot->recovery().opaque_state();
        }
        if (!session.context_.secureInput) {
            std::string serializedSnapshot;
            if (snapshot->SerializeToString(&serializedSnapshot)) {
                session.journal_.confirmSnapshot(std::move(serializedSnapshot));
            }
            session.fallbackComposition_.clear();
            for (const auto& span : snapshot->preedit()) {
                session.fallbackComposition_.append(span.text());
            }
        }
    };

    auto response = send(action);
    if (!response.has_value() && !bestEffort && !lifecycle &&
        session.capabilities_.idempotentRequestSupport) {
        response = send(action);
    }

    if (response.has_value() && response->status() == hazkey::SESSION_NOT_FOUND) {
        if (!allowSessionRecovery) {
            // Keep the exact old namespace binding. A later normal replay must
            // resend it, observe SESSION_NOT_FOUND itself, and enter the
            // open/restore/rebind path below.
            return std::nullopt;
        }
        const std::string staleSessionID = session.id_;
        const uint64_t staleRevision = session.revision_;
        session.id_.clear();
        if (!open(session, tryConnect)) {
            // A transient open failure is not destructive recovery. Retain the
            // dead binding so the next normal replay can take the same
            // SESSION_NOT_FOUND recovery route without losing composition.
            session.id_ = staleSessionID;
            session.revision_ = staleRevision;
            return std::nullopt;
        }
        if (!session.capabilities_.supportsV2()) {
            // The replacement was opened but cannot honor journal/recovery
            // semantics. Dispose that live namespace before restoring the old
            // binding; otherwise it becomes an orphan that may retain hidden
            // composition or staged learning.
            discardServerNamespacePreservingRecovery(session);
            session.id_ = staleSessionID;
            session.revision_ = staleRevision;
            return std::nullopt;
        }

        const bool hadRecoveryMaterial =
            !session.recoveryCheckpoint_.empty() ||
            !session.fallbackComposition_.empty();
        const auto retainStaleBinding = [&] {
            session.id_ = staleSessionID;
            session.revision_ = staleRevision;
        };

        bool recovered = false;
        bool recoveryWasExplicitlyRejected =
            !session.fallbackComposition_.empty() &&
            !session.canUseStoredTextFallback();
        if (!session.recoveryCheckpoint_.empty()) {
            hazkey::commands::HandleImeAction restore;
            restore.set_request_id(
                "fcitx-recover-" + std::to_string(nextRequestID.fetch_add(1)));
            restore.set_expected_revision(0);
            restore.mutable_restore_checkpoint()->set_opaque_state(
                session.recoveryCheckpoint_);
            auto restoreResponse = send(restore);
            if (!restoreResponse.has_value() &&
                session.capabilities_.idempotentRequestSupport) {
                restoreResponse = send(restore);
            }
            if (isSuccessfulImeResponse(restoreResponse)) {
                normalizeEffectIDs(session, *restoreResponse);
                collectResponseEffects(session, *restoreResponse);
                updateSnapshot(*restoreResponse, restore);
                recovered = true;
            } else if (isUncertainImeResponse(restoreResponse)) {
                // The restore itself may have succeeded before its response was
                // lost. Never layer a fallback insertion on top of that unknown
                // state; keep all recovery material and retry from the exact old
                // journal namespace on the next normal transaction.
                discardServerNamespacePreservingRecovery(session);
                retainStaleBinding();
                return std::nullopt;
            } else {
                recoveryWasExplicitlyRejected = true;
            }
        }
        if (!recovered) {
            session.setRevision(0);
            if (session.canUseStoredTextFallback() &&
                !session.context_.secureInput) {
                hazkey::commands::HandleImeAction fallback;
                fallback.set_request_id(
                    "fcitx-fallback-" +
                    std::to_string(nextRequestID.fetch_add(1)));
                fallback.set_expected_revision(0);
                fallback.mutable_insert_text()->set_text(
                    session.fallbackComposition_);
                auto fallbackResponse = send(fallback);
                if (!fallbackResponse.has_value() &&
                    session.capabilities_.idempotentRequestSupport) {
                    fallbackResponse = send(fallback);
                }
                if (isSuccessfulImeResponse(fallbackResponse)) {
                    // Fallback reconstruction starts a genuinely fresh server
                    // Effect-ID namespace. Preserve confirmed buffered effects,
                    // but map this response only after resetting the raw-ID map.
                    session.beginFreshEffectNamespace();
                    session.reconversionFallbackUnsafe_ = false;
                    session.unicodeFallbackUnsafe_ = false;
                    session.directFallbackUnsafe_ = false;
                    session.presentationFallbackUnsafe_ = false;
                    normalizeEffectIDs(session, *fallbackResponse);
                    collectResponseEffects(session, *fallbackResponse);
                    updateSnapshot(*fallbackResponse, fallback);
                    recovered = true;
                } else if (isUncertainImeResponse(fallbackResponse)) {
                    discardServerNamespacePreservingRecovery(session);
                    retainStaleBinding();
                    return std::nullopt;
                } else {
                    recoveryWasExplicitlyRejected = true;
                }
            }
            if (!recovered && hadRecoveryMaterial &&
                recoveryWasExplicitlyRejected) {
                // This replacement session may contain a partially processed
                // fallback, but the server definitively rejected the recovery
                // sequence. Dispose it before clearing local recovery/UI state.
                auto recoveryHandler = session.recoveryHandler_;
                finalizeWithoutUITarget(session, false);
                if (recoveryHandler) {
                    recoveryHandler();
                }
                return std::nullopt;
            }
            if (!recovered && !hadRecoveryMaterial) {
                session.beginFreshEffectNamespace();
                if (session.recoveryHandler_) {
                    session.recoveryHandler_();
                }
            }
        }

        action.set_expected_revision(session.revision());
        response = send(action);
        if (!response.has_value() &&
            session.capabilities_.idempotentRequestSupport) {
            response = send(action);
        }
    }

    if (!response.has_value()) {
        return response;
    }
    if (response->status() == hazkey::SESSION_NOT_FOUND) {
        // The permitted recovery was exhausted, but retaining this latest
        // namespace lets the next normal replay observe SESSION_NOT_FOUND and
        // recover once more. Clearing it here would open without restore.
        return std::nullopt;
    }
    const bool trustedSuccess =
        response->status() == hazkey::SUCCESS &&
        response->has_handle_ime_action_result() &&
        response->handle_ime_action_result().status() == hazkey::SUCCESS;
    if (!trustedSuccess) {
        // Failure snapshots are useful for revision/UI recovery but are not
        // authoritative for external effects. Strip server-provided effects
        // here; transactV2 may subsequently attach only effects collected from
        // separately confirmed journal replay.
        if (response->has_handle_ime_action_result() &&
            response->handle_ime_action_result().has_snapshot()) {
            response->mutable_handle_ime_action_result()
                ->mutable_snapshot()
                ->clear_effects();
        }
        if (response->has_session_snapshot()) {
            response->mutable_session_snapshot()->clear_effects();
        }
    }
    // Normalize only effects that can actually be delivered. Mapping IDs from
    // an untrusted snapshot before stripping them creates holes in the global
    // monotonic ledger and can suppress a later confirmed effect.
    normalizeEffectIDs(session, *response);
    updateSnapshot(*response, action);

    return response;
}

bool HazkeySessionClient::replayPendingV2(HazkeyClientSession& session,
                                         bool tryConnect,
                                         bool lifecycle,
                                         std::optional<hazkey::ResponseEnvelope>*
                                             terminalFailure,
                                         bool* confirmedAfterTerminalFailure) {
    const auto pending = session.journal_.pending();
    for (const auto& entry : pending) {
        hazkey::commands::HandleImeAction action;
        if (!action.ParseFromString(entry.serializedAction)) {
            // A malformed in-memory entry cannot have reached the server and
            // must not block all future input in this process.
            session.journal_.acknowledge(entry.requestID);
            continue;
        }
        if (!entry.sent || entry.sessionID != session.id_) {
            // This entry was queued behind an earlier action and has never
            // touched this server request-cache namespace. It may safely
            // inherit the revision confirmed by the preceding replay.
            action.set_expected_revision(session.revision());
            std::string serializedAction;
            if (!action.SerializeToString(&serializedAction)) {
                return false;
            }
            if (entry.sent) {
                if (!session.journal_.replace(
                        entry.requestID,
                        HazkeyJournalEntry{
                            .requestID = entry.requestID,
                            .serializedAction = std::move(serializedAction),
                            .expectedRevision = action.expected_revision(),
                            .sessionID = session.id_,
                            .sent = false,
                        })) {
                    return false;
                }
            } else if (!session.journal_.rebaseUnsent(
                           entry.requestID, std::move(serializedAction),
                           action.expected_revision(), session.id_)) {
                return false;
            }
        }
        const bool learningResolution =
            action.has_resolve_pending_learning();
        const auto response =
            executeJournaledV2(session, std::move(action), tryConnect, true,
                               lifecycle);
        if (isSuccessfulImeResponse(response) && terminalFailure != nullptr &&
            terminalFailure->has_value() &&
            confirmedAfterTerminalFailure != nullptr) {
            *confirmedAfterTerminalFailure = true;
        }
        if (!isSuccessfulImeResponse(response)) {
            // A generic semantic rejection is terminal and was acknowledged by
            // executeJournaledV2. It must not pin the journal head forever.
            // Learning resolution is different: only SUCCESS proves that the
            // staged transaction received its intended disposition.
            if (learningResolution || isUncertainImeResponse(response) ||
                isStaleImeResponse(response)) {
                return false;
            }
            if (terminalFailure != nullptr) {
                *terminalFailure = response;
                // The rejected entry was acknowledged and therefore drained.
                // Continue with later ordered entries, then return this failure
                // envelope once the journal is empty.
                continue;
            }
        }
    }
    return true;
}

std::optional<hazkey::ResponseEnvelope>
HazkeySessionClient::executeJournaledV2(
    HazkeyClientSession& session,
    hazkey::commands::HandleImeAction action,
    bool tryConnect,
    bool collectEffects,
    bool lifecycle) {
    std::string requestID = action.request_id();
    for (int staleAttempt = 0; staleAttempt < 2; ++staleAttempt) {
        if (!session.journal_.markSent(requestID)) {
            return std::nullopt;
        }
        const std::string boundSessionID = session.id_;
        auto response = executeV2(session, action, tryConnect,
                                  !lifecycle, false, lifecycle);

        // SESSION_NOT_FOUND creates a fresh server-side idempotency namespace.
        // executeV2 rebases the action before sending it there; persist that
        // exact wire binding so a lost response can be replayed byte-for-byte.
        if (!session.id_.empty() && session.id_ != boundSessionID) {
            std::string reboundAction;
            if (!action.SerializeToString(&reboundAction) ||
                !session.journal_.rebindSent(
                    requestID, std::move(reboundAction),
                    action.expected_revision(), session.id_)) {
                return std::nullopt;
            }
        }

        if (isStaleImeResponse(response)) {
            // The old ID is now permanently bound to the stale result in the
            // server cache. Replace it in-place before retrying; if the fresh
            // response is lost, the journal retains precisely the fresh wire
            // envelope rather than resurrecting the poisoned ID.
            action.set_request_id(
                "fcitx-" + std::to_string(nextRequestID.fetch_add(1)));
            action.set_expected_revision(session.revision());
            std::string serializedAction;
            if (!action.SerializeToString(&serializedAction)) {
                return response;
            }
            HazkeyJournalEntry replacement{
                .requestID = action.request_id(),
                .serializedAction = std::move(serializedAction),
                .expectedRevision = action.expected_revision(),
                .sessionID = session.id_,
                .sent = false,
            };
            if (!session.journal_.replace(requestID, std::move(replacement))) {
                return response;
            }
            requestID = action.request_id();
            if (staleAttempt == 1) {
                return response;
            }
            continue;
        }

        if (!isSuccessfulImeResponse(response)) {
            if (isUncertainImeResponse(response)) {
                // Unknown completion is never acknowledged or rebound to a
                // fresh semantic ID. Preserve the exact wire envelope for the
                // next ordered replay.
            } else if (response.has_value() &&
                action.has_resolve_pending_learning()) {
                // A server cache binds non-success just as permanently as
                // success. Keep the decision fail-closed, but replace the
                // terminal ID before a future attempt so it can never pretend
                // that the same cached envelope later succeeded.
                action.set_request_id(
                    "fcitx-learning-retry-" +
                    std::to_string(nextRequestID.fetch_add(1)));
                action.set_expected_revision(session.revision());
                std::string serializedAction;
                if (action.SerializeToString(&serializedAction)) {
                    (void)session.journal_.replace(
                        requestID,
                        HazkeyJournalEntry{
                            .requestID = action.request_id(),
                            .serializedAction = std::move(serializedAction),
                            .expectedRevision = action.expected_revision(),
                            .sessionID = session.id_,
                            .sent = false,
                        });
                }
            } else if (response.has_value()) {
                session.journal_.acknowledge(requestID);
            }
            return response;
        }
        if (collectEffects) {
            collectResponseEffects(session, *response);
        }
        session.journal_.acknowledge(requestID);
        return response;
    }
    return std::nullopt;
}

bool HazkeySessionClient::hasPendingLearningResolution(
    const HazkeyClientSession& session) const {
    for (const auto& entry : session.journal_.pending()) {
        hazkey::commands::HandleImeAction action;
        if (action.ParseFromString(entry.serializedAction) &&
            action.has_resolve_pending_learning()) {
            return true;
        }
    }
    return false;
}

void HazkeySessionClient::normalizeEffectIDs(
    HazkeyClientSession& session, hazkey::ResponseEnvelope& response) {
    const auto normalize = [&](hazkey::SessionSnapshot* snapshot) {
        for (auto& effect : *snapshot->mutable_effects()) {
            const uint64_t rawID = effect.effect_id();
            if (rawID == 0) {
                continue;
            }
            if (!session.hasEffectNamespaceAnchor_) {
                session.hasEffectNamespaceAnchor_ = true;
                session.rawEffectAnchor_ = rawID;
                session.globalEffectAnchor_ = session.nextGlobalEffectID_;
            }
            if (rawID < session.rawEffectAnchor_) {
                // Older than the first observed ID in this namespace: it can
                // only be a stale replay. Drop fail-closed rather than risking
                // unsigned underflow into another global namespace.
                effect.set_effect_id(0);
                continue;
            }
            const uint64_t delta = rawID - session.rawEffectAnchor_;
            if (delta > std::numeric_limits<uint64_t>::max() -
                            session.globalEffectAnchor_) {
                effect.set_effect_id(0);
                continue;
            }
            const uint64_t globalID = session.globalEffectAnchor_ + delta;
            session.highestRawEffectID_ =
                std::max(session.highestRawEffectID_, rawID);
            if (globalID == std::numeric_limits<uint64_t>::max()) {
                // Exhaustion is practically unreachable, but saturating keeps
                // all later effects fail-closed instead of wrapping to zero.
                session.nextGlobalEffectID_ = globalID;
            } else {
                session.nextGlobalEffectID_ =
                    std::max(session.nextGlobalEffectID_, globalID + 1);
            }
            effect.set_effect_id(globalID);
        }
    };
    if (response.has_handle_ime_action_result() &&
        response.handle_ime_action_result().has_snapshot()) {
        normalize(response.mutable_handle_ime_action_result()
                      ->mutable_snapshot());
    }
    if (response.has_session_snapshot()) {
        normalize(response.mutable_session_snapshot());
    }
}

void HazkeySessionClient::collectResponseEffects(
    HazkeyClientSession& session,
    const hazkey::ResponseEnvelope& response) {
    if (!response.has_handle_ime_action_result() ||
        !response.handle_ime_action_result().has_snapshot()) {
        return;
    }
    for (const auto& effect :
         response.handle_ime_action_result().snapshot().effects()) {
        session.replayedEffects_.push_back(effect);
    }
}

void HazkeySessionClient::attachReplayedEffects(
    HazkeyClientSession& session, hazkey::ResponseEnvelope& response) {
    if (session.replayedEffects_.empty() ||
        !response.has_handle_ime_action_result() ||
        !response.handle_ime_action_result().has_snapshot()) {
        return;
    }

    auto* snapshot =
        response.mutable_handle_ime_action_result()->mutable_snapshot();
    const auto currentEffects = snapshot->effects();
    snapshot->clear_effects();
    for (const auto& effect : session.replayedEffects_) {
        *snapshot->add_effects() = effect;
    }
    for (const auto& effect : currentEffects) {
        *snapshot->add_effects() = effect;
    }
    session.replayedEffects_.clear();
}

std::optional<hazkey::ResponseEnvelope> HazkeySessionClient::transactV2(
    HazkeyClientSession& session, hazkey::commands::HandleImeAction action,
    bool tryConnect) {
    if (session.id_.empty() && !open(session, tryConnect)) {
        return std::nullopt;
    }

    if (session.context_.secureInput) {
        // Secure text is never retained beyond this synchronous transaction.
        session.journal_.clear();
    } else if (!replayPendingV2(session, tryConnect)) {
        if (!session.replayedEffects_.empty()) {
            return makeEffectHandoffResponse(
                session, hazkey::RETRYABLE_TRANSPORT_ERROR);
        }
        return std::nullopt;
    }

    if (action.request_id().empty()) {
        action.set_request_id(
            "fcitx-" + std::to_string(nextRequestID.fetch_add(1)));
    }
    action.set_expected_revision(session.revision());

    const std::string requestID = action.request_id();
    if (session.context_.secureInput) {
        auto response = executeV2(session, action, tryConnect);
        if (isStaleImeResponse(response)) {
            action.set_request_id(
                "fcitx-secure-" +
                std::to_string(nextRequestID.fetch_add(1)));
            action.set_expected_revision(session.revision());
            response = executeV2(session, action, tryConnect);
        }
        return response;
    } else {
        std::string serializedAction;
        if (!action.SerializeToString(&serializedAction)) {
            return std::nullopt;
        }
        if (!session.journal_.record(HazkeyJournalEntry{
            .requestID = requestID,
            .serializedAction = std::move(serializedAction),
            .expectedRevision = action.expected_revision(),
            .sessionID = session.id_,
            .sent = false,
        })) {
            return std::nullopt;
        }
    }

    auto response =
        executeJournaledV2(session, std::move(action), tryConnect, false);
    if (!response.has_value() && !session.replayedEffects_.empty()) {
        return makeEffectHandoffResponse(
            session, hazkey::RETRYABLE_TRANSPORT_ERROR);
    }
    if (response.has_value() && isUncertainImeResponse(response)) {
        if (!session.replayedEffects_.empty()) {
            return makeEffectHandoffResponse(
                session, hazkey::RETRYABLE_TRANSPORT_ERROR);
        }
        return std::nullopt;
    }
    // executeV2 strips effects from terminal failure snapshots. Anything
    // attached here therefore has explicit provenance from an independently
    // confirmed journal replay and remains safe to apply even though the new
    // current action itself failed.
    if (response.has_value()) {
        attachReplayedEffects(session, *response);
        if (!session.replayedEffects_.empty()) {
            auto status = response->status();
            if (status == hazkey::SUCCESS &&
                (!response->has_handle_ime_action_result() ||
                 response->handle_ime_action_result().status() !=
                     hazkey::SUCCESS)) {
                status = hazkey::RETRYABLE_TRANSPORT_ERROR;
            }
            return makeEffectHandoffResponse(session, status);
        }
    }
    return response;
}

std::optional<hazkey::ResponseEnvelope>
HazkeySessionClient::transactV2BestEffort(
    HazkeyClientSession& session,
    hazkey::commands::HandleImeAction action) {
    // A timer must never jump ahead of an unconfirmed semantic key. It also
    // must not become replayable itself: a cancelled live conversion cannot be
    // allowed to run before a later Enter, Space, or edit during recovery.
    if (session.id_.empty() || !session.journal_.pending().empty()) {
        return std::nullopt;
    }
    if (action.request_id().empty()) {
        action.set_request_id(
            "fcitx-best-effort-" +
            std::to_string(nextRequestID.fetch_add(1)));
    }
    action.set_expected_revision(session.revision());
    return executeV2(session, action, false, false, true);
}

std::optional<hazkey::ResponseEnvelope>
HazkeySessionClient::transactV2DurableBestEffort(
    HazkeyClientSession& session,
    hazkey::commands::HandleImeAction action) {
    // Learning is never staged for secure input, and retaining any secure
    // action would violate the journal's privacy contract.
    if (session.context_.secureInput ||
        !action.has_resolve_pending_learning()) {
        return std::nullopt;
    }
    // The first boundary after a commit is authoritative. If it was Backspace
    // (discard), a later navigation key must not replace it with commit merely
    // because the first response has not arrived yet.
    if (hasPendingLearningResolution(session)) {
        return std::nullopt;
    }
    if (action.request_id().empty()) {
        action.set_request_id(
            "fcitx-durable-best-effort-" +
            std::to_string(nextRequestID.fetch_add(1)));
    }
    action.set_expected_revision(session.revision());

    std::string serializedAction;
    if (!action.SerializeToString(&serializedAction)) {
        return std::nullopt;
    }
    if (!session.journal_.record(HazkeyJournalEntry{
        .requestID = action.request_id(),
        .serializedAction = std::move(serializedAction),
        .expectedRevision = action.expected_revision(),
        .sessionID = session.id_,
        .sent = false,
    })) {
        return std::nullopt;
    }

    // Once the server has confirmed staged learning, leaving a discard only in
    // local memory would let idle/capacity eviction commit it when no later key
    // arrives. Hand the journal head to the connector-owned 10ms lifecycle
    // path immediately. Unknown responses remain journaled byte-for-byte.
    hazkey::SessionSnapshot confirmed;
    const bool stagedOnServer =
        !session.journal_.lastSnapshot().empty() &&
        confirmed.ParseFromString(session.journal_.lastSnapshot()) &&
        confirmed.pending_learning();
    if (stagedOnServer && session.journal_.pending().size() == 1) {
        return executeJournaledV2(session, std::move(action), false, false,
                                  true);
    }
    return std::nullopt;
}

std::optional<hazkey::ResponseEnvelope>
HazkeySessionClient::makeFlushResponse(HazkeyClientSession& session) {
    if (session.journal_.lastSnapshot().empty()) {
        return std::nullopt;
    }
    hazkey::SessionSnapshot snapshot;
    if (!snapshot.ParseFromString(session.journal_.lastSnapshot())) {
        return std::nullopt;
    }
    snapshot.clear_effects();

    hazkey::ResponseEnvelope response;
    response.set_status(hazkey::SUCCESS);
    auto* result = response.mutable_handle_ime_action_result();
    result->set_status(hazkey::SUCCESS);
    *result->mutable_snapshot() = std::move(snapshot);
    attachReplayedEffects(session, response);
    return response;
}

std::optional<hazkey::ResponseEnvelope>
HazkeySessionClient::makeEffectHandoffResponse(
    HazkeyClientSession& session, hazkey::StatusCode status) {
    auto response = makeFlushResponse(session);
    if (!response.has_value()) {
        return std::nullopt;
    }
    response->set_status(status);
    response->mutable_handle_ime_action_result()->set_status(status);
    return response;
}

HazkeyFlushResult HazkeySessionClient::flushPendingV2(
    HazkeyClientSession& session, bool tryConnect) {
    if (session.context_.secureInput) {
        session.journal_.clear();
        session.clearEffects();
        return {.completed = true, .response = std::nullopt};
    }
    const bool hadDeferred = !session.journal_.pending().empty() ||
                             !session.replayedEffects_.empty();
    if (!hadDeferred) {
        return {.completed = true, .response = std::nullopt};
    }
    // A bounded lifecycle drain is meaningful only for the currently owned
    // namespace. A recovery flush explicitly opts into the normal transport:
    // it may reconnect, observe SESSION_NOT_FOUND, open/restore, and rebind the
    // journal to the replacement server request-cache namespace.
    if (session.id_.empty() && !tryConnect &&
        !session.journal_.pending().empty()) {
        return {.completed = false, .response = std::nullopt};
    }
    std::optional<hazkey::ResponseEnvelope> terminalFailure;
    bool confirmedAfterTerminalFailure = false;
    const auto terminalFailureWithEffectHandoff = [&]() {
        auto response = terminalFailure;
        if (!response.has_value()) {
            return response;
        }
        auto failureStatus = response->status();
        if (failureStatus == hazkey::SUCCESS) {
            failureStatus = response->has_handle_ime_action_result()
                                ? response->handle_ime_action_result().status()
                                : hazkey::RETRYABLE_TRANSPORT_ERROR;
            if (failureStatus == hazkey::SUCCESS) {
                failureStatus = hazkey::RETRYABLE_TRANSPORT_ERROR;
            }
        }

        const bool failureHasSnapshot =
            response->has_handle_ime_action_result() &&
            response->handle_ime_action_result().has_snapshot();
        hazkey::SessionSnapshot latestSnapshot;
        const bool hasLatestSnapshot =
            !session.journal_.lastSnapshot().empty() &&
            latestSnapshot.ParseFromString(session.journal_.lastSnapshot());
        if (hasLatestSnapshot &&
            (!failureHasSnapshot || confirmedAfterTerminalFailure)) {
            // A later ordered replay succeeded after this earlier rejection.
            // Return its newest snapshot/effects while preserving a failure
            // status, otherwise State would apply the effect and then roll UI
            // state back to the rejected entry's older snapshot.
            auto latest = makeEffectHandoffResponse(session, failureStatus);
            if (latest.has_value()) {
                latest->set_error_message(response->error_message());
                return latest;
            }
        }
        attachReplayedEffects(session, *response);
        if (session.replayedEffects_.empty()) {
            return response;
        }
        return makeEffectHandoffResponse(session, failureStatus);
    };
    if (!replayPendingV2(session, tryConnect, !tryConnect,
                         &terminalFailure,
                         &confirmedAfterTerminalFailure)) {
        if (terminalFailure.has_value()) {
            return {
                .completed = session.journal_.pending().empty(),
                .response = terminalFailureWithEffectHandoff(),
            };
        }
        if (!session.replayedEffects_.empty()) {
            auto replayOnly = makeEffectHandoffResponse(
                session, hazkey::RETRYABLE_TRANSPORT_ERROR);
            if (replayOnly.has_value()) {
                return {
                    .completed = false,
                    .response = std::move(replayOnly),
                };
            }
        }
        return {.completed = false, .response = std::nullopt};
    }
    if (terminalFailure.has_value()) {
        return {
            .completed = session.journal_.pending().empty(),
            .response = terminalFailureWithEffectHandoff(),
        };
    }
    auto response = makeFlushResponse(session);
    if (!response.has_value()) {
        return {.completed = false, .response = std::nullopt};
    }
    return {.completed = true, .response = std::move(response)};
}
