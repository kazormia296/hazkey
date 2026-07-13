#include "hazkey_session_client.h"

#include <utility>

#include <atomic>

namespace {
std::atomic<uint64_t> nextRequestID{1};
constexpr uint64_t scheduleLiveConversionClientFeature = 1ULL << 0;
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
    openSession->set_client_feature_bits(scheduleLiveConversionClientFeature);
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
    session.revision_ = 0;
    return true;
}

bool HazkeySessionClient::close(HazkeyClientSession& session,
                                bool tryConnect) {
    if (session.id_.empty()) {
        return true;
    }
    hazkey::RequestEnvelope request;
    request.mutable_close_session()->set_session_id(session.id_);
    const auto response = transport_(request, tryConnect);
    session.id_.clear();
    session.revision_ = 0;
    return response.has_value() && response->status() == hazkey::SUCCESS;
}

void HazkeySessionClient::abandonUnconfirmedInput(HazkeyClientSession& session) {
    (void)close(session, false);
    session.recoveryCheckpoint_.clear();
    session.fallbackComposition_.clear();
    session.journal_.clear();
    session.clearEffects();
}

bool HazkeySessionClient::updateContext(HazkeyClientSession& session,
                                        HazkeyClientContext context) {
    const auto transition =
        evaluateHazkeyClientContextTransition(session.context_, context);
    if (!transition.contextChanged) {
        return true;
    }
    (void)close(session, false);
    if (transition.clearPreedit) {
        session.recoveryCheckpoint_.clear();
        session.fallbackComposition_.clear();
        session.journal_.clear();
        session.clearEffects();
    }
    session.context_ = std::move(context);
    if (!open(session, true)) {
        return false;
    }
    if (!session.capabilities_.supportsV2() || session.context_.secureInput ||
        transition.clearPreedit) {
        return true;
    }

    bool restored = false;
    if (!session.recoveryCheckpoint_.empty()) {
        hazkey::commands::HandleImeAction restore;
        restore.set_request_id(
            "fcitx-context-restore-" +
            std::to_string(nextRequestID.fetch_add(1)));
        restore.set_expected_revision(session.revision());
        restore.mutable_restore_checkpoint()->set_opaque_state(
            session.recoveryCheckpoint_);
        const auto response = executeV2(session, std::move(restore), true);
        restored = response.has_value() && response->status() == hazkey::SUCCESS &&
                   response->has_handle_ime_action_result() &&
                   response->handle_ime_action_result().status() == hazkey::SUCCESS;
    }
    if (!restored && !session.fallbackComposition_.empty()) {
        session.clearEffects();
        hazkey::commands::HandleImeAction fallback;
        fallback.set_request_id(
            "fcitx-context-fallback-" +
            std::to_string(nextRequestID.fetch_add(1)));
        fallback.set_expected_revision(session.revision());
        fallback.mutable_insert_text()->set_text(session.fallbackComposition_);
        const auto response = executeV2(session, std::move(fallback), true);
        restored = response.has_value() && response->status() == hazkey::SUCCESS &&
                   response->has_handle_ime_action_result() &&
                   response->handle_ime_action_result().status() == hazkey::SUCCESS;
    }
    if (!restored && session.recoveryHandler_) {
        session.clearEffects();
        session.recoveryHandler_();
    }
    if (!restored && session.recoveryCheckpoint_.empty()) {
        // No checkpoint means the new server session starts a fresh Effect-ID
        // namespace, even when pending semantic actions can reconstruct text.
        session.clearEffects();
    }
    return restored || (session.recoveryCheckpoint_.empty() &&
                        session.fallbackComposition_.empty());
}

