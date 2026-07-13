#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <filesystem>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "base.pb.h"
#include "grimodex_product_identity.h"
#include "serverconnector.h"

namespace {

void require(bool condition, const std::string& message);

class ScopedRuntimeDirectory {
   public:
    ScopedRuntimeDirectory() {
        const char* previous = std::getenv("XDG_RUNTIME_DIR");
        if (previous != nullptr) {
            previousRuntimeDirectory_ = previous;
        }

        char pathTemplate[] = "/tmp/grimodex-settings-save-XXXXXX";
        char* created = mkdtemp(pathTemplate);
        if (created == nullptr) {
            throw std::runtime_error("failed to create temporary runtime root");
        }
        root_ = created;
        if (setenv("XDG_RUNTIME_DIR", root_.c_str(), 1) != 0) {
            throw std::runtime_error("failed to set XDG_RUNTIME_DIR");
        }

        paths_ = grimodex::ime::resolveRuntimePaths(root_.c_str(), getuid());
        if (mkdir(paths_.directory.c_str(), 0700) != 0) {
            throw std::runtime_error("failed to create product runtime directory");
        }
    }

    ~ScopedRuntimeDirectory() {
        if (previousRuntimeDirectory_.has_value()) {
            setenv("XDG_RUNTIME_DIR", previousRuntimeDirectory_->c_str(), 1);
        } else {
            unsetenv("XDG_RUNTIME_DIR");
        }
        std::error_code error;
        std::filesystem::remove_all(root_, error);
    }

    const std::string& socketPath() const { return paths_.socket; }

   private:
    std::optional<std::string> previousRuntimeDirectory_;
    std::string root_;
    grimodex::ime::RuntimePaths paths_;
};

void readExactly(int socket, void* destination, std::size_t size) {
    std::size_t received = 0;
    while (received < size) {
        const ssize_t count =
            read(socket, static_cast<char*>(destination) + received,
                 size - received);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            throw std::runtime_error("mock server failed to read request");
        }
        received += static_cast<std::size_t>(count);
    }
}

void writeExactly(int socket, const void* source, std::size_t size) {
    std::size_t sent = 0;
    while (sent < size) {
        const ssize_t count =
            write(socket, static_cast<const char*>(source) + sent, size - sent);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            throw std::runtime_error("mock server failed to write response");
        }
        sent += static_cast<std::size_t>(count);
    }
}

int createListener(const std::string& socketPath) {
    const int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0) {
        throw std::runtime_error("failed to create mock server socket");
    }

    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (socketPath.size() >= sizeof(address.sun_path)) {
        close(listener);
        throw std::runtime_error("mock server socket path is too long");
    }
    std::memcpy(address.sun_path, socketPath.c_str(), socketPath.size() + 1);

    if (bind(listener, reinterpret_cast<sockaddr*>(&address),
             sizeof(address)) != 0) {
        const std::string message =
            "failed to bind mock server socket: " +
            std::string(std::strerror(errno));
        close(listener);
        throw std::runtime_error(message);
    }
    if (listen(listener, 1) != 0) {
        const std::string message =
            "failed to listen on mock server socket: " +
            std::string(std::strerror(errno));
        close(listener);
        throw std::runtime_error(message);
    }
    return listener;
}

hazkey::RequestEnvelope receiveRequest(int client) {
    std::uint32_t networkLength = 0;
    readExactly(client, &networkLength, sizeof(networkLength));
    const std::uint32_t requestLength = ntohl(networkLength);
    std::vector<char> request(requestLength);
    readExactly(client, request.data(), request.size());

    hazkey::RequestEnvelope envelope;
    if (!envelope.ParseFromArray(request.data(),
                                 static_cast<int>(request.size()))) {
        throw std::runtime_error("mock server received malformed protobuf");
    }
    return envelope;
}

void sendResponse(int client, hazkey::StatusCode status,
                  const std::string& errorMessage) {
    hazkey::ResponseEnvelope response;
    response.set_status(status);
    response.set_error_message(errorMessage);

    std::string payload;
    if (!response.SerializeToString(&payload)) {
        throw std::runtime_error("failed to serialize mock server response");
    }
    const std::uint32_t networkLength =
        htonl(static_cast<std::uint32_t>(payload.size()));
    writeExactly(client, &networkLength, sizeof(networkLength));
    writeExactly(client, payload.data(), payload.size());
}

