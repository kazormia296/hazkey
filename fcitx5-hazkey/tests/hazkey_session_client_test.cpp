#include <chrono>
#include <cstdlib>
#include <iostream>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "base.pb.h"
#include "hazkey_session_client.h"

namespace {

class FakeTransport {
   public:
    std::vector<hazkey::RequestEnvelope> requests;
    std::vector<bool> tryConnectValues;
    std::vector<std::optional<hazkey::ResponseEnvelope>> responses;

    std::optional<hazkey::ResponseEnvelope> transact(
        const hazkey::RequestEnvelope& request, bool tryConnect) {
        requests.push_back(request);
        tryConnectValues.push_back(tryConnect);
        if (responses.empty()) {
            return std::nullopt;
        }
        auto response = responses.front();
        responses.erase(responses.begin());
        return response;
    }
};

[[noreturn]] void fail(const std::string& message) {
    std::cerr << message << '\n';
    std::exit(1);
}

void expect(bool condition, const std::string& message) {
    if (!condition) {
        fail(message);
    }
}

hazkey::ResponseEnvelope openSuccess(const std::string& sessionId) {
    hazkey::ResponseEnvelope response;
    response.set_status(hazkey::SUCCESS);
    auto* result = response.mutable_open_session_result();
    result->set_session_id(sessionId);
    result->set_protocol_version(2);
    result->set_feature_bits(0b1111);
    result->set_max_snapshot_version(1);
    result->set_recovery_support(true);
    result->set_idempotent_request_support(true);
    return response;
}

hazkey::ResponseEnvelope openLegacySuccess(const std::string& sessionId) {
    auto response = openSuccess(sessionId);
    auto* result = response.mutable_open_session_result();
    result->set_protocol_version(1);
    result->set_idempotent_request_support(false);
    result->set_recovery_support(false);
    return response;
}

hazkey::ResponseEnvelope status(hazkey::StatusCode code) {
    hazkey::ResponseEnvelope response;
    response.set_status(code);
    return response;
}

hazkey::ResponseEnvelope v2Response(
    uint64_t revision, const std::string& checkpoint = "",
    hazkey::StatusCode code = hazkey::SUCCESS,
    const std::string& preedit = "") {
    hazkey::ResponseEnvelope response;
    response.set_status(code);
    auto* result = response.mutable_handle_ime_action_result();
    result->set_status(code);
    auto* snapshot = result->mutable_snapshot();
    snapshot->set_revision(revision);
    snapshot->set_phase(hazkey::COMPOSING);
    if (!preedit.empty()) {
        snapshot->add_preedit()->set_text(preedit);
    }
    if (!checkpoint.empty()) {
        snapshot->mutable_recovery()->set_revision(revision);
        snapshot->mutable_recovery()->set_opaque_state(checkpoint);
    }
    return response;
}

void addCommitEffect(hazkey::ResponseEnvelope& response, uint64_t effectID,
                     const std::string& text) {
    auto* effect = response.mutable_handle_ime_action_result()
                       ->mutable_snapshot()
                       ->add_effects();
    effect->set_effect_id(effectID);
    effect->set_type(hazkey::ClientEffect::COMMIT_TEXT);
    effect->set_text(text);
}

HazkeyClientContext context(bool secure = false) {
    return HazkeyClientContext{
        .program = "grimodex",
        .frontend = "wayland",
        .secureInput = secure,
    };
}

hazkey::commands::HandleImeAction inputAction() {
    hazkey::commands::HandleImeAction action;
    action.mutable_insert_text()->set_text("a");
    return action;
}

void reopensAndRetriesOnlyOnce() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-1"),
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-2"),
        v2Response(1),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());

    auto response = client.transactV2(session, inputAction());

    expect(response.has_value(), "retried command must return a response");
    expect(response->status() == hazkey::SUCCESS, "retried command must succeed");
    expect(transport.requests.size() == 4, "exactly four RPCs are expected");
    expect(transport.requests[0].has_open_session(), "first RPC must open");
    expect(transport.requests[0].open_session().client_feature_bits() == 3,
           "current clients must advertise delayed effects and staged learning");
    expect(transport.requests[1].session_id() == "session-1", "first command uses session-1");
    expect(transport.requests[2].has_open_session(), "third RPC must reopen");
    expect(transport.requests[3].session_id() == "session-2", "retry uses session-2");
}

void stopsAfterTheSingleRetry() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-1"),
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-2"),
        status(hazkey::SESSION_NOT_FOUND),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());

    auto response = client.transactV2(session, inputAction());

    expect(!response.has_value(),
           "a second SESSION_NOT_FOUND must stay pending without looping");
    expect(transport.requests.size() == 4, "no third open or command is allowed");
    expect(session.pendingActionCount() == 1,
           "an unacknowledged action must remain journaled");
}

void preservesContextWhenReopening() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-1"),
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-2"),
        v2Response(1),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(true));

    (void)client.transactV2(session, inputAction());

    for (const auto index : {0U, 2U}) {
        const auto& clientContext = transport.requests[index].open_session().client();
        expect(clientContext.program() == "grimodex", "program must survive reopen");
        expect(clientContext.frontend() == "wayland", "frontend must survive reopen");
        expect(clientContext.secure_input(), "secure flag must survive reopen");
    }
}

void neverMixesTwoSessionIds() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-a"),
        openSuccess("session-b"),
        v2Response(1),
        v2Response(1),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession sessionA(context());
    HazkeyClientSession sessionB(context());

    expect(client.open(sessionA), "session A must open");
    expect(client.open(sessionB), "session B must open");
    (void)client.transactV2(sessionA, inputAction());
    (void)client.transactV2(sessionB, inputAction());

    expect(transport.requests[2].session_id() == "session-a", "A must retain its id");
    expect(transport.requests[3].session_id() == "session-b", "B must retain its id");
}

void secureContextTransitionClearsBeforeReopening() {
    const auto normal = context(false);
    const auto secure = context(true);

    const auto enteredSecure = evaluateHazkeyClientContextTransition(normal, secure);
    expect(enteredSecure.contextChanged, "secure transition must change context");
    expect(enteredSecure.enteredSecure, "secure transition must be detected");
    expect(enteredSecure.clearPreedit, "secure transition must clear client preedit");
    expect(enteredSecure.reopenSession, "secure transition must reopen the session");
    expect(!enteredSecure.allowSurroundingText, "secure context must hide surrounding text");

    const auto stillSecure = evaluateHazkeyClientContextTransition(secure, secure);
    expect(!stillSecure.contextChanged, "identical secure context must be stable");
    expect(!stillSecure.reopenSession, "identical secure context must not reopen");
    expect(!stillSecure.allowSurroundingText, "secure context must stay private");

    const auto leftSecure = evaluateHazkeyClientContextTransition(secure, normal);
    expect(leftSecure.contextChanged, "leaving secure input must change context");
    expect(!leftSecure.enteredSecure, "leaving secure input is not an entry");
    expect(leftSecure.clearPreedit,
           "leaving secure input must discard any secure-field composition");
    expect(leftSecure.reopenSession, "leaving secure input must reopen the session");
    expect(leftSecure.allowSurroundingText, "normal context may send surrounding text");

    auto otherProgram = normal;
    otherProgram.program = "firefox";
    const auto changedProgram =
        evaluateHazkeyClientContextTransition(normal, otherProgram);
    expect(changedProgram.clearPreedit,
           "a program boundary must not recover another application's composition");

    auto otherFrontend = normal;
    otherFrontend.frontend = "x11";
    const auto changedFrontend =
        evaluateHazkeyClientContextTransition(normal, otherFrontend);
    expect(!changedFrontend.clearPreedit,
           "a frontend transport change alone may preserve non-secure composition");
}

void notifiesTheOwnerWhenAStatefulRequestReopensTheSession() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-1"),
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-2"),
        v2Response(1),
        v2Response(2),
    };
    int recoveryCount = 0;
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(), [&recoveryCount] {
        ++recoveryCount;
    });

    expect(client.open(session), "initial session must open");
    expect(recoveryCount == 0, "initial open is not recovery");
    const auto recovered = client.transactV2(session, inputAction());
    expect(recovered.has_value() && recovered->status() == hazkey::SUCCESS,
           "replayed request must succeed");
    expect(recoveryCount == 1,
           "owner must invalidate local composition exactly once after recovery");

    (void)client.transactV2(session, inputAction());
    expect(recoveryCount == 1, "ordinary requests must not notify recovery");
}

