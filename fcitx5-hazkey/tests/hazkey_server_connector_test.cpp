#include <arpa/inet.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <optional>
#include <string>
#include <thread>

#include "base.pb.h"
#include "hazkey_server_connector.h"

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
           "failed to make connector socket nonblocking");
}

bool readAll(int fd, void* destination, std::size_t size) {
    auto* output = static_cast<char*>(destination);
    std::size_t offset = 0;
    while (offset < size) {
        const auto count = read(fd, output + offset, size - offset);
        if (count <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(count);
    }
    return true;
}

bool writeAll(int fd, const void* source, std::size_t size) {
    const auto* input = static_cast<const char*>(source);
    std::size_t offset = 0;
    while (offset < size) {
        const auto count = write(fd, input + offset, size - offset);
        if (count <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(count);
    }
    return true;
}

std::optional<hazkey::RequestEnvelope> readRequest(int fd) {
    uint32_t networkLength = 0;
    if (!readAll(fd, &networkLength, sizeof(networkLength))) {
        std::cerr << "failed to read frame header\n";
        return std::nullopt;
    }
    const uint32_t length = ntohl(networkLength);
    std::string payload(length, '\0');
    if (!readAll(fd, payload.data(), payload.size())) {
        std::cerr << "failed to read frame body length=" << length << "\n";
        return std::nullopt;
    }
    hazkey::RequestEnvelope request;
    if (!request.ParseFromString(payload)) {
        std::cerr << "failed to parse request length=" << length << "\n";
        return std::nullopt;
    }
    return request;
}

void writeResponse(int fd, const hazkey::ResponseEnvelope& response) {
    std::string payload;
    expect(response.SerializeToString(&payload), "failed to encode response");
    const uint32_t networkLength = htonl(static_cast<uint32_t>(payload.size()));
    expect(writeAll(fd, &networkLength, sizeof(networkLength)) &&
               writeAll(fd, payload.data(), payload.size()),
           "failed to write framed response");
}

hazkey::RequestEnvelope lifecycleDiscard() {
    hazkey::RequestEnvelope request;
    request.set_session_id("session-a");
    auto* action = request.mutable_handle_ime_action();
    action->set_request_id("discard-1");
    action->set_expected_revision(1);
    action->mutable_resolve_pending_learning()->set_commit(false);
    return request;
}

hazkey::RequestEnvelope ordinaryRequest() {
    hazkey::RequestEnvelope request;
    request.mutable_open_session()->set_client_feature_bits(3);
    return request;
}

hazkey::ResponseEnvelope successResponse() {
    hazkey::ResponseEnvelope response;
    response.set_status(hazkey::SUCCESS);
    return response;
}

void lifecycleHeadKeepsItsShortBudgetWhenNormalResumesIt() {
    int sockets[2];
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0,
           "failed to create lifecycle socket pair");
    setNonblocking(sockets[0]);
    std::atomic<bool> normalReceived{false};

    std::thread server([&] {
        const auto lifecycle = readRequest(sockets[1]);
        expect(lifecycle.has_value() && lifecycle->has_handle_ime_action(),
               "server must receive the lifecycle head first");
        std::this_thread::sleep_for(35ms);
        writeResponse(sockets[1], successResponse());
        const auto normal = readRequest(sockets[1]);
        normalReceived.store(normal.has_value() && normal->has_open_session(),
                             std::memory_order_release);
        writeResponse(sockets[1], successResponse());
        close(sockets[1]);
    });

    {
        HazkeyServerConnector connector(sockets[0]);
        const auto lifecycleStart = Clock::now();
        expect(!connector
                    .transact(lifecycleDiscard(), false,
                              HazkeyTransportPolicy::lifecycle)
                    .has_value(),
               "a stalled lifecycle response must defer");
        expect(Clock::now() - lifecycleStart < 60ms,
               "initial lifecycle call exceeded its short budget");

        const auto normalStart = Clock::now();
        expect(!connector.transact(ordinaryRequest(), false).has_value(),
               "normal RPC must remain unsent behind a stalled head");
        expect(Clock::now() - normalStart < 60ms,
               "normal caller inherited an unbounded lifecycle read");
        expect(!normalReceived.load(std::memory_order_acquire),
               "normal frame overtook the lifecycle response");

        std::this_thread::sleep_for(40ms);
        expect(connector.transact(ordinaryRequest(), false).has_value(),
               "normal RPC must resume after the lifecycle response drains");
        expect(normalReceived.load(std::memory_order_acquire),
               "normal frame was not sent after stream resynchronization");
    }
    server.join();
}

void permanentlyStalledLifecycleHeadEventuallyDisconnects() {
    int sockets[2];
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0,
           "failed to create lifecycle expiry socket pair");
    setNonblocking(sockets[0]);
    std::atomic<bool> sawEof{false};
    std::atomic<bool> sawOvertake{false};
    std::thread server([&] {
        expect(readRequest(sockets[1]).has_value(),
               "server must receive the lifecycle request");
        char buffer[64];
        const auto count = read(sockets[1], buffer, sizeof(buffer));
        sawOvertake.store(count > 0, std::memory_order_release);
        sawEof.store(count == 0, std::memory_order_release);
        close(sockets[1]);
    });

    {
        HazkeyServerConnector connector(sockets[0]);
        (void)connector.transact(lifecycleDiscard(), false,
                                 HazkeyTransportPolicy::lifecycle);
        std::this_thread::sleep_for(120ms);
        const auto started = Clock::now();
        expect(!connector.transact(ordinaryRequest(), false).has_value(),
               "expired lifecycle head must force recovery");
        expect(Clock::now() - started < 60ms,
               "expired lifecycle head blocked the normal caller");
    }
    server.join();
    expect(sawEof.load(std::memory_order_acquire),
           "absolute lifecycle age cap must disconnect the stalled stream");
    expect(!sawOvertake.load(std::memory_order_acquire),
           "normal request must never be written before stalled-head recovery");
}

