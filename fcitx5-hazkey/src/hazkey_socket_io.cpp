#include "hazkey_socket_io.h"

#include <sys/select.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>

namespace grimodex::ime::socketio {
namespace {

using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

bool waitUntilReady(int fd, bool readable, const Deadline& deadline,
                    std::chrono::microseconds normalTimeout) {
    while (true) {
        auto timeout = normalTimeout;
        if (deadline.has_value()) {
            const auto remaining = std::chrono::duration_cast<std::chrono::microseconds>(
                *deadline - Clock::now());
            if (remaining <= 0us) {
                return false;
            }
            timeout = remaining;
        }

        const auto totalMicroseconds =
            std::max<int64_t>(1, timeout.count());
        timeval value{
            .tv_sec = static_cast<time_t>(
                totalMicroseconds / 1'000'000),
            .tv_usec = static_cast<suseconds_t>(
                totalMicroseconds % 1'000'000),
        };
        fd_set descriptors;
        FD_ZERO(&descriptors);
        FD_SET(fd, &descriptors);
        const int result = readable
                               ? select(fd + 1, &descriptors, nullptr, nullptr,
                                        &value)
                               : select(fd + 1, nullptr, &descriptors, nullptr,
                                        &value);
        if (result > 0) {
            return true;
        }
        if (result == 0 || errno != EINTR) {
            return false;
        }
    }
}

}  // namespace

bool writeAll(int fd, const void* data, std::size_t length,
              const Deadline& deadline) {
    std::size_t sent = 0;
    while (sent < length) {
        if (deadline.has_value() && Clock::now() >= *deadline) {
            return false;
        }
        const auto count =
            write(fd, static_cast<const char*>(data) + sent, length - sent);
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            if ((errno == EAGAIN || errno == EWOULDBLOCK) &&
                waitUntilReady(fd, false, deadline, 2s)) {
                continue;
            }
            return false;
        }
        if (count == 0) {
            return false;
        }
        sent += static_cast<std::size_t>(count);
    }
    return true;
}

bool readAll(int fd, void* data, std::size_t length,
             const Deadline& deadline) {
    std::size_t received = 0;
    while (received < length) {
        if (deadline.has_value() && Clock::now() >= *deadline) {
            return false;
        }
        const auto count =
            read(fd, static_cast<char*>(data) + received, length - received);
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            if ((errno == EAGAIN || errno == EWOULDBLOCK) &&
                waitUntilReady(fd, true, deadline, 10s)) {
                continue;
            }
            return false;
        }
        if (count == 0) {
            return false;
        }
        received += static_cast<std::size_t>(count);
    }
    return true;
}

}  // namespace grimodex::ime::socketio
