#include <cstdlib>
#include <iostream>
#include <optional>
#include <string>
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
        status(hazkey::SUCCESS),
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
    expect(transport.requests[0].open_session().client_feature_bits() == 1,
           "current clients must advertise delayed-effect support");
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
        status(hazkey::SUCCESS),
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
        status(hazkey::SUCCESS),
        status(hazkey::SUCCESS),
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
        status(hazkey::SUCCESS),
        status(hazkey::SUCCESS),
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
    FakeTransport transport;
    transport.responses = {
        openSuccess("session-v2-a"),
        v2Response(1, "", hazkey::SUCCESS, "かな"),
        status(hazkey::SESSION_NOT_FOUND),
        openSuccess("session-v2-b"),
        v2Response(1, "", hazkey::SUCCESS, "かな"),
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
    expect(client.transactV2(session, first).has_value(),
           "the initial snapshot must be confirmed");
    expect(session.shouldApplyEffect(1), "the old fallback namespace must accept effect one");
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
    expect(session.shouldApplyEffect(1),
           "fallback reconstruction must start a fresh Effect-ID namespace");
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
    abandoningAFallthroughKeyClearsItsRecoveryJournal();
    replaysAnUnacknowledgedJournalEntryBeforeTheNextAction();
    restoresCheckpointBeforeReplayingAfterServerRestart();
    fallsBackToTheLastVisiblePreeditWhenCheckpointIsUnavailable();
    return 0;
}
