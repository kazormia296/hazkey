#ifndef HAZKEY_SESSION_CLIENT_H
#define HAZKEY_SESSION_CLIENT_H

#include <functional>
#include <optional>
#include <string>
#include <utility>

#include "base.pb.h"

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

HazkeyClientContextTransition evaluateHazkeyClientContextTransition(
    const HazkeyClientContext& previous, const HazkeyClientContext& next);

class HazkeyClientSession {
   public:
    explicit HazkeyClientSession(HazkeyClientContext context)
        : context_(std::move(context)) {}

    const HazkeyClientContext& context() const { return context_; }
    const std::string& id() const { return id_; }

   private:
    friend class HazkeySessionClient;

    HazkeyClientContext context_;
    std::string id_;
};

class HazkeySessionClient {
   public:
    using Transport = std::function<std::optional<hazkey::ResponseEnvelope>(
        const hazkey::RequestEnvelope&, bool)>;

    explicit HazkeySessionClient(Transport transport)
        : transport_(std::move(transport)) {}

    bool open(HazkeyClientSession& session, bool tryConnect = true);
    bool close(HazkeyClientSession& session, bool tryConnect = false);
    bool updateContext(HazkeyClientSession& session, HazkeyClientContext context);

    std::optional<hazkey::ResponseEnvelope> transact(
        HazkeyClientSession& session, hazkey::RequestEnvelope request,
        bool tryConnect = true);

   private:
    Transport transport_;
};

#endif  // HAZKEY_SESSION_CLIENT_H
