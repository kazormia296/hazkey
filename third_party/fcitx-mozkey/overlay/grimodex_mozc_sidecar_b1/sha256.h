// SPDX-License-Identifier: MIT

#ifndef GRIMODEX_MOZC_SIDECAR_SHA256_H_
#define GRIMODEX_MOZC_SIDECAR_SHA256_H_

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <string_view>
#include <utility>

#include <fcntl.h>
#include <linux/memfd.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

namespace hazkey::mozc_sidecar_internal {

// Small dependency-free SHA-256 implementation used only to authenticate the
// fixed Mozc dataset before the helper loads it.
class Sha256 final {
  public:
    bool Update(const uint8_t* data, size_t size) {
        constexpr uint64_t kMaximumHashedBytes =
            std::numeric_limits<uint64_t>::max() / 8;
        if (size > kMaximumHashedBytes - total_bytes_) {
            return false;
        }
        total_bytes_ += size;

        while (size > 0) {
            const size_t copied = std::min(size, block_.size() - block_size_);
            std::copy_n(data, copied, block_.begin() + block_size_);
            block_size_ += copied;
            data += copied;
            size -= copied;
            if (block_size_ == block_.size()) {
                Transform(block_.data());
                block_size_ = 0;
            }
        }
        return true;
    }

    std::array<uint8_t, 32> Finalize() {
        const uint64_t total_bits = total_bytes_ * 8;
        block_[block_size_++] = 0x80;
        if (block_size_ > 56) {
            std::fill(block_.begin() + block_size_, block_.end(), 0);
            Transform(block_.data());
            block_size_ = 0;
        }
        std::fill(block_.begin() + block_size_, block_.begin() + 56, 0);
        for (size_t index = 0; index < 8; ++index) {
            block_[63 - index] =
                static_cast<uint8_t>(total_bits >> (index * 8));
        }
        Transform(block_.data());

        std::array<uint8_t, 32> digest = {};
        for (size_t index = 0; index < state_.size(); ++index) {
            digest[index * 4] = static_cast<uint8_t>(state_[index] >> 24);
            digest[index * 4 + 1] =
                static_cast<uint8_t>(state_[index] >> 16);
            digest[index * 4 + 2] =
                static_cast<uint8_t>(state_[index] >> 8);
            digest[index * 4 + 3] = static_cast<uint8_t>(state_[index]);
        }
        return digest;
    }

  private:
    static uint32_t RotateRight(uint32_t value, uint32_t count) {
        return (value >> count) | (value << (32 - count));
    }

    void Transform(const uint8_t* block) {
        static constexpr std::array<uint32_t, 64> kRoundConstants = {
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
            0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
            0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
            0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
            0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
            0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
            0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
            0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
            0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
            0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
            0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
            0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
            0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
            0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
            0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
            0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
        };

        std::array<uint32_t, 64> schedule = {};
        for (size_t index = 0; index < 16; ++index) {
            const size_t offset = index * 4;
            schedule[index] =
                (static_cast<uint32_t>(block[offset]) << 24) |
                (static_cast<uint32_t>(block[offset + 1]) << 16) |
                (static_cast<uint32_t>(block[offset + 2]) << 8) |
                static_cast<uint32_t>(block[offset + 3]);
        }
        for (size_t index = 16; index < schedule.size(); ++index) {
            const uint32_t previous15 = schedule[index - 15];
            const uint32_t previous2 = schedule[index - 2];
            const uint32_t sigma0 = RotateRight(previous15, 7) ^
                                    RotateRight(previous15, 18) ^
                                    (previous15 >> 3);
            const uint32_t sigma1 = RotateRight(previous2, 17) ^
                                    RotateRight(previous2, 19) ^
                                    (previous2 >> 10);
            schedule[index] = schedule[index - 16] + sigma0 +
                              schedule[index - 7] + sigma1;
        }

        uint32_t a = state_[0];
        uint32_t b = state_[1];
        uint32_t c = state_[2];
        uint32_t d = state_[3];
        uint32_t e = state_[4];
        uint32_t f = state_[5];
        uint32_t g = state_[6];
        uint32_t h = state_[7];
        for (size_t index = 0; index < schedule.size(); ++index) {
            const uint32_t sum1 = RotateRight(e, 6) ^ RotateRight(e, 11) ^
                                  RotateRight(e, 25);
            const uint32_t choice = (e & f) ^ (~e & g);
            const uint32_t temporary1 =
                h + sum1 + choice + kRoundConstants[index] + schedule[index];
            const uint32_t sum0 = RotateRight(a, 2) ^ RotateRight(a, 13) ^
                                  RotateRight(a, 22);
            const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
            const uint32_t temporary2 = sum0 + majority;
            h = g;
            g = f;
            f = e;
            e = d + temporary1;
            d = c;
            c = b;
            b = a;
            a = temporary1 + temporary2;
        }
        state_[0] += a;
        state_[1] += b;
        state_[2] += c;
        state_[3] += d;
        state_[4] += e;
        state_[5] += f;
        state_[6] += g;
        state_[7] += h;
    }

    std::array<uint32_t, 8> state_ = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    };
    std::array<uint8_t, 64> block_ = {};
    size_t block_size_ = 0;
    uint64_t total_bytes_ = 0;
};