std::optional<hazkey::ResponseEnvelope> HazkeySessionClient::executeV2(
    HazkeyClientSession& session, hazkey::commands::HandleImeAction action,
    bool tryConnect, bool allowSessionRecovery, bool bestEffort) {
    if (session.id_.empty() && !open(session, tryConnect)) {
        return std::nullopt;
    }

    const auto send = [&](const hazkey::commands::HandleImeAction& value) {
        hazkey::RequestEnvelope request;
        request.set_session_id(session.id_);
        *request.mutable_handle_ime_action() = value;
        return (bestEffort ? bestEffortTransport_ : transport_)(request,
                                                                tryConnect);
    };
    const auto updateSnapshot = [&](const hazkey::ResponseEnvelope& response) {
        const hazkey::SessionSnapshot* snapshot = nullptr;
        if (response.has_handle_ime_action_result() &&
            response.handle_ime_action_result().has_snapshot()) {
            snapshot = &response.handle_ime_action_result().snapshot();
        } else if (response.has_session_snapshot()) {
            snapshot = &response.session_snapshot();
        }
        if (snapshot == nullptr) {
            return;
        }
        session.setRevision(snapshot->revision());
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
    if (!response.has_value() && !bestEffort &&
        session.capabilities_.idempotentRequestSupport) {
        response = send(action);
    }

    if (response.has_value() && response->status() == hazkey::SESSION_NOT_FOUND) {
        if (!allowSessionRecovery) {
            return std::nullopt;
        }
        session.id_.clear();
        if (!open(session, tryConnect) || !session.capabilities_.supportsV2()) {
            if (session.recoveryHandler_) {
                session.recoveryHandler_();
            }
            return std::nullopt;
        }

        bool recovered = false;
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
            if (restoreResponse.has_value() &&
                restoreResponse->status() == hazkey::SUCCESS &&
                restoreResponse->has_handle_ime_action_result() &&
                restoreResponse->handle_ime_action_result().status() ==
                    hazkey::SUCCESS) {
                updateSnapshot(*restoreResponse);
                recovered = true;
            }
        }
        if (!recovered) {
            session.setRevision(0);
            session.recoveryCheckpoint_.clear();
            session.clearEffects();
            if (!session.fallbackComposition_.empty() &&
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
                if (fallbackResponse.has_value() &&
                    fallbackResponse->status() == hazkey::SUCCESS &&
                    fallbackResponse->has_handle_ime_action_result() &&
                    fallbackResponse->handle_ime_action_result().status() ==
                        hazkey::SUCCESS) {
                    updateSnapshot(*fallbackResponse);
                    recovered = true;
                }
            }
            if (!recovered && session.recoveryHandler_) {
                session.fallbackComposition_.clear();
                session.recoveryHandler_();
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
        return std::nullopt;
    }
    updateSnapshot(*response);

    if (response->has_handle_ime_action_result() &&
        response->handle_ime_action_result().status() ==
            hazkey::STALE_REVISION) {
        if (bestEffort) {
            return response;
        }
        action.set_request_id(
            "fcitx-" + std::to_string(nextRequestID.fetch_add(1)));
        action.set_expected_revision(session.revision());
        auto retried = send(action);
        if (retried.has_value()) {
            updateSnapshot(*retried);
            return retried;
        }
        return std::nullopt;
    }
    return response;
}

bool HazkeySessionClient::replayPendingV2(HazkeyClientSession& session,
                                         bool tryConnect) {
    const auto pending = session.journal_.pending();
    for (const auto& entry : pending) {
        hazkey::commands::HandleImeAction action;
        if (!action.ParseFromString(entry.serializedAction)) {
            // A malformed in-memory entry cannot have reached the server and
            // must not block all future input in this process.
            session.journal_.acknowledge(entry.requestID);
            continue;
        }
        const auto response = executeV2(session, std::move(action), tryConnect);
        if (!response.has_value()) {
            return false;
        }
        session.journal_.acknowledge(entry.requestID);
    }
    return true;
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
        return std::nullopt;
    }

    if (action.request_id().empty()) {
        action.set_request_id(
            "fcitx-" + std::to_string(nextRequestID.fetch_add(1)));
    }
    action.set_expected_revision(session.revision());

    const std::string requestID = action.request_id();
    if (!session.context_.secureInput) {
        std::string serializedAction;
        if (!action.SerializeToString(&serializedAction)) {
            return std::nullopt;
        }
        session.journal_.record(HazkeyJournalEntry{
            .requestID = requestID,
            .serializedAction = std::move(serializedAction),
            .expectedRevision = action.expected_revision(),
        });
    }

    auto response = executeV2(session, std::move(action), tryConnect);
    if (response.has_value() && !session.context_.secureInput) {
        session.journal_.acknowledge(requestID);
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
    return executeV2(session, std::move(action), false, false, true);
}
