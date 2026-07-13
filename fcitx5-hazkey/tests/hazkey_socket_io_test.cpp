#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>

#include "hazkey_socket_io.h"

namespace {

using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

[[noreturn]] void fail(const std::string& message) {
    std::cerr << message << '\n';
    std::exit(1);
}

void expect(bool condition, const std::string& message) {
    if (!condition) {
        fail(message);
    }
}

void setNonblocking(int fd) {
    const int flags = fcntl(fd, F_GETFL, 0);
    expect(flags >= 0 && fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0,
           "failed to make socket nonblocking");
}

void responseHeaderUsesTheBestEffortDeadline() {
    int sockets[2];
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0,
           "failed to create socket pair");
    setNonblocking(sockets[0]);

    const auto start = Clock::now();
    const auto deadline = start + 250ms;
    uint32_t header = 0;
    const bool read = grimodex::ime::socketio::readAll(
        sockets[0], &header, sizeof(header), deadline);
    const auto elapsed = Clock::now() - start;

    close(sockets[0]);
    close(sockets[1]);
    expect(!read, "missing response header must time out");
    expect(elapsed >= 150ms && elapsed < 750ms,
           "response header did not honor the bounded deadline");
}

void responseBodySharesTheHeaderDeadline() {
    int sockets[2];
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0,
           "failed to create socket pair");
    setNonblocking(sockets[0]);

    const uint32_t header = 4;
    expect(write(sockets[1], &header, sizeof(header)) ==
               static_cast<ssize_t>(sizeof(header)),
           "failed to write response header");
    const auto start = Clock::now();
    const auto deadline = start + 250ms;
    uint32_t receivedHeader = 0;
    expect(grimodex::ime::socketio::readAll(
               sockets[0], &receivedHeader, sizeof(receivedHeader), deadline),
           "available response header must be read");
    std::this_thread::sleep_for(150ms);
    char body[4]{};
    const bool read = grimodex::ime::socketio::readAll(
        sockets[0], body, sizeof(body), deadline);
    const auto elapsed = Clock::now() - start;

    close(sockets[0]);
    close(sockets[1]);
    expect(!read, "missing response body must time out");
    expect(elapsed >= 200ms && elapsed < 750ms,
           "response body received a fresh timeout instead of the shared deadline");
}

}  // namespace

int main() {
    responseHeaderUsesTheBestEffortDeadline();
    responseBodySharesTheHeaderDeadline();
    return 0;
}