class ScopedFileDescriptor final {
  public:
    ScopedFileDescriptor() = default;
    explicit ScopedFileDescriptor(int descriptor) : descriptor_(descriptor) {}
    ScopedFileDescriptor(const ScopedFileDescriptor&) = delete;
    ScopedFileDescriptor& operator=(const ScopedFileDescriptor&) = delete;
    ScopedFileDescriptor(ScopedFileDescriptor&& other) noexcept
        : descriptor_(other.descriptor_) {
        other.descriptor_ = -1;
    }
    ScopedFileDescriptor& operator=(ScopedFileDescriptor&& other) noexcept {
        if (this != &other) {
            Reset(other.descriptor_);
            other.descriptor_ = -1;
        }
        return *this;
    }
    ~ScopedFileDescriptor() { Reset(); }

    int get() const { return descriptor_; }

  private:
    void Reset(int descriptor = -1) {
        if (descriptor_ >= 0) {
            close(descriptor_);
        }
        descriptor_ = descriptor;
    }

    int descriptor_ = -1;
};

inline bool WriteAll(int descriptor, const uint8_t* data, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        const ssize_t bytes_written =
            write(descriptor, data + offset, size - offset);
        if (bytes_written > 0) {
            offset += static_cast<size_t>(bytes_written);
            continue;
        }
        if (bytes_written < 0 && errno == EINTR) {
            continue;
        }
        return false;
    }
    return true;
}

inline std::string HexDigest(const std::array<uint8_t, 32>& digest) {
    static constexpr char kHexDigits[] = "0123456789abcdef";
    std::string result;
    result.reserve(digest.size() * 2);
    for (const uint8_t byte : digest) {
        result.push_back(kHexDigits[byte >> 4]);
        result.push_back(kHexDigits[byte & 0x0f]);
    }
    return result;
}

inline bool ComputeFileSHA256(int descriptor, std::string* result) {
    if (lseek(descriptor, 0, SEEK_SET) < 0) {
        return false;
    }

    Sha256 sha256;
    std::array<uint8_t, 32 * 1024> buffer = {};
    for (;;) {
        const ssize_t bytes_read =
            read(descriptor, buffer.data(), buffer.size());
        if (bytes_read > 0) {
            if (!sha256.Update(buffer.data(),
                               static_cast<size_t>(bytes_read))) {
                return false;
            }
            continue;
        }
        if (bytes_read == 0) {
            break;
        }
        if (errno != EINTR) {
            return false;
        }
    }
    if (lseek(descriptor, 0, SEEK_SET) < 0) {
        return false;
    }
    *result = HexDigest(sha256.Finalize());
    return true;
}

inline bool CreateSealedSnapshot(int source_descriptor, off_t expected_size,
                                 std::string_view expected_sha256,
                                 ScopedFileDescriptor* result) {
#if !defined(SYS_memfd_create)
    return false;
#else
    struct stat source_status = {};
    if (source_descriptor < 0 || result == nullptr || expected_size < 0 ||
        fstat(source_descriptor, &source_status) != 0 ||
        !S_ISREG(source_status.st_mode) ||
        source_status.st_size != expected_size ||
        lseek(source_descriptor, 0, SEEK_SET) < 0) {
        return false;
    }

    ScopedFileDescriptor snapshot(static_cast<int>(syscall(
        SYS_memfd_create, "grimodex-mozc-data",
        static_cast<unsigned int>(MFD_CLOEXEC | MFD_ALLOW_SEALING))));
    if (snapshot.get() < 0) {
        return false;
    }

    Sha256 sha256;
    off_t copied_size = 0;
    std::array<uint8_t, 32 * 1024> buffer = {};
    while (copied_size < expected_size) {
        const off_t remaining = expected_size - copied_size;
        const size_t requested_size = std::min(
            buffer.size(), static_cast<size_t>(remaining));
        const ssize_t bytes_read =
            read(source_descriptor, buffer.data(), requested_size);
        if (bytes_read > 0) {
            const size_t size = static_cast<size_t>(bytes_read);
            if (!sha256.Update(buffer.data(), size) ||
                !WriteAll(snapshot.get(), buffer.data(), size)) {
                return false;
            }
            copied_size += static_cast<off_t>(bytes_read);
            continue;
        }
        if (bytes_read == 0) {
            return false;
        }
        if (errno != EINTR) {
            return false;
        }
    }

    const std::string actual_sha256 = HexDigest(sha256.Finalize());
    struct stat snapshot_status = {};
    if (copied_size != expected_size || actual_sha256 != expected_sha256 ||
        fstat(snapshot.get(), &snapshot_status) != 0 ||
        !S_ISREG(snapshot_status.st_mode) ||
        snapshot_status.st_size != expected_size ||
        lseek(snapshot.get(), 0, SEEK_SET) < 0) {
        return false;
    }

    constexpr int kRequiredSeals =
        F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL;
    if (fcntl(snapshot.get(), F_ADD_SEALS, kRequiredSeals) != 0) {
        return false;
    }
    const int actual_seals = fcntl(snapshot.get(), F_GET_SEALS);
    if (actual_seals < 0 ||
        (actual_seals & kRequiredSeals) != kRequiredSeals) {
        return false;
    }
    *result = std::move(snapshot);
    return true;
#endif
}

}  // namespace hazkey::mozc_sidecar_internal

#endif  // GRIMODEX_MOZC_SIDECAR_SHA256_H_
