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

void receiveRequest(int client) {
    std::uint32_t networkLength = 0;
    readExactly(client, &networkLength, sizeof(networkLength));
    const std::uint32_t requestLength = ntohl(networkLength);
    std::vector<char> request(requestLength);
    readExactly(client, request.data(), request.size());

    hazkey::RequestEnvelope envelope;
    if (!envelope.ParseFromArray(request.data(),
                                 static_cast<int>(request.size())) ||
        !envelope.has_set_config()) {
        throw std::runtime_error("mock server received an unexpected request");
    }
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
            receiveRequest(client);
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
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
    return 0;
}