void replacesSessionWhenClientContextChanges() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-1"),
        status(hazkey::SUCCESS),
        openSuccess("session-2"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(false));
    expect(client.open(session), "initial session must open");

    expect(client.updateContext(session, context(true)),
           "context update must reopen the session");

    expect(transport.requests.size() == 3, "context update is close plus open");
    expect(transport.requests[1].has_close_session(), "old session must close");
    expect(transport.requests[1].close_session().session_id() == "session-1",
           "close must target the old id");
    expect(transport.requests[2].open_session().client().secure_input(),
           "new open must carry secure context");
    expect(session.id() == "session-2", "session must expose the replacement id");
    expect(session.context().secureInput, "session must retain replacement context");
}

void tracksV2CapabilitiesRevisionAndEffectDeduplication() {
    FakeTransport transport;
    hazkey::ResponseEnvelope response;
    response.set_status(hazkey::SUCCESS);
    response.mutable_handle_ime_action_result()->set_status(hazkey::SUCCESS);
    response.mutable_handle_ime_action_result()->mutable_snapshot()->set_revision(7);
    transport.responses = {openSuccess("session-v2"), response};

    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "v2 session must open");
    expect(session.capabilities().supportsV2(), "negotiated v2 must be advertised");

    hazkey::commands::HandleImeAction action;
    action.set_request_id("request-1");
    action.mutable_insert_text()->set_text("かな");
    const auto result = client.transactV2(session, action);
    expect(result.has_value() && result->status() == hazkey::SUCCESS,
           "v2 action must return its response");
    expect(transport.requests.back().handle_ime_action().expected_revision() == 0,
           "first v2 action must use revision zero");
    expect(session.revision() == 7, "snapshot revision must be recorded");
    expect(session.shouldApplyEffect(11), "first effect application must claim the ID");
    expect(!session.shouldApplyEffect(11), "duplicate effect application must be ignored");
}

void retriesLostV2ResponsesWithTheSameRequestID() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        std::nullopt,
        v2Response(1, "checkpoint-1"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "v2 session must open");

    hazkey::commands::HandleImeAction action;
    action.mutable_insert_text()->set_text("a");
    const auto result = client.transactV2(session, action);

    expect(result.has_value() && result->status() == hazkey::SUCCESS,
           "a response-loss retry must succeed");
    expect(transport.requests.size() == 3,
           "response loss must retry exactly once");
    expect(transport.requests[1].handle_ime_action().request_id() ==
               transport.requests[2].handle_ime_action().request_id(),
           "an idempotent retry must preserve its request ID");
    expect(session.hasRecoveryCheckpoint(),
           "the confirmed checkpoint must be retained");
    expect(session.pendingActionCount() == 0,
           "a confirmed retry must acknowledge its journal entry");
    expect(session.hasConfirmedSnapshot(),
           "the journal must retain the last confirmed snapshot");
}

void failedBestEffortActionIsNotReplayedBeforeTheNextAction() {
    FakeTransport normalTransport;
    normalTransport.responses = {
        openSuccess("session-v2"),
        v2Response(1, "checkpoint-1", hazkey::SUCCESS, "a"),
    };
    FakeTransport bestEffortTransport;
    bestEffortTransport.responses = {
        std::nullopt,
    };
    HazkeySessionClient client(
        [&normalTransport](const auto& request, bool tryConnect) {
            return normalTransport.transact(request, tryConnect);
        },
        [&bestEffortTransport](const auto& request, bool tryConnect) {
            return bestEffortTransport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "best-effort session must open");

    hazkey::commands::HandleImeAction delayed;
    delayed.mutable_apply_live_conversion()->set_scheduled_revision(0);
    const auto delayedResult =
        client.transactV2BestEffort(session, std::move(delayed));

    expect(!delayedResult.has_value(),
           "a lost best-effort response must remain unconfirmed");
    expect(session.pendingActionCount() == 0,
           "a failed best-effort action must never enter the recovery journal");
    expect(bestEffortTransport.requests.size() == 1,
           "best-effort work must not use the normal idempotent retry");
    expect(!bestEffortTransport.tryConnectValues.front(),
           "best-effort work must not reconnect");

    const auto normalResult = client.transactV2(session, inputAction());
    expect(normalResult.has_value() &&
               normalResult->status() == hazkey::SUCCESS,
           "the next normal action must still succeed");
    expect(normalTransport.requests.size() == 2,
           "normal transport must contain only open and the next action");
    expect(normalTransport.requests[1].handle_ime_action().has_insert_text(),
           "the first normal request after best-effort failure must be the edit");
}

void failedDurableBestEffortActionIsJournaledWithoutBlockingRecovery() {
    FakeTransport normalTransport;
    normalTransport.responses = {
        openSuccess("session-v2"),
        v2Response(1, "checkpoint-1"),
        v2Response(2, "checkpoint-2", hazkey::SUCCESS, "a"),
    };
    FakeTransport bestEffortTransport;
    HazkeySessionClient client(
        [&normalTransport](const auto& request, bool tryConnect) {
            return normalTransport.transact(request, tryConnect);
        },
        [&bestEffortTransport](const auto& request, bool tryConnect) {
            // If the durable key path ever touches this callback again, the
            // latency assertion below catches the regression as well as the
            // request-count assertion.
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            return bestEffortTransport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "durable best-effort session must open");

    hazkey::commands::HandleImeAction resolution;
    resolution.mutable_resolve_pending_learning()->set_commit(false);
    const auto started = std::chrono::steady_clock::now();
    const auto lost =
        client.transactV2DurableBestEffort(session, std::move(resolution));
    const auto elapsed = std::chrono::steady_clock::now() - started;

    expect(!lost.has_value(),
           "a lost bounded resolution must return without a normal retry");
    expect(session.pendingActionCount() == 1,
           "a lost bounded resolution must remain journaled");
    expect(bestEffortTransport.requests.empty(),
           "the application key path must not touch the shared stream");
    expect(elapsed < std::chrono::milliseconds(25),
           "journaling a learning decision must be nearly nonblocking");
    expect(normalTransport.requests.size() == 1,
           "the bounded path must not use the normal transport");

    const auto replayed = client.transactV2(session, inputAction());
    expect(replayed.has_value() && replayed->status() == hazkey::SUCCESS,
           "the next normal edit must replay the resolution first");
    expect(normalTransport.requests.size() == 3,
           "normal recovery must contain one replay followed by the edit");
    expect(normalTransport.requests[1]
               .handle_ime_action()
               .has_resolve_pending_learning(),
           "the durable resolution must replay before the edit");
    expect(normalTransport.requests[2].handle_ime_action().has_insert_text(),
           "the edit must remain ordered after the resolution");
    expect(session.pendingActionCount() == 0,
           "successful replay must drain the durable journal");
}

void staleResolutionGetsAFreshImmutableRetryBinding() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        v2Response(5, "checkpoint-5", hazkey::STALE_REVISION),
        std::nullopt,
        std::nullopt,
        v2Response(6, "checkpoint-6"),
        v2Response(7, "checkpoint-7", hazkey::SUCCESS, "a"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "stale-resolution session must open");

    hazkey::commands::HandleImeAction resolution;
    resolution.mutable_resolve_pending_learning()->set_commit(false);
    (void)client.transactV2DurableBestEffort(session, std::move(resolution));
    expect(!client.transactV2(session, inputAction()).has_value(),
           "a lost fresh retry must remain pending");
    expect(session.pendingActionCount() == 1,
           "only the fresh retry binding must remain journaled");

    expect(transport.requests.size() == 4,
           "open, stale request, and two identical fresh sends are expected");
    const auto& stale = transport.requests[1].handle_ime_action();
    const auto& freshFirst = transport.requests[2].handle_ime_action();
    const auto& freshRetry = transport.requests[3].handle_ime_action();
    expect(stale.request_id() != freshFirst.request_id(),
           "a cached stale ID must never be reused for a new revision");
    expect(freshFirst.expected_revision() == 5,
           "the fresh ID must bind to the stale snapshot revision");
    expect(freshFirst.SerializeAsString() == freshRetry.SerializeAsString(),
           "response-loss retry must preserve the complete wire envelope");

    const auto recovered = client.transactV2(session, inputAction());
    expect(recovered.has_value() && recovered->status() == hazkey::SUCCESS,
           "the exact fresh binding must recover before the next edit");
    expect(transport.requests[4].handle_ime_action().SerializeAsString() ==
               freshFirst.SerializeAsString(),
           "journal replay must reproduce the fresh wire envelope byte-for-byte");
    expect(transport.requests[5].handle_ime_action().expected_revision() == 6,
           "the next edit must follow the confirmed fresh retry");
}

void sessionRecoveryRebindsTheExactNewNamespaceEnvelope() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-old"),
        v2Response(3, "checkpoint-3", hazkey::SUCCESS, "a"),
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-new"),
        v2Response(4, "checkpoint-4", hazkey::SUCCESS, "a"),
        std::nullopt,
        std::nullopt,
        v2Response(5, "checkpoint-5", hazkey::SUCCESS, "ab"),
        v2Response(6, "checkpoint-6", hazkey::SUCCESS, "abc"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "namespace-rebind session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "the first action must establish a checkpoint");

    auto second = inputAction();
    second.mutable_insert_text()->set_text("b");
    expect(!client.transactV2(session, second).has_value(),
           "a lost new-session response must remain journaled");
    expect(session.pendingActionCount() == 1,
           "the rebound new-session action must remain pending");
    expect(transport.requests.size() == 7,
           "recovery must send old action, open, restore, and two new sends");
    const auto rebound = transport.requests[5];
    expect(rebound.session_id() == "session-new" &&
               rebound.handle_ime_action().expected_revision() == 4,
           "the action must bind to the restored new-session revision");
    expect(rebound.handle_ime_action().SerializeAsString() ==
               transport.requests[6].handle_ime_action().SerializeAsString(),
           "new-session response-loss retry must be byte-identical");

    auto third = inputAction();
    third.mutable_insert_text()->set_text("c");
    expect(client.transactV2(session, third).has_value(),
           "the rebound journal must replay before the third action");
    expect(transport.requests[7].session_id() == "session-new" &&
               transport.requests[7].handle_ime_action().SerializeAsString() ==
                   rebound.handle_ime_action().SerializeAsString(),
           "journal replay must preserve the complete new-namespace envelope");
}

void learningResolutionCoalescesAndFailsClosed() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        v2Response(1, "checkpoint-1", hazkey::INVALID_ACTION),
        v2Response(2, "checkpoint-2"),
        v2Response(3, "checkpoint-3", hazkey::SUCCESS, "a"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "coalescing session must open");

    hazkey::commands::HandleImeAction discard;
    discard.mutable_resolve_pending_learning()->set_commit(false);
    (void)client.transactV2DurableBestEffort(session, std::move(discard));
    hazkey::commands::HandleImeAction laterCommit;
    laterCommit.mutable_resolve_pending_learning()->set_commit(true);
    (void)client.transactV2DurableBestEffort(session, std::move(laterCommit));
    expect(session.pendingActionCount() == 1,
           "the first learning disposition must remain authoritative");

    expect(!client.transactV2(session, inputAction()).has_value(),
           "a rejected learning resolution must block later input");
    expect(session.pendingActionCount() == 1,
           "non-success must not acknowledge a learning resolution");
    expect(!transport.requests[1]
                .handle_ime_action()
                .resolve_pending_learning()
                .commit(),
           "coalescing must preserve the original discard decision");
    const auto rejectedID =
        transport.requests[1].handle_ime_action().request_id();

    expect(client.transactV2(session, inputAction()).has_value(),
           "a later confirmed replay may unblock the edit");
    expect(transport.requests[2].handle_ime_action().request_id() != rejectedID,
           "a terminal cached rejection must retry with a fresh request ID");
    expect(session.pendingActionCount() == 0,
           "successful resolution and edit must drain the journal");
}