std::optional<std::string> saveWithResponse(
    std::optional<hazkey::StatusCode> status,
    const std::string& errorMessage = {}) {
    ScopedRuntimeDirectory runtimeDirectory;
    const int listener = createListener(runtimeDirectory.socketPath());
    std::exception_ptr serverFailure;
    std::thread server([&]() {
        try {
            const int client = accept(listener, nullptr, nullptr);
            if (client < 0) {
                throw std::runtime_error("mock server failed to accept client");
            }
            const auto request = receiveRequest(client);
            if (!request.has_set_config()) {
                throw std::runtime_error(
                    "mock server received an unexpected request");
            }
            if (status.has_value()) {
                sendResponse(client, *status, errorMessage);
            }
            close(client);
        } catch (...) {
            serverFailure = std::current_exception();
        }
    });

    std::optional<std::string> failure;
    try {
        ServerConnector connector;
        hazkey::config::CurrentConfig config;
        connector.setCurrentConfig(config);
    } catch (const std::runtime_error& error) {
        failure = error.what();
    }

    server.join();
    close(listener);
    if (serverFailure != nullptr) {
        std::rethrow_exception(serverFailure);
    }
    return failure;
}

void dictionaryRoundTrip() {
    ScopedRuntimeDirectory runtimeDirectory;
    const int listener = createListener(runtimeDirectory.socketPath());
    std::exception_ptr serverFailure;
    std::thread server([&]() {
        try {
            for (int operation = 0; operation < 6; ++operation) {
                const int client = accept(listener, nullptr, nullptr);
                if (client < 0) {
                    throw std::runtime_error(
                        "mock dictionary server failed to accept client");
                }
                const auto request = receiveRequest(client);
                hazkey::ResponseEnvelope response;
                response.set_status(hazkey::SUCCESS);
                switch (operation) {
                    case 0: {
                        require(request.has_list_user_dictionary(),
                                "list request was not serialized");
                        auto* entry = response.mutable_user_dictionary_result()
                                          ->add_entries();
                        entry->set_id("entry-1");
                        entry->set_reading("せつな");
                        entry->set_surface("刹那");
                        entry->set_part_of_speech("person");
                        entry->set_layer(hazkey::config::PERSONAL);
                        break;
                    }
                    case 1:
                        require(request.has_add_user_dictionary_entry() &&
                                    request.add_user_dictionary_entry()
                                            .entry()
                                            .surface() == "刹那",
                                "add request lost its entry");
                        break;
                    case 2:
                        require(request.has_update_user_dictionary_entry() &&
                                    request.update_user_dictionary_entry()
                                            .entry()
                                            .id() == "entry-1",
                                "update request lost its id");
                        break;
                    case 3:
                        require(request.has_remove_user_dictionary_entry() &&
                                    request.remove_user_dictionary_entry().id() ==
                                        "entry-1",
                                "remove request lost its id");
                        break;
                    case 4:
                        require(request.has_import_user_dictionary() &&
                                    request.import_user_dictionary().merge() &&
                                    request.import_user_dictionary().json() ==
                                        "[]",
                                "import request lost JSON or merge mode");
                        break;
                    case 5:
                        require(request.has_export_user_dictionary(),
                                "export request was not serialized");
                        response.mutable_user_dictionary_result()
                            ->set_exported_json("[{\"id\":\"entry-1\"}]");
                        break;
                }
                std::string payload;
                require(response.SerializeToString(&payload),
                        "failed to serialize dictionary response");
                const std::uint32_t networkLength =
                    htonl(static_cast<std::uint32_t>(payload.size()));
                writeExactly(client, &networkLength, sizeof(networkLength));
                writeExactly(client, payload.data(), payload.size());
                close(client);
            }
        } catch (...) {
            serverFailure = std::current_exception();
        }
    });

    ServerConnector connector;
    const auto entries = connector.listUserDictionary();
    require(entries.has_value() && entries->size() == 1 &&
                entries->front().surface() == "刹那",
            "list response was not decoded");
    auto entry = entries->front();
    require(connector.addUserDictionaryEntry(entry), "add request failed");
    require(connector.updateUserDictionaryEntry(entry),
            "update request failed");
    require(connector.removeUserDictionaryEntry(entry.id()),
            "remove request failed");
    require(connector.importUserDictionary("[]", true),
            "import request failed");
    const auto exported = connector.exportUserDictionary();
    require(exported.has_value() &&
                *exported == "[{\"id\":\"entry-1\"}]",
            "export response was not decoded");

    server.join();
    close(listener);
    if (serverFailure != nullptr) {
        std::rethrow_exception(serverFailure);
    }
}

void require(bool condition, const std::string& message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

}  // namespace

int main() {
    try {
        const auto rejected =
            saveWithResponse(hazkey::FAILED, "configuration rejected");
        require(rejected.has_value(),
                "FAILED response was silently accepted as a successful save");
        require(rejected->find("configuration rejected") != std::string::npos,
                "server rejection detail was not propagated to the UI layer");

        const auto disconnected = saveWithResponse(std::nullopt);
        require(disconnected.has_value(),
                "disconnected response was silently accepted as a successful save");

        const auto saved = saveWithResponse(hazkey::SUCCESS);
        require(!saved.has_value(), "successful response raised a save error");

        dictionaryRoundTrip();
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
    return 0;
}
