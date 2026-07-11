#include "hazkey_session_client.h"

#include <utility>

bool HazkeySessionClient::open(HazkeyClientSession& session, bool tryConnect) {
    hazkey::RequestEnvelope request;
    auto* client = request.mutable_open_session()->mutable_client();
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
    return response.has_value() && response->status() == hazkey::SUCCESS;
}

std::optional<hazkey::ResponseEnvelope> HazkeySessionClient::transact(
    HazkeyClientSession& session, hazkey::RequestEnvelope request,
    bool tryConnect) {
    if (session.id_.empty() && !open(session, tryConnect)) {
        return std::nullopt;
    }

    request.set_session_id(session.id_);
    auto response = transport_(request, tryConnect);
    if (!response.has_value() ||
        response->status() != hazkey::SESSION_NOT_FOUND) {
        return response;
    }

    // The socket owner may have changed after a reconnect. Reopen exactly
    // once and replay exactly once; a second SESSION_NOT_FOUND is returned to
    // the caller so request execution can never loop indefinitely.
    session.id_.clear();
    if (!open(session, tryConnect)) {
        return response;
    }
    request.set_session_id(session.id_);
    return transport_(request, tryConnect);
}