void terminalGenericFailureDoesNotPinTheJournal() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        v2Response(1, "checkpoint-1", hazkey::INVALID_ACTION),
        v2Response(2, "checkpoint-2", hazkey::SUCCESS, "a"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "terminal-failure session must open");
    const auto rejected = client.transactV2(session, inputAction());
    expect(rejected.has_value() && rejected->status() == hazkey::INVALID_ACTION,
           "the semantic rejection must reach the caller");
    expect(session.pendingActionCount() == 0,
           "a terminal generic rejection must be acknowledged");
    expect(client.transactV2(session, inputAction()).has_value(),
           "the next input must not be pinned behind the rejection");
}

void separateSessionsDoNotOvertakeADeferredResolution() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-a"),
        openSuccess("session-b"),
        v2Response(1, "b-1", hazkey::SUCCESS, "b"),
        v2Response(1, "a-1"),
        v2Response(2, "a-2", hazkey::SUCCESS, "a"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession sessionA(context());
    HazkeyClientSession sessionB(context());
    expect(client.open(sessionA) && client.open(sessionB),
           "both independent sessions must open");
    hazkey::commands::HandleImeAction resolution;
    resolution.mutable_resolve_pending_learning()->set_commit(false);
    (void)client.transactV2DurableBestEffort(sessionA, std::move(resolution));

    expect(client.transactV2(sessionB, inputAction()).has_value(),
           "session B must remain independent of A's local journal");
    expect(transport.requests[2].session_id() == "session-b" &&
               transport.requests[2].handle_ime_action().has_insert_text(),
           "B must send only its own semantic input");
    expect(client.transactV2(sessionA, inputAction()).has_value(),
           "A must replay its own disposition before its edit");
    expect(transport.requests[3].session_id() == "session-a" &&
               transport.requests[3]
                   .handle_ime_action()
                   .has_resolve_pending_learning(),
           "A's deferred resolution must stay in A's namespace");
}

void lifecycleFlushReturnsConfirmedCommitEffectsBeforeClose() {
    auto resolutionResponse = v2Response(1, "checkpoint-1");
    auto* snapshot = resolutionResponse.mutable_handle_ime_action_result()
                         ->mutable_snapshot();
    snapshot->set_phase(hazkey::IDLE);
    auto* effect = snapshot->add_effects();
    effect->set_effect_id(91);
    effect->set_type(hazkey::ClientEffect::COMMIT_TEXT);
    effect->set_text("確定");

    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        resolutionResponse,
        status(hazkey::SUCCESS),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "flush-effect session must open");
    hazkey::commands::HandleImeAction resolution;
    resolution.mutable_resolve_pending_learning()->set_commit(true);
    (void)client.transactV2DurableBestEffort(session, std::move(resolution));

    expect(!client.close(session),
           "close must refuse while a disposition/effect is unresolved");
    expect(transport.requests.size() == 1,
           "a refused close must not touch the transport");
    auto flushed = client.flushPendingV2(session);
    expect(flushed.completed && flushed.response.has_value(),
           "lifecycle flush must return an applyable synthetic response");
    const auto& effects = flushed.response->handle_ime_action_result()
                              .snapshot()
                              .effects();
    expect(effects.size() == 1 && effects.Get(0).effect_id() != 0 &&
               effects.Get(0).text() == "確定",
           "confirmed replay effects must be handed to state before close");
    expect(session.shouldApplyEffect(effects.Get(0).effect_id()) &&
               !session.shouldApplyEffect(effects.Get(0).effect_id()),
           "normalized replay effect must remain exactly-once");
    expect(client.close(session), "close may proceed after effect handoff");
}

