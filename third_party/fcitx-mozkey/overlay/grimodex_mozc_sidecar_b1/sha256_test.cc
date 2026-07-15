// SPDX-License-Identifier: MIT

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdint>
#include <string>
#include <string_view>

#include <fcntl.h>
#include <linux/memfd.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "grimodex_mozc_sidecar_b1/sha256.h"
#include "testing/gunit.h"

namespace hazkey::mozc_sidecar_internal {
namespace {

std::string Digest(std::string_view input, size_t chunk_size) {
    Sha256 sha256;
    size_t offset = 0;
    while (offset < input.size()) {
        const size_t size = std::min(chunk_size, input.size() - offset);
        EXPECT_TRUE(sha256.Update(
            reinterpret_cast<const uint8_t*>(input.data() + offset), size));
        offset += size;
    }

    static constexpr char kHexDigits[] = "0123456789abcdef";
    const std::array<uint8_t, 32> digest = sha256.Finalize();
    std::string result;
    result.reserve(digest.size() * 2);
    for (const uint8_t byte : digest) {
        result.push_back(kHexDigits[byte >> 4]);
        result.push_back(kHexDigits[byte & 0x0f]);
    }
    return result;
}

TEST(Sha256Test, KnownVectorsAndIncrementalUpdates) {
    EXPECT_EQ(Digest("", 1),
              "e3b0c44298fc1c149afbf4c8996fb924"
              "27ae41e4649b934ca495991b7852b855");
    EXPECT_EQ(Digest("abc", 3),
              "ba7816bf8f01cfea414140de5dae2223"
              "b00361a396177a9cb410ff61f20015ad");
    EXPECT_EQ(
        Digest("abcdbcdecdefdefgefghfghighijhijk"
               "ijkljklmklmnlmnomnopnopq",
               1),
        "248d6a61d20638b8e5c026930c3e6039"
        "a33ce45964ff2167f6ecedd419db06c1");
    EXPECT_EQ(Digest(std::string(1'000'000, 'a'), 32 * 1024),
              "cdc76e5c9914fb9281a1c7e284d73e67"
              "f1809a48a497200e046d39ccc7112cd0");
}

TEST(Sha256Test, SealedSnapshotIsIndependentFromMutableSource) {
    ScopedFileDescriptor source(static_cast<int>(syscall(
        SYS_memfd_create, "grimodex-mozc-data-source",
        static_cast<unsigned int>(MFD_CLOEXEC))));
    ASSERT_GE(source.get(), 0);

    constexpr std::string_view kOriginal = "abc";
    constexpr std::string_view kOriginalSHA256 =
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad";
    ASSERT_TRUE(WriteAll(
        source.get(), reinterpret_cast<const uint8_t*>(kOriginal.data()),
        kOriginal.size()));

    ScopedFileDescriptor snapshot;
    ASSERT_TRUE(CreateSealedSnapshot(
        source.get(), static_cast<off_t>(kOriginal.size()), kOriginalSHA256,
        &snapshot));

    constexpr char kReplacement = 'z';
    ASSERT_EQ(pwrite(source.get(), &kReplacement, sizeof(kReplacement), 0), 1);

    std::string snapshot_sha256;
    ASSERT_TRUE(ComputeFileSHA256(snapshot.get(), &snapshot_sha256));
    EXPECT_EQ(snapshot_sha256, kOriginalSHA256);

    errno = 0;
    EXPECT_EQ(pwrite(snapshot.get(), &kReplacement, sizeof(kReplacement), 0),
              -1);
    EXPECT_EQ(errno, EPERM);

    constexpr int kRequiredSeals =
        F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL;
    EXPECT_EQ(fcntl(snapshot.get(), F_GET_SEALS) & kRequiredSeals,
              kRequiredSeals);
}

}  // namespace
}  // namespace hazkey::mozc_sidecar_internal
