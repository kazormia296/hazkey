#ifndef HAZKEY_SOCKET_IO_H
#define HAZKEY_SOCKET_IO_H

#include <chrono>
#include <cstddef>
#include <optional>

namespace grimodex::ime::socketio {

using Deadline = std::optional<std::chrono::steady_clock::time_point>;

bool writeAll(int fd, const void* data, std::size_t length,
              const Deadline& deadline = std::nullopt);
bool readAll(int fd, void* data, std::size_t length,
             const Deadline& deadline = std::nullopt);

}  // namespace grimodex::ime::socketio

#endif  // HAZKEY_SOCKET_IO_H