void abandonHandsOffDiscardThenCloseAndDropsOldJournal() {
    FakeTransport normalTransport;
    normalTransport.responses = {
        openSuccess("session-old"),
        std::nullopt,
        std::nullopt,
        openSuccess("session-new"),
    };
    FakeTransport lifecycleTransport;
    HazkeySessionClient client(
        [&normalTransport](const auto& request, bool tryConnect) {
            return normalTransport.transact(request, tryConnect);
        },
        {},
        [&lifecycleTransport](const auto& request, bool tryConnect) {
            return lifecycleTransport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "abandon-handoff session must open");
    expect(!client.transactV2(session, inputAction()).has_value(),
           "uncertain old-domain action must remain journaled");

    client.abandonUnconfirmedInput(session);
    expect(session.id().empty() && session.pendingActionCount() == 0,
           "abandon must detach and clear all old-domain recovery state");
    expect(lifecycleTransport.requests.size() == 2,
           "abandon must hand off discard followed by close");
    expect(lifecycleTransport.requests[0]
               .handle_ime_action()
               .has_resolve_pending_learning() &&
               !lifecycleTransport.requests[0]
                    .handle_ime_action()
                    .resolve_pending_learning()
                    .commit(),
           "the first lifecycle frame must be a fresh discard disposition");
    expect(lifecycleTransport.requests[1].has_close_session(),
           "explicit close must be ordered after discard");

    auto next = context();
    next.program = "other-program";
    expect(client.updateContext(session, next),
           "new domain must open without replaying the old journal");
    expect(normalTransport.requests.back().has_open_session() &&
               normalTransport.requests.back()
                       .open_session()
                       .client()
                       .program() == "other-program",
           "the replacement open must carry only the new context");
}

void replayedCommitEffectsAreDeliveredWithTheFollowingResponse() {
    auto replayedCommit = v2Response(1, "checkpoint-1");
    auto* commitSnapshot = replayedCommit.mutable_handle_ime_action_result()
                               ->mutable_snapshot();
    commitSnapshot->set_phase(hazkey::IDLE);
    commitSnapshot->set_pending_learning(true);
    auto* commitEffect = commitSnapshot->add_effects();
    commitEffect->set_effect_id(42);
    commitEffect->set_type(hazkey::ClientEffect::COMMIT_TEXT);
    commitEffect->set_text("確定");

    auto replayedResolution = v2Response(2, "checkpoint-2");
    replayedResolution.mutable_handle_ime_action_result()
        ->mutable_snapshot()
        ->set_phase(hazkey::IDLE);
    auto finalCancel = v2Response(3, "checkpoint-3");
    finalCancel.mutable_handle_ime_action_result()
        ->mutable_snapshot()
        ->set_phase(hazkey::IDLE);

    FakeTransport normalTransport;
    normalTransport.responses = {
        openSuccess("session-v2"),
        std::nullopt,
        std::nullopt,
        replayedCommit,
        replayedResolution,
        finalCancel,
    };
    FakeTransport bestEffortTransport;
    HazkeySessionClient client(
        [&normalTransport](const auto& request, bool tryConnect) {
            return normalTransport.transact(request, tryConnect);
        },
        [&bestEffortTransport](const auto& request, bool tryConnect) {
            return bestEffortTransport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "lost-commit session must open");

    hazkey::commands::HandleImeAction commit;
    commit.mutable_commit_all();
    expect(!client.transactV2(session, std::move(commit)).has_value(),
           "a doubly lost commit response must remain journaled");

    hazkey::commands::HandleImeAction resolution;
    resolution.mutable_resolve_pending_learning()->set_commit(true);
    expect(!client
                .transactV2DurableBestEffort(session, std::move(resolution))
                .has_value(),
           "resolution must queue behind the unconfirmed commit");
    expect(bestEffortTransport.requests.empty(),
           "a queued resolution must not overtake the commit");
    expect(session.pendingActionCount() == 2,
           "commit and its learning decision must both remain ordered");

    hazkey::commands::HandleImeAction cancel;
    cancel.mutable_cancel();
    const auto recovered = client.transactV2(session, std::move(cancel));
    expect(recovered.has_value(),
           "a later normal action must complete ordered recovery");
    const auto& effects = recovered->handle_ime_action_result()
                              .snapshot()
                              .effects();
    expect(effects.size() == 1 && effects.Get(0).effect_id() != 0 &&
               effects.Get(0).type() == hazkey::ClientEffect::COMMIT_TEXT &&
               effects.Get(0).text() == "確定",
           "the replayed commit effect must be prepended to the final response");
    expect(session.pendingActionCount() == 0,
           "commit, resolution, and final action must all be acknowledged");
}

void sessionRecoveryDoesNotDropEarlierConfirmedReplayEffects() {
    auto commitResponse = v2Response(1, "checkpoint-1");
    auto* commitSnapshot = commitResponse.mutable_handle_ime_action_result()
                               ->mutable_snapshot();
    commitSnapshot->set_phase(hazkey::IDLE);
    auto* effect = commitSnapshot->add_effects();
    effect->set_effect_id(73);
    effect->set_type(hazkey::ClientEffect::COMMIT_TEXT);
    effect->set_text("復旧");

    FakeTransport transport;
    transport.responses = {
        openSuccess("session-old"),
        std::nullopt,
        std::nullopt,
        commitResponse,
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-new"),
        v2Response(2, "checkpoint-2"),
        v2Response(3, "checkpoint-3"),
        v2Response(4, "checkpoint-4"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "effect-recovery session must open");

    hazkey::commands::HandleImeAction commit;
    commit.mutable_commit_all();
    expect(!client.transactV2(session, commit).has_value(),
           "lost commit must stay journaled");
    hazkey::commands::HandleImeAction resolution;
    resolution.mutable_resolve_pending_learning()->set_commit(true);
    (void)client.transactV2DurableBestEffort(session, std::move(resolution));

    hazkey::commands::HandleImeAction cancel;
    cancel.mutable_cancel();
    const auto recovered = client.transactV2(session, std::move(cancel));
    expect(recovered.has_value(),
           "replay must recover across the replacement server session");
    const auto& effects = recovered->handle_ime_action_result()
                              .snapshot()
                              .effects();
    expect(effects.size() == 1 && effects.Get(0).effect_id() != 0 &&
               effects.Get(0).text() == "復旧",
           "new-session ledger reset must preserve earlier confirmed effects");
}

void failedLearningResolutionIsReplayedBeforeTheNextAction() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        std::nullopt,
        std::nullopt,
        v2Response(1, "checkpoint-1"),
        v2Response(2, "checkpoint-2", hazkey::SUCCESS, "a"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "learning-resolution session must open");

    hazkey::commands::HandleImeAction resolution;
    resolution.mutable_resolve_pending_learning()->set_commit(false);
    const auto lost = client.transactV2(session, std::move(resolution));

    expect(!lost.has_value(),
           "two lost resolution responses must remain unconfirmed");
    expect(session.pendingActionCount() == 1,
           "durable learning resolution must remain journaled");

    const auto next = client.transactV2(session, inputAction());
    expect(next.has_value() && next->status() == hazkey::SUCCESS,
           "the next edit must follow replayed learning resolution");
    expect(transport.requests.size() == 5,
           "open, two lost resolutions, replay, and edit are expected");
    expect(transport.requests[1].handle_ime_action().has_resolve_pending_learning() &&
               transport.requests[2].handle_ime_action().has_resolve_pending_learning() &&
               transport.requests[3].handle_ime_action().has_resolve_pending_learning(),
           "the durable action must replay as learning resolution");
    expect(transport.requests[1].handle_ime_action().request_id() ==
                   transport.requests[2].handle_ime_action().request_id() &&
               transport.requests[2].handle_ime_action().request_id() ==
                   transport.requests[3].handle_ime_action().request_id(),
           "learning-resolution replay must retain its request ID");
    expect(transport.requests[4].handle_ime_action().has_insert_text(),
           "the edit must be sent only after learning resolution is confirmed");
    expect(session.pendingActionCount() == 0,
           "confirmed resolution and edit must empty the journal");
}

void abandoningAFallthroughKeyClearsItsRecoveryJournal() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        std::nullopt,
        std::nullopt,
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "v2 session must open");

    const auto result = client.transactV2(session, inputAction());
    expect(!result.has_value(), "both lost responses must remain unconfirmed");
    expect(session.pendingActionCount() == 1,
           "the uncertain action must remain journaled before fallback");

    client.abandonUnconfirmedInput(session);
    expect(session.id().empty(), "fallback must abandon the uncertain session");
    expect(session.pendingActionCount() == 0,
           "a key passed to the application must never be replayed by the IME");
    expect(!session.hasRecoveryCheckpoint(),
           "fallback must discard any prior checkpoint namespace");
    expect(!session.hasFallbackComposition(),
           "fallback must discard reconstructed preedit text");
}