void stalledBestEffortHeadUsesOnlyItsOwnBudget() {
    int sockets[2];
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0,
           "failed to create best-effort socket pair");
    setNonblocking(sockets[0]);
    std::atomic<bool> sawEof{false};
    std::thread server([&] {
        expect(readRequest(sockets[1]).has_value(),
               "server must receive the best-effort request");
        char byte = 0;
        sawEof.store(read(sockets[1], &byte, 1) == 0,
                     std::memory_order_release);
        close(sockets[1]);
    });

    {
        HazkeyServerConnector connector(sockets[0]);
        const auto started = Clock::now();
        expect(!connector
                    .transact(ordinaryRequest(), false,
                              HazkeyTransportPolicy::bestEffort)
                    .has_value(),
               "stalled best-effort request must fail");
        const auto elapsed = Clock::now() - started;
        expect(elapsed >= 150ms && elapsed < 700ms,
               "best-effort exchange did not honor its 250ms total budget");
    }
    server.join();
    expect(sawEof.load(std::memory_order_acquire),
           "exhausted best-effort head must recover the shared stream");
}

void lifecycleQueuedBehindNormalIsHandedOffWithoutAFutureCaller() {
    int sockets[2];
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0,
           "failed to create concurrent handoff socket pair");
    setNonblocking(sockets[0]);
    std::atomic<bool> normalWasRead{false};
    std::atomic<bool> normalReturned{false};
    std::atomic<bool> lifecycleBeforeReturn{false};

    std::thread server([&] {
        const auto normal = readRequest(sockets[1]);
        expect(normal.has_value() && normal->has_open_session(),
               "normal owner must write its frame first");
        normalWasRead.store(true, std::memory_order_release);
        std::this_thread::sleep_for(25ms);
        writeResponse(sockets[1], successResponse());
        const auto lifecycle = readRequest(sockets[1]);
        lifecycleBeforeReturn.store(
            lifecycle.has_value() && lifecycle->has_handle_ime_action() &&
                !normalReturned.load(std::memory_order_acquire),
            std::memory_order_release);
        writeResponse(sockets[1], successResponse());
        close(sockets[1]);
    });

    {
        HazkeyServerConnector connector(sockets[0]);
        std::optional<hazkey::ResponseEnvelope> normalResult;
        std::thread normalCaller([&] {
            normalResult = connector.transact(ordinaryRequest(), false);
            normalReturned.store(true, std::memory_order_release);
        });
        const auto waitDeadline = Clock::now() + 500ms;
        while (!normalWasRead.load(std::memory_order_acquire) &&
               Clock::now() < waitDeadline) {
            std::this_thread::yield();
        }
        expect(normalWasRead.load(std::memory_order_acquire),
               "normal request was not observed in time");
        const auto queuedAt = Clock::now();
        expect(!connector
                    .transact(lifecycleDiscard(), false,
                              HazkeyTransportPolicy::lifecycle)
                    .has_value(),
               "contended lifecycle request must transfer queue ownership");
        expect(Clock::now() - queuedAt < 60ms,
               "contended lifecycle handoff exceeded its short deadline");
        normalCaller.join();
        expect(normalResult.has_value(),
               "normal owner must still receive its own response");
    }
    server.join();
    expect(lifecycleBeforeReturn.load(std::memory_order_acquire),
           "normal owner must flush newly queued lifecycle work before returning");
}

void peerCloseFailsWithoutBlockingOrSigpipe() {
    int sockets[2];
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0,
           "failed to create peer-close socket pair");
    setNonblocking(sockets[0]);
    close(sockets[1]);
    const auto started = Clock::now();
    {
        HazkeyServerConnector connector(sockets[0]);
        expect(!connector.transact(ordinaryRequest(), false).has_value(),
               "closed peer must fail the exchange");
    }
    expect(Clock::now() - started < 60ms,
           "closed peer must not consume a transport timeout");
}

}  // namespace

int main() {
    signal(SIGPIPE, SIG_IGN);
    lifecycleHeadKeepsItsShortBudgetWhenNormalResumesIt();
    permanentlyStalledLifecycleHeadEventuallyDisconnects();
    stalledBestEffortHeadUsesOnlyItsOwnBudget();
    lifecycleQueuedBehindNormalIsHandedOffWithoutAFutureCaller();
    peerCloseFailsWithoutBlockingOrSigpipe();
    return 0;
}
