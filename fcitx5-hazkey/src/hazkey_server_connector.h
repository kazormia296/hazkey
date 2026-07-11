#ifndef HAZKEY_SERVER_CONNECTOR_H
#define HAZKEY_SERVER_CONNECTOR_H

#include <fcitx/text.h>

#include <optional>
#include <string>

#include "base.pb.h"
#include "commands.pb.h"
#include "hazkey_session_client.h"

class HazkeyServerConnector;

class HazkeyServerSession {
   public:
    HazkeyServerSession(HazkeyServerConnector& connector,
                        HazkeyClientContext context);
    ~HazkeyServerSession();

    HazkeyServerSession(const HazkeyServerSession&) = delete;
    HazkeyServerSession& operator=(const HazkeyServerSession&) = delete;

    const HazkeyClientContext& context() const { return session_.context(); }
    bool updateClientContext(HazkeyClientContext context);

    std::string getComposingText(
        hazkey::commands::GetComposingString::CharType type,
        std::string currentPreedit);
    fcitx::Text getComposingHiraganaWithCursor();
    void inputChar(std::string text);
    void shiftKeyEvent(bool isRelease);
    bool currentInputModeIsDirect();
    void deleteLeft();
    void deleteRight();
    void moveCursor(int offset);
    void setContext(std::string context, int anchor);
    void newComposingText();
    void completePrefix(int index);
    void saveLearningData(bool tryConnect = true);
    hazkey::commands::CandidatesResult getCandidates(bool isSuggest);

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
        const hazkey::RequestEnvelope& sendData, bool tryConnect = true);

   private:
    friend class HazkeyServerSession;

    std::string getComposingText(
        HazkeyClientSession& session,
        hazkey::commands::GetComposingString::CharType type,
        std::string currentPreedit);
    fcitx::Text getComposingHiraganaWithCursor(HazkeyClientSession& session);
    void inputChar(HazkeyClientSession& session, std::string text);
    void shiftKeyEvent(HazkeyClientSession& session, bool isRelease);
    bool currentInputModeIsDirect(HazkeyClientSession& session);
    void deleteLeft(HazkeyClientSession& session);
    void deleteRight(HazkeyClientSession& session);
    void moveCursor(HazkeyClientSession& session, int offset);
    void setContext(HazkeyClientSession& session, std::string context, int anchor);
    void newComposingText(HazkeyClientSession& session);
    void completePrefix(HazkeyClientSession& session, int index);
    void saveLearningData(HazkeyClientSession& session, bool tryConnect = true);
    hazkey::commands::CandidatesResult getCandidates(
        HazkeyClientSession& session, bool isSuggest);

    HazkeySessionClient sessionClient_;
    int sock_ = -1;
};

#endif  // HAZKEY_SERVER_CONNECTOR_H