void replaysAnUnacknowledgedJournalEntryBeforeTheNextAction() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2"),
        std::nullopt,
        std::nullopt,
        v2Response(1, "checkpoint-1", hazkey::SUCCESS, "a"),
        v2Response(2, "checkpoint-2", hazkey::SUCCESS, "ab"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "journal session must open");

    auto first = inputAction();
    const auto lost = client.transactV2(session, first);
    expect(!lost.has_value(), "two lost responses must leave the action pending");
    expect(session.pendingActionCount() == 1,
           "the lost action must remain in the in-memory journal");

    hazkey::commands::HandleImeAction second;
    second.mutable_insert_text()->set_text("b");
    const auto result = client.transactV2(session, second);
    expect(result.has_value() && result->status() == hazkey::SUCCESS,
           "the next action must run after journal replay");
    expect(transport.requests.size() == 5,
           "open, two lost sends, one journal replay, and one new action are expected");
    expect(transport.requests[1].handle_ime_action().request_id() ==
               transport.requests[2].handle_ime_action().request_id() &&
               transport.requests[2].handle_ime_action().request_id() ==
                   transport.requests[3].handle_ime_action().request_id(),
           "journal replay must retain the original request ID");
    expect(transport.requests[4].handle_ime_action().expected_revision() == 1,
           "the new action must follow the replayed snapshot revision");
    expect(session.pendingActionCount() == 0,
           "both acknowledged actions must leave the journal empty");
}

void restoresCheckpointBeforeReplayingAfterServerRestart() {
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2-a"),
        v2Response(3, "checkpoint-3"),
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-v2-b"),
        v2Response(4, "checkpoint-4"),
        v2Response(5, "checkpoint-5"),
    };
    int destructiveRecoveryCount = 0;
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(), [&destructiveRecoveryCount] {
        ++destructiveRecoveryCount;
    });
    expect(client.open(session), "initial v2 session must open");

    hazkey::commands::HandleImeAction first;
    first.mutable_insert_text()->set_text("a");
    expect(client.transactV2(session, first).has_value(),
           "the first action must establish a checkpoint");
    expect(session.shouldApplyEffect(1), "the old namespace must accept effect one");

    hazkey::commands::HandleImeAction second;
    second.mutable_insert_text()->set_text("b");
    const auto result = client.transactV2(session, second);

    expect(result.has_value() && result->status() == hazkey::SUCCESS,
           "the action must resume after checkpoint restoration");
    expect(transport.requests.size() == 6,
           "restart recovery is open, restore, then one replay");
    expect(transport.requests[4].handle_ime_action().has_restore_checkpoint(),
           "the new session must restore before replaying the action");
    expect(transport.requests[4]
               .handle_ime_action()
               .restore_checkpoint()
               .opaque_state() == "checkpoint-3",
           "the last confirmed checkpoint must be restored");
    expect(transport.requests[5].handle_ime_action().expected_revision() == 4,
           "the replay must continue from the restored revision");
    expect(destructiveRecoveryCount == 0,
           "successful restoration must keep the visible composition");
    expect(session.revision() == 5,
           "the replayed snapshot revision must become current");
    expect(!session.shouldApplyEffect(1),
           "checkpoint restoration must retain the Effect-ID namespace");
}

void fallsBackToTheLastVisiblePreeditWhenCheckpointIsUnavailable() {
    auto oldResponse = v2Response(1, "", hazkey::SUCCESS, "かな");
    addCommitEffect(oldResponse, 1, "old");
    auto fallbackResponse = v2Response(1, "", hazkey::SUCCESS, "かな");
    addCommitEffect(fallbackResponse, 1, "fresh");
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2-a"),
        oldResponse,
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-v2-b"),
        fallbackResponse,
        v2Response(2, "", hazkey::SUCCESS, "かなb"),
    };
    int destructiveRecoveryCount = 0;
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(), [&destructiveRecoveryCount] {
        ++destructiveRecoveryCount;
    });
    expect(client.open(session), "initial fallback session must open");

    hazkey::commands::HandleImeAction first;
    first.mutable_insert_text()->set_text("かな");
    const auto initial = client.transactV2(session, first);
    expect(initial.has_value(),
           "the initial snapshot must be confirmed");
    const uint64_t oldGlobalID = initial->handle_ime_action_result()
                                     .snapshot()
                                     .effects(0)
                                     .effect_id();
    expect(session.shouldApplyEffect(oldGlobalID),
           "the old fallback namespace must accept its normalized effect");
    expect(!session.hasRecoveryCheckpoint(),
           "the fixture intentionally has no opaque checkpoint");
    expect(session.hasFallbackComposition(),
           "the rendered preedit must be retained as a safe fallback");

    hazkey::commands::HandleImeAction second;
    second.mutable_insert_text()->set_text("b");
    const auto result = client.transactV2(session, second);

    expect(result.has_value() && result->status() == hazkey::SUCCESS,
           "fallback recovery must replay the interrupted action");
    expect(transport.requests.size() == 6,
           "fallback recovery is open, insert fallback, then replay");
    expect(transport.requests[4].handle_ime_action().has_insert_text(),
           "the fallback must be reconstructed as semantic text input");
    expect(transport.requests[4]
               .handle_ime_action()
               .insert_text()
               .text() == "かな",
           "the fallback must use the last visible preedit exactly");
    expect(transport.requests[5].handle_ime_action().expected_revision() == 1,
           "the interrupted action must continue after fallback revision one");
    expect(destructiveRecoveryCount == 0,
           "a successful visible-text fallback must not clear the UI");
    const auto& effects =
        result->handle_ime_action_result().snapshot().effects();
    expect(effects.size() == 1 && effects.Get(0).text() == "fresh",
           "fallback reconstruction must surface its confirmed effect");
    const uint64_t freshGlobalID = effects.Get(0).effect_id();
    expect(freshGlobalID > oldGlobalID &&
               session.shouldApplyEffect(freshGlobalID) &&
               !session.shouldApplyEffect(oldGlobalID),
           "raw effect one in a fresh namespace must get a distinct global ID");
}

void failedSnapshotEffectsDoNotCreateGlobalLedgerHoles() {
    auto rejected = v2Response(1, "", hazkey::INVALID_ACTION);
    addCommitEffect(rejected, 100, "untrusted");
    auto trusted99 = v2Response(2);
    addCommitEffect(trusted99, 99, "first");
    auto trusted100 = v2Response(3);
    addCommitEffect(trusted100, 100, "second");

    FakeTransport transport;
    transport.responses = {
        openSuccess("session-effects"), rejected, trusted99, trusted100,
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "effect-hole session must open");

    const auto failed = client.transactV2(session, inputAction());
    expect(failed.has_value() &&
               failed->handle_ime_action_result().snapshot().effects().empty(),
           "untrusted failure effects must be stripped before normalization");
    const auto first = client.transactV2(session, inputAction());
    const auto second = client.transactV2(session, inputAction());
    const uint64_t firstID =
        first->handle_ime_action_result().snapshot().effects(0).effect_id();
    const uint64_t secondID =
        second->handle_ime_action_result().snapshot().effects(0).effect_id();
    expect(firstID != 0 && secondID > firstID &&
               session.shouldApplyEffect(firstID) &&
               session.shouldApplyEffect(secondID),
           "later trusted raw 99 and 100 must both remain deliverable");
}

void confirmedReplayEffectSurvivesLostCurrentResponse() {
    auto replayedCommit = v2Response(1, "checkpoint-1");
    replayedCommit.mutable_handle_ime_action_result()
        ->mutable_snapshot()
        ->set_phase(hazkey::IDLE);
    addCommitEffect(replayedCommit, 1, "confirmed");

    FakeTransport transport;
    transport.responses = {
        openSuccess("session-replay-null"),
        std::nullopt,
        std::nullopt,
        replayedCommit,
        std::nullopt,
        std::nullopt,
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "replay-null session must open");
    hazkey::commands::HandleImeAction commit;
    commit.mutable_commit_all();
    expect(!client.transactV2(session, commit).has_value(),
           "lost commit must remain pending");

    hazkey::commands::HandleImeAction cancel;
    cancel.mutable_cancel();
    const auto handoff = client.transactV2(session, cancel);
    expect(handoff.has_value() &&
               handoff->status() == hazkey::RETRYABLE_TRANSPORT_ERROR &&
               handoff->handle_ime_action_result().snapshot().effects_size() == 1 &&
               handoff->handle_ime_action_result().snapshot().effects(0).text() ==
                   "confirmed",
           "confirmed replay effect must be handed off in a failure envelope");
    expect(session.pendingActionCount() == 1,
           "only the unconfirmed current action must remain journaled");
}

