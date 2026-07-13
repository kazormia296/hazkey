#ifndef SERVERCONNECTOR_H
#define SERVERCONNECTOR_H

#include <optional>
#include <string>
#include <vector>

#include "base.pb.h"

class ServerConnector {
   public:
    ServerConnector();
    ~ServerConnector();
    std::optional<hazkey::config::CurrentConfig> getConfig();
    void setCurrentConfig(hazkey::config::CurrentConfig);
    bool clearAllHistory(const std::string& profileId);
    bool reloadZenzaiModel();
    std::optional<std::vector<hazkey::config::UserDictionaryEntry>>
    listUserDictionary();
    bool addUserDictionaryEntry(
        const hazkey::config::UserDictionaryEntry& entry);
    bool updateUserDictionaryEntry(
        const hazkey::config::UserDictionaryEntry& entry);
    bool removeUserDictionaryEntry(const std::string& id);
    bool importUserDictionary(const std::string& json, bool merge);
    std::optional<std::string> exportUserDictionary();

    // Begin a session with persistent connection
    bool beginSession();
    // End the session and close connection
    void endSession();
    // Session-aware versions of methods
    std::optional<hazkey::config::CurrentConfig> getConfigInSession();
    bool reloadZenzaiModelInSession();

   private:
    std::string getSocketPath();
    int createConnection();
    std::optional<hazkey::ResponseEnvelope> transact(
        const hazkey::RequestEnvelope& send_data);
    std::optional<hazkey::ResponseEnvelope> transactOnSocket(
        int sock, const hazkey::RequestEnvelope& send_data);

    int session_socket_;
};

#endif  // SERVERCONNECTOR_H
