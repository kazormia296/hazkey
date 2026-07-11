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
    std::vector<std::optional<hazkey::ResponseEnvelope>> responses;

    std::optional<hazkey::ResponseEnvelope> transact(
        const hazkey::RequestEnvelope& request, bool) {
        requests.push_back(request);
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
    response.mutable_open_session_result()->set_session_id(sessionId);
    return response;
}

hazkey::ResponseEnvelope status(hazkey::StatusCode code) {
    hazkey::ResponseEnvelope response;
    response.set_status(code);
    return response;
}

HazkeyClientContext context(bool secure = false) {
    return HazkeyClientContext{
        .program = "grimodex",
        .frontend = "wayland",
        .secureInput = secure,
    };
}

hazkey::RequestEnvelope inputRequest() {
    hazkey::RequestEnvelope request;
    request.mutable_input_char()->set_text("a");
    return request;
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

    auto response = client.transact(session, inputRequest());

    expect(response.has_value(), "retried command must return a response");
    expect(response->status() == hazkey::SUCCESS, "retried command must succeed");
    expect(transport.requests.size() == 4, "exactly four RPCs are expected");
    expect(transport.requests[0].has_open_session(), "first RPC must open");
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

    auto response = client.transact(session, inputRequest());

    expect(response.has_value(), "second SESSION_NOT_FOUND must be returned");
    expect(response->status() == hazkey::SESSION_NOT_FOUND, "retry must not loop");
    expect(transport.requests.size() == 4, "no third open or command is allowed");
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

    (void)client.transact(session, inputRequest());

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
    (void)client.transact(sessionA, inputRequest());
    (void)client.transact(sessionB, inputRequest());

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
    const auto recovered = client.transact(session, inputRequest());
    expect(recovered.has_value() && recovered->status() == hazkey::SUCCESS,
           "replayed request must succeed");
    expect(recoveryCount == 1,
           "owner must invalidate local composition exactly once after recovery");

    (void)client.transact(session, inputRequest());
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

}  // namespace

int main() {
    reopensAndRetriesOnlyOnce();
    stopsAfterTheSingleRetry();
    preservesContextWhenReopening();
    neverMixesTwoSessionIds();
    secureContextTransitionClearsBeforeReopening();
    notifiesTheOwnerWhenAStatefulRequestReopensTheSession();
    replacesSessionWhenClientContextChanges();
    return 0;
}