void confirmedReplayEffectSurvivesTerminalResponseWithoutSnapshot() {
    auto replayedCommit = v2Response(1, "checkpoint-1");
    replayedCommit.mutable_handle_ime_action_result()
        ->mutable_snapshot()
        ->set_phase(hazkey::IDLE);
    addCommitEffect(replayedCommit, 1, "confirmed-terminal");

    FakeTransport transport;
    transport.responses = {
        openSuccess("session-replay-terminal"),
        std::nullopt,
        std::nullopt,
        replayedCommit,
        status(hazkey::INVALID_ACTION),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "replay-terminal session must open");
    hazkey::commands::HandleImeAction commit;
    commit.mutable_commit_all();
    (void)client.transactV2(session, commit);

    const auto handoff = client.transactV2(session, inputAction());
    expect(handoff.has_value() &&
               handoff->status() == hazkey::INVALID_ACTION &&
               handoff->has_handle_ime_action_result() &&
               handoff->handle_ime_action_result().has_snapshot() &&
               handoff->handle_ime_action_result().snapshot().effects_size() == 1,
           "terminal response without snapshot must synthesize an effect handoff");
    expect(session.pendingActionCount() == 0,
           "terminal generic current action must be drained");
}

void stagedDiscardIsHandedOffImmediatelyAndOverridesFinalizeDefault() {
    auto staged = v2Response(1, "checkpoint-staged");
    auto* snapshot =
        staged.mutable_handle_ime_action_result()->mutable_snapshot();
    snapshot->set_phase(hazkey::IDLE);
    snapshot->set_pending_learning(true);

    FakeTransport normal;
    normal.responses = {openSuccess("session-staged"), staged};
    FakeTransport lifecycle;
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "staged-discard session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "fixture must confirm pending learning");

    hazkey::commands::HandleImeAction discard;
    discard.mutable_resolve_pending_learning()->set_commit(false);
    (void)client.transactV2DurableBestEffort(session, discard);
    expect(lifecycle.requests.size() == 1 &&
               lifecycle.requests[0].handle_ime_action()
                   .has_resolve_pending_learning() &&
               !lifecycle.requests[0]
                    .handle_ime_action()
                    .resolve_pending_learning()
                    .commit(),
           "confirmed pending discard must enter lifecycle transport immediately");

    client.finalizeWithoutUITarget(session, true);
    expect(lifecycle.requests.size() == 3 &&
               !lifecycle.requests[1]
                    .handle_ime_action()
                    .resolve_pending_learning()
                    .commit() &&
               lifecycle.requests[2].has_close_session(),
           "journaled discard must override a later preferred commit and precede close");
}

void genericFlushFailureDrainsAndReportsFailure() {
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-flush-failure"), std::nullopt, std::nullopt,
    };
    FakeTransport lifecycle;
    lifecycle.responses = {
        v2Response(1, "", hazkey::INVALID_ACTION),
    };
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "generic-flush session must open");
    expect(!client.transactV2(session, inputAction()).has_value(),
           "fixture action must remain pending");

    const auto flushed = client.flushPendingV2(session, false);
    expect(flushed.completed && flushed.response.has_value() &&
               flushed.response->status() == hazkey::INVALID_ACTION,
           "terminal generic failure must drain but never masquerade as success");
    expect(session.pendingActionCount() == 0,
           "terminal generic flush must empty the journal");
}

void lifecycleSessionNotFoundRecoversThroughNormalFlush() {
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"),
        v2Response(1, "checkpoint-1", hazkey::SUCCESS, "a"),
        std::nullopt,
        std::nullopt,
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-new"),
        v2Response(2, "checkpoint-2", hazkey::SUCCESS, "a"),
        v2Response(3, "checkpoint-3", hazkey::SUCCESS, "ab"),
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SESSION_NOT_FOUND)};
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "normal-recovery session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "fixture must establish a checkpoint");
    auto second = inputAction();
    second.mutable_insert_text()->set_text("b");
    expect(!client.transactV2(session, second).has_value(),
           "lost edit must remain pending");
    const auto oldWire = normal.requests[2].handle_ime_action();

    expect(!client.flushPendingV2(session, false).completed &&
               session.id() == "session-old",
           "bounded SESSION_NOT_FOUND must retain the exact old binding");
    const auto recovered = client.flushPendingV2(session, true);
    expect(recovered.completed && recovered.response.has_value() &&
               session.id() == "session-new" &&
               session.pendingActionCount() == 0,
           "normal flush must reconnect, restore, rebind, and drain");
    expect(normal.requests[4].session_id() == "session-old" &&
               normal.requests[4].handle_ime_action().request_id() ==
                   oldWire.request_id() &&
               normal.requests[6].handle_ime_action().has_restore_checkpoint() &&
               normal.requests[7].handle_ime_action().expected_revision() == 2,
           "normal recovery must resend old ID before restore and revision rebase");
}

void uncertainContextRestoreNeverFallsBackAndCanRetry() {
    auto initial = v2Response(1, "checkpoint-1", hazkey::SUCCESS, "かな");
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"),
        initial,
        openSuccess("session-replacement"),
        std::nullopt,
        std::nullopt,
        openSuccess("session-retry"),
        v2Response(2, "checkpoint-2", hazkey::SUCCESS, "かな"),
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS), std::nullopt, std::nullopt};
    int recoveryCount = 0;
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(), [&recoveryCount] { ++recoveryCount; });
    expect(client.open(session), "context-restore session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "fixture must establish recovery material");
    auto next = context();
    next.frontend = "x11";

    expect(!client.updateContext(session, next),
           "uncertain restore must pause context recovery");
    expect(session.id().empty() && session.hasRecoveryCheckpoint() &&
               session.hasFallbackComposition() && recoveryCount == 0,
           "uncertain restore must preserve UI/material but detach replacement");
    for (const auto& request : normal.requests) {
        expect(!request.has_handle_ime_action() ||
                   !request.handle_ime_action().has_insert_text() ||
                   request.handle_ime_action().insert_text().text() != "かな",
               "unknown restore must never be followed by text fallback");
    }
    expect(client.updateContext(session, next) && session.id() == "session-retry",
           "same-context retry must open and restore preserved material");
}

void explicitlyRejectedContextRecoveryClearsHiddenState() {
    auto initial = v2Response(1, "checkpoint-1", hazkey::SUCCESS, "かな");
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"),
        initial,
        openSuccess("session-replacement"),
        v2Response(0, "", hazkey::INVALID_ACTION),
        v2Response(0, "", hazkey::INVALID_ACTION),
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS), std::nullopt, std::nullopt};
    int recoveryCount = 0;
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(), [&recoveryCount] { ++recoveryCount; });
    expect(client.open(session), "rejected-context session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "fixture must establish checkpoint and fallback");
    auto next = context();
    next.frontend = "x11";

    expect(!client.updateContext(session, next),
           "explicitly rejected recovery must fail destructively");
    expect(session.id().empty() && !session.hasRecoveryCheckpoint() &&
               !session.hasFallbackComposition() &&
               session.pendingActionCount() == 0 && recoveryCount == 1,
           "terminal restore/fallback failure must clear all hidden state once");
    expect(normal.requests.size() == 5 &&
               normal.requests[3].handle_ime_action().has_restore_checkpoint() &&
               normal.requests[4].handle_ime_action().has_insert_text(),
           "terminal checkpoint rejection may try exactly one semantic fallback");
}

void incompleteSuccessRemainsJournaledAndBlocksOvertaking() {
    hazkey::ResponseEnvelope incomplete;
    incomplete.set_status(hazkey::SUCCESS);
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-incomplete"),
        incomplete,
        v2Response(1, "checkpoint-1", hazkey::SUCCESS, "a"),
        v2Response(2, "checkpoint-2", hazkey::SUCCESS, "ab"),
    };
    HazkeySessionClient client(
        [&transport](const auto& request, bool tryConnect) {
            return transport.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "incomplete-response session must open");
    expect(!client.transactV2(session, inputAction()).has_value() &&
               session.pendingActionCount() == 1,
           "outer success without a complete action result must remain pending");
    const auto firstID = transport.requests[1].handle_ime_action().request_id();

    auto second = inputAction();
    second.mutable_insert_text()->set_text("b");
    expect(client.transactV2(session, second).has_value(),
           "a later complete replay may unblock the next action");
    expect(transport.requests[2].handle_ime_action().request_id() == firstID &&
               transport.requests[3].handle_ime_action().has_insert_text(),
           "the next action must never overtake an incomplete-success head");
}

void incompleteContextRestoreNeverTriggersFallback() {
    auto initial = v2Response(1, "checkpoint-1", hazkey::SUCCESS, "かな");
    hazkey::ResponseEnvelope incomplete;
    incomplete.set_status(hazkey::SUCCESS);
    incomplete.mutable_handle_ime_action_result()->set_status(
        hazkey::RETRYABLE_TRANSPORT_ERROR);
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"), initial,
        openSuccess("session-incomplete-restore"), incomplete,
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS), std::nullopt, std::nullopt};
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "incomplete-restore session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "fixture must establish recovery material");
    auto next = context();
    next.frontend = "x11";
    expect(!client.updateContext(session, next) && session.id().empty() &&
               session.hasRecoveryCheckpoint(),
           "inner retryable restore must detach but preserve recovery material");
    expect(normal.requests.size() == 4 &&
               normal.requests.back()
                   .handle_ime_action()
                   .has_restore_checkpoint(),
           "incomplete restore must not be followed by fallback insertion");
}

void unsupportedReplacementIsDisposedBeforeStaleBindingReturns() {
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"),
        v2Response(1, "checkpoint-1", hazkey::SUCCESS, "a"),
        status(hazkey::SESSION_NOT_FOUND),
        openLegacySuccess("session-legacy"),
    };
    FakeTransport lifecycle;
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "unsupported-replacement session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "fixture must establish a checkpoint");
    expect(!client.transactV2(session, inputAction()).has_value(),
           "unsupported replacement cannot confirm the current action");
    expect(session.id() == "session-old" && session.pendingActionCount() == 1,
           "old journal binding must survive unsupported replacement");
    expect(lifecycle.requests.size() == 2 &&
               lifecycle.requests[0].session_id() == "session-legacy" &&
               lifecycle.requests[1].close_session().session_id() ==
                   "session-legacy",
           "opened legacy replacement must be discarded and closed, not orphaned");
}

void reconversionOriginNeverUsesTextOnlyFallback() {
    auto reconverted = v2Response(1, "checkpoint-reconvert", hazkey::SUCCESS,
                                  "選択");
    reconverted.mutable_handle_ime_action_result()
        ->mutable_snapshot()
        ->set_phase(hazkey::SELECTING);
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"), reconverted,
        openSuccess("session-replacement"),
        v2Response(0, "", hazkey::INVALID_ACTION),
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS), std::nullopt, std::nullopt};
    int recoveryCount = 0;
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(), [&recoveryCount] { ++recoveryCount; });
    expect(client.open(session), "reconversion session must open");
    hazkey::commands::HandleImeAction reconvert;
    reconvert.mutable_reconvert()->set_text("選択");
    expect(client.transactV2(session, reconvert).has_value() &&
               !session.isLocalTextFallbackSemanticallySafe(),
           "reconvert action must mark even SELECTING output fallback-unsafe");
    auto next = context();
    next.frontend = "x11";
    expect(!client.updateContext(session, next) && recoveryCount == 1 &&
               !session.hasFallbackComposition(),
           "reconversion restore rejection must clear rather than text-insert");
    expect(normal.requests.size() == 4 &&
               normal.requests.back()
                   .handle_ime_action()
                   .has_restore_checkpoint(),
           "reconversion recovery must never send text-only fallback");
}

void unicodeInputWithoutCheckpointNeverUsesTextFallback() {
    auto unicode = v2Response(1, "", hazkey::SUCCESS, "u3042");
    unicode.mutable_handle_ime_action_result()
        ->mutable_snapshot()
        ->set_phase(hazkey::UNICODE_INPUT);
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"), unicode,
        openSuccess("session-replacement"),
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS), std::nullopt, std::nullopt};
    int recoveryCount = 0;
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context(), [&recoveryCount] { ++recoveryCount; });
    expect(client.open(session), "unicode fallback session must open");
    hazkey::commands::HandleImeAction beginUnicode;
    beginUnicode.mutable_begin_unicode_input();
    expect(client.transactV2(session, beginUnicode).has_value() &&
               !session.isLocalTextFallbackSemanticallySafe(),
           "Unicode entry display must never be treated as ordinary text");
    auto next = context();
    next.frontend = "x11";
    expect(!client.updateContext(session, next) && recoveryCount == 1 &&
               normal.requests.size() == 3,
           "checkpointless Unicode state must clear without inserting its marker");
}

void terminalThenSuccessfulFlushUsesNewestSnapshot() {
    auto rejected = v2Response(10, "", hazkey::INVALID_ACTION, "old");
    auto later = v2Response(2, "checkpoint-2", hazkey::SUCCESS, "new");
    addCommitEffect(later, 1, "later-effect");
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-flush-order"), std::nullopt, std::nullopt,
    };
    FakeTransport lifecycle;
    lifecycle.responses = {rejected, later};
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "flush-order session must open");
    expect(!client.transactV2(session, inputAction()).has_value(),
           "first action must remain pending");
    hazkey::commands::HandleImeAction disposition;
    disposition.mutable_resolve_pending_learning()->set_commit(false);
    (void)client.transactV2DurableBestEffort(session, disposition);

    const auto flushed = client.flushPendingV2(session, false);
    expect(flushed.completed && flushed.response.has_value() &&
               flushed.response->status() == hazkey::INVALID_ACTION &&
               flushed.response->handle_ime_action_result()
                       .snapshot()
                       .revision() == 2 &&
               flushed.response->handle_ime_action_result()
                       .snapshot()
                       .effects(0)
                       .text() == "later-effect",
           "failure status must carry the newest later-success snapshot/effect");
}

void uncertainFallbackRestoresAffineEffectNamespace() {
    auto initial = v2Response(1, "checkpoint-1", hazkey::SUCCESS, "かな");
    addCommitEffect(initial, 42, "old-effect");
    hazkey::ResponseEnvelope incomplete;
    incomplete.set_status(hazkey::SUCCESS);
    auto restored = v2Response(2, "checkpoint-2", hazkey::SUCCESS, "かな");
    addCommitEffect(restored, 42, "duplicate-effect");
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"), initial,
        openSuccess("session-fallback-unknown"),
        v2Response(0, "", hazkey::INVALID_ACTION), incomplete,
        openSuccess("session-retry"), restored,
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS), std::nullopt, std::nullopt};
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "affine-rollback session must open");
    const auto first = client.transactV2(session, inputAction());
    const uint64_t globalID =
        first->handle_ime_action_result().snapshot().effects(0).effect_id();
    expect(session.shouldApplyEffect(globalID),
           "old namespace effect must be claimed once");
    auto next = context();
    next.frontend = "x11";
    expect(!client.updateContext(session, next),
           "uncertain fallback must pause recovery");
    expect(client.updateContext(session, next),
           "later restore must recover preserved checkpoint");
    const auto handoff = client.flushPendingV2(session, false);
    const uint64_t restoredID = handoff.response->handle_ime_action_result()
                                    .snapshot()
                                    .effects(0)
                                    .effect_id();
    expect(restoredID == globalID && !session.shouldApplyEffect(restoredID),
           "uncertain fresh-namespace attempt must roll affine anchor back exactly");
}

void fallbackEligibilitySeparatesStoredAndLocalSemantics() {
    auto secureComposing = v2Response(1, "", hazkey::SUCCESS, "secret");
    auto secureUnicode = v2Response(2, "", hazkey::SUCCESS, "u3042");
    secureUnicode.mutable_handle_ime_action_result()
        ->mutable_snapshot()
        ->set_phase(hazkey::UNICODE_INPUT);
    FakeTransport secureTransport;
    secureTransport.responses = {
        openSuccess("secure-session"), secureComposing, secureUnicode,
    };
    HazkeySessionClient secureClient(
        [&secureTransport](const auto& request, bool tryConnect) {
            return secureTransport.transact(request, tryConnect);
        });
    HazkeyClientSession secureSession(context(true));
    expect(secureClient.open(secureSession), "secure eligibility session must open");
    expect(secureClient.transactV2(secureSession, inputAction()).has_value() &&
               secureSession.isLocalTextFallbackSemanticallySafe() &&
               !secureSession.canUseStoredTextFallback(),
           "secure ordinary preedit may commit locally but must never be stored");
    hazkey::commands::HandleImeAction beginUnicode;
    beginUnicode.mutable_begin_unicode_input();
    expect(secureClient.transactV2(secureSession, beginUnicode).has_value() &&
               !secureSession.isLocalTextFallbackSemanticallySafe(),
           "secure Unicode marker text must not be committed locally");

    auto selecting = v2Response(1, "", hazkey::SUCCESS, "候補");
    auto* selectingSnapshot =
        selecting.mutable_handle_ime_action_result()->mutable_snapshot();
    selectingSnapshot->set_phase(hazkey::SELECTING);
    selectingSnapshot->add_preedit()->set_style(hazkey::PreeditSpan::ACTIVE);
    auto direct = v2Response(2, "", hazkey::SUCCESS, "あ");
    auto idle = v2Response(3);
    idle.mutable_handle_ime_action_result()->mutable_snapshot()->set_phase(
        hazkey::IDLE);
    auto ordinary = v2Response(4, "", hazkey::SUCCESS, "かな");
    auto transformed = v2Response(5, "", hazkey::SUCCESS, "カナ");
    FakeTransport normalTransport;
    normalTransport.responses = {
        openSuccess("normal-eligibility"), selecting, direct, idle, ordinary,
        transformed,
    };
    HazkeySessionClient normalClient(
        [&normalTransport](const auto& request, bool tryConnect) {
            return normalTransport.transact(request, tryConnect);
        });
    HazkeyClientSession normalSession(context());
    expect(normalClient.open(normalSession), "normal eligibility session must open");
    expect(normalClient.transactV2(normalSession, inputAction()).has_value() &&
               normalSession.isLocalTextFallbackSemanticallySafe() &&
               !normalSession.canUseStoredTextFallback(),
           "candidate/active presentation may commit locally but is not reconstructable");
    hazkey::commands::HandleImeAction commitUnicode;
    commitUnicode.mutable_commit_unicode_input();
    expect(normalClient.transactV2(normalSession, commitUnicode).has_value() &&
               normalSession.isLocalTextFallbackSemanticallySafe() &&
               !normalSession.canUseStoredTextFallback(),
           "direct Unicode surface must remain stored-fallback unsafe in COMPOSING");
    hazkey::commands::HandleImeAction cancel;
    cancel.mutable_cancel();
    expect(normalClient.transactV2(normalSession, cancel).has_value(),
           "IDLE transition must clear persistent direct fallback guard");
    expect(normalClient.transactV2(normalSession, inputAction()).has_value() &&
               normalSession.canUseStoredTextFallback(),
           "ordinary composition after IDLE must regain stored fallback eligibility");
    hazkey::commands::HandleImeAction transform;
    transform.mutable_transform_active_segment()->set_transform(
        hazkey::commands::TransformActiveSegment::KATAKANA_FULLWIDTH);
    expect(normalClient.transactV2(normalSession, transform).has_value() &&
               !normalSession.canUseStoredTextFallback(),
           "transformed direct surface must remain stored-fallback unsafe");
}

void nestedSessionNotFoundCannotDuplicateContextFallback() {
    auto initial = v2Response(1, "", hazkey::SUCCESS, "かな");
    FakeTransport normal;
    normal.responses = {
        openSuccess("session-old"), initial,
        openSuccess("session-replacement"),
        status(hazkey::SESSION_NOT_FOUND),
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS), std::nullopt, std::nullopt};
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session), "nested-SNF session must open");
    expect(client.transactV2(session, inputAction()).has_value(),
           "fixture must establish safe stored fallback");
    auto next = context();
    next.frontend = "x11";

    expect(!client.updateContext(session, next) && session.id().empty() &&
               session.hasFallbackComposition(),
           "fallback SESSION_NOT_FOUND must detach and retain material");
    expect(normal.requests.size() == 4 &&
               normal.requests[3].handle_ime_action().has_insert_text(),
           "nested SESSION_NOT_FOUND must stop before another open/fallback");
    expect(lifecycle.requests.size() == 3 &&
               lifecycle.requests[1].session_id() == "session-replacement" &&
               lifecycle.requests[2].close_session().session_id() ==
                   "session-replacement",
           "uncertain replacement namespace must be discarded and closed");
}

void sameContextLegacySessionRenegotiatesV2() {
    FakeTransport normal;
    normal.responses = {
        openLegacySuccess("session-legacy"), openSuccess("session-v2"),
    };
    FakeTransport lifecycle;
    lifecycle.responses = {status(hazkey::SUCCESS)};
    HazkeySessionClient client(
        [&normal](const auto& request, bool tryConnect) {
            return normal.transact(request, tryConnect);
        },
        {},
        [&lifecycle](const auto& request, bool tryConnect) {
            return lifecycle.transact(request, tryConnect);
        });
    HazkeyClientSession session(context());
    expect(client.open(session) && !session.capabilities().supportsV2(),
           "fixture must begin on a legacy negotiated session");
    expect(client.updateContext(session, context()) &&
               session.id() == "session-v2" &&
               session.capabilities().supportsV2(),
           "same-context legacy session must close and renegotiate v2");
    expect(lifecycle.requests.size() == 1 &&
               lifecycle.requests[0].has_close_session() &&
               normal.requests.size() == 2 && normal.requests[1].has_open_session(),
           "renegotiation must explicitly close legacy before reopening");
}

}  // namespace

int main() {
    reopensAndRetriesOnlyOnce();
    stopsAfterTheSingleRetry();
    preservesContextWhenReopening();
    neverMixesTwoSessionIds();
    secureContextTransitionClearsBeforeReopening();
    notifiesTheOwnerWhenAStatefulRequestReopensTheSession();
    replacesSessionWhenClientContextChanges();
    tracksV2CapabilitiesRevisionAndEffectDeduplication();
    retriesLostV2ResponsesWithTheSameRequestID();
    failedBestEffortActionIsNotReplayedBeforeTheNextAction();
    failedDurableBestEffortActionIsJournaledWithoutBlockingRecovery();
    staleResolutionGetsAFreshImmutableRetryBinding();
    sessionRecoveryRebindsTheExactNewNamespaceEnvelope();
    learningResolutionCoalescesAndFailsClosed();
    terminalGenericFailureDoesNotPinTheJournal();
    separateSessionsDoNotOvertakeADeferredResolution();
    lifecycleFlushReturnsConfirmedCommitEffectsBeforeClose();
    abandonHandsOffDiscardThenCloseAndDropsOldJournal();
    replayedCommitEffectsAreDeliveredWithTheFollowingResponse();
    sessionRecoveryDoesNotDropEarlierConfirmedReplayEffects();
    failedLearningResolutionIsReplayedBeforeTheNextAction();
    abandoningAFallthroughKeyClearsItsRecoveryJournal();
    replaysAnUnacknowledgedJournalEntryBeforeTheNextAction();
    restoresCheckpointBeforeReplayingAfterServerRestart();
    fallsBackToTheLastVisiblePreeditWhenCheckpointIsUnavailable();
    failedSnapshotEffectsDoNotCreateGlobalLedgerHoles();
    confirmedReplayEffectSurvivesLostCurrentResponse();
    confirmedReplayEffectSurvivesTerminalResponseWithoutSnapshot();
    stagedDiscardIsHandedOffImmediatelyAndOverridesFinalizeDefault();
    genericFlushFailureDrainsAndReportsFailure();
    lifecycleSessionNotFoundRecoversThroughNormalFlush();
    uncertainContextRestoreNeverFallsBackAndCanRetry();
    explicitlyRejectedContextRecoveryClearsHiddenState();
    incompleteSuccessRemainsJournaledAndBlocksOvertaking();
    incompleteContextRestoreNeverTriggersFallback();
    unsupportedReplacementIsDisposedBeforeStaleBindingReturns();
    reconversionOriginNeverUsesTextOnlyFallback();
    unicodeInputWithoutCheckpointNeverUsesTextFallback();
    terminalThenSuccessfulFlushUsesNewestSnapshot();
    uncertainFallbackRestoresAffineEffectNamespace();
    fallbackEligibilitySeparatesStoredAndLocalSemantics();
    nestedSessionNotFoundCannotDuplicateContextFallback();
    sameContextLegacySessionRenegotiatesV2();
    return 0;
}
