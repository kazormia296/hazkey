// SPDX-License-Identifier: MIT

#include <algorithm>
#include <cerrno>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <iostream>
#include <limits>
#include <memory>
#include <string>
#include <utility>

#include <fcntl.h>
#include <unistd.h>

#include "absl/flags/flag.h"
#include "absl/status/statusor.h"
#include "base/file/temp_dir.h"
#include "base/init_mozc.h"
#include "base/system_util.h"
#include "base/util.h"
#include "converter/converter_interface.h"
#include "converter/segments.h"
#include "engine/engine.h"
#include "engine/eval_engine_factory.h"
#include "grimodex_mozc_sidecar/mozc_sidecar.pb.h"
#include "grimodex_mozc_sidecar/sha256.h"
#include "request/conversion_request.h"

ABSL_FLAG(std::string, data_file, "", "Path to the OSS Mozc dataset");
ABSL_FLAG(std::string, dataset_sha256, "",
          "Expected fixed B0 dataset SHA-256 identity");

namespace {

using SidecarRequest = ::hazkey::mozc_sidecar::Request;
using SidecarResponse = ::hazkey::mozc_sidecar::Response;
using ::hazkey::mozc_sidecar_internal::CreateSealedSnapshot;
using ::hazkey::mozc_sidecar_internal::ScopedFileDescriptor;

constexpr uint32_t kProtocolVersion = 1;
constexpr uint32_t kMaximumFrameSize = 4U * 1024U * 1024U;
constexpr uint32_t kDefaultMaxCandidates = 20;
constexpr uint32_t kMaximumMaxCandidates = 100;
constexpr char kFixedB0DatasetSHA256[] =
    "b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e";
constexpr off_t kFixedB0DatasetSize = 18'887'468;
// ImmutableConverter rejects conversion keys at or above 1024 bytes.
constexpr size_t kMaximumReadingBytes = 1023;
constexpr size_t kMaximumResizableSegmentKeySize =
    std::numeric_limits<uint8_t>::max();

enum class FrameReadResult {
    kOk,
    kEof,
    kError,
};

// Wipes request and response payload buffers before reusing their allocation.
// This is best-effort process-local hygiene, not a replacement for ensuring
// that secure-input text is never sent to this helper.
void ClearPayload(std::string* value) {
    std::fill(value->begin(), value->end(), '\0');
    value->clear();
}

void ClearResponsePayload(SidecarResponse* response) {
    ClearPayload(response->mutable_error_message());
    for (auto& candidate : *response->mutable_candidates()) {
        ClearPayload(candidate.mutable_value());
        ClearPayload(candidate.mutable_description());
    }
    response->Clear();
}

FrameReadResult ReadExactly(int fd, void* destination, size_t size) {
    auto* output = static_cast<uint8_t*>(destination);
    size_t offset = 0;
    while (offset < size) {
        const ssize_t bytes_read = read(fd, output + offset, size - offset);
        if (bytes_read > 0) {
            offset += static_cast<size_t>(bytes_read);
            continue;
        }
        if (bytes_read == 0) {
            return offset == 0 ? FrameReadResult::kEof
                               : FrameReadResult::kError;
        }
        if (errno == EINTR) {
            continue;
        }
        return FrameReadResult::kError;
    }
    return FrameReadResult::kOk;
}

bool WriteExactly(int fd, const void* source, size_t size) {
    const auto* input = static_cast<const uint8_t*>(source);
    size_t offset = 0;
    while (offset < size) {
        const ssize_t bytes_written = write(fd, input + offset, size - offset);
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

FrameReadResult ReadFrame(std::string* payload, std::string* error_message) {
    uint8_t header[4] = {};
    const FrameReadResult header_result =
        ReadExactly(STDIN_FILENO, header, sizeof(header));
    if (header_result != FrameReadResult::kOk) {
        if (header_result == FrameReadResult::kError) {
            *error_message = "truncated or unreadable frame header";
        }
        return header_result;
    }

    const uint32_t payload_size =
        (static_cast<uint32_t>(header[0]) << 24) |
        (static_cast<uint32_t>(header[1]) << 16) |
        (static_cast<uint32_t>(header[2]) << 8) |
        static_cast<uint32_t>(header[3]);
    if (payload_size > kMaximumFrameSize) {
        *error_message = "frame exceeds the 4 MiB limit";
        return FrameReadResult::kError;
    }

    payload->resize(payload_size);
    if (payload_size == 0) {
        return FrameReadResult::kOk;
    }
    const FrameReadResult payload_result =
        ReadExactly(STDIN_FILENO, payload->data(), payload->size());
    if (payload_result != FrameReadResult::kOk) {
        *error_message = "truncated or unreadable frame payload";
        return FrameReadResult::kError;
    }
    return FrameReadResult::kOk;
}

bool WriteFrame(const SidecarResponse& response) {
    std::string payload;
    if (!response.SerializeToString(&payload) ||
        payload.size() > kMaximumFrameSize) {
        ClearPayload(&payload);
        return false;
    }

    const uint32_t payload_size = static_cast<uint32_t>(payload.size());
    const uint8_t header[4] = {
        static_cast<uint8_t>((payload_size >> 24) & 0xff),
        static_cast<uint8_t>((payload_size >> 16) & 0xff),
        static_cast<uint8_t>((payload_size >> 8) & 0xff),
        static_cast<uint8_t>(payload_size & 0xff),
    };
    const bool success = WriteExactly(STDOUT_FILENO, header, sizeof(header)) &&
                         WriteExactly(STDOUT_FILENO, payload.data(),
                                      payload.size());
    ClearPayload(&payload);
    return success;
}

SidecarResponse MakeResponse(uint64_t request_id,
                             const std::string& dataset_sha256) {
    SidecarResponse response;
    response.set_protocol_version(kProtocolVersion);
    response.set_request_id(request_id);
    response.set_dataset_sha256(dataset_sha256);
    return response;
}

SidecarResponse MakeErrorResponse(uint64_t request_id,
                                  const std::string& dataset_sha256,
                                  const char* message) {
    SidecarResponse response = MakeResponse(request_id, dataset_sha256);
    response.set_ok(false);
    response.set_error_message(message);
    return response;
}

class ScopedConversionCancellation final {
  public:
    ScopedConversionCancellation(const mozc::ConverterInterface* converter,
                                 mozc::Segments* segments)
        : converter_(converter), segments_(segments) {}

    ScopedConversionCancellation(const ScopedConversionCancellation&) =
        delete;
    ScopedConversionCancellation& operator=(
        const ScopedConversionCancellation&) = delete;

    ~ScopedConversionCancellation() { converter_->CancelConversion(segments_); }

  private:
    const mozc::ConverterInterface* converter_;
    mozc::Segments* segments_;
};

SidecarResponse Convert(const SidecarRequest& sidecar_request,
                        const mozc::ConverterInterface& converter,
                        const std::string& dataset_sha256) {
    if (sidecar_request.reading().empty()) {
        return MakeErrorResponse(sidecar_request.request_id(), dataset_sha256,
                                 "reading must not be empty");
    }
    if (sidecar_request.reading().size() > kMaximumReadingBytes ||
        !mozc::Util::IsValidUtf8(sidecar_request.reading())) {
        return MakeErrorResponse(sidecar_request.request_id(), dataset_sha256,
                                 "reading is invalid or too long");
    }
    const size_t reading_size =
        mozc::Util::CharsLen(sidecar_request.reading());
    if (reading_size > kMaximumResizableSegmentKeySize) {
        return MakeErrorResponse(sidecar_request.request_id(), dataset_sha256,
                                 "reading is invalid or too long");
    }
    if (sidecar_request.target_key_size() > reading_size ||
        sidecar_request.target_key_size() >
            kMaximumResizableSegmentKeySize) {
        return MakeErrorResponse(sidecar_request.request_id(), dataset_sha256,
                                 "target key size is out of range");
    }

    const uint32_t requested_max_candidates =
        sidecar_request.max_candidates() == 0
            ? kDefaultMaxCandidates
            : sidecar_request.max_candidates();
    const int max_candidates = static_cast<int>(
        std::min(requested_max_candidates, kMaximumMaxCandidates));

    mozc::ConversionRequest::Options options;
    options.request_type = mozc::ConversionRequest::CONVERSION;
    options.max_conversion_candidates_size = max_candidates;
    options.enable_user_history_for_conversion = false;
    options.incognito_mode = true;
    const mozc::ConversionRequest conversion_request =
        mozc::ConversionRequestBuilder()
            .SetOptions(options)
            .SetKey(sidecar_request.reading())
            .Build();

    mozc::Segments segments;
    if (!converter.StartConversion(conversion_request, &segments)) {
        return MakeErrorResponse(sidecar_request.request_id(), dataset_sha256,
                                 "Mozc conversion failed");
    }
    ScopedConversionCancellation cancellation(&converter, &segments);
    if (segments.conversion_segments_size() == 0) {
        return MakeErrorResponse(sidecar_request.request_id(), dataset_sha256,
                                 "Mozc returned no conversion segments");
    }

    if (sidecar_request.target_key_size() != 0) {
        const int64_t offset =
            static_cast<int64_t>(sidecar_request.target_key_size()) -
            static_cast<int64_t>(segments.conversion_segment(0).key_len());
        if (offset < std::numeric_limits<int>::min() ||
            offset > std::numeric_limits<int>::max()) {
            return MakeErrorResponse(sidecar_request.request_id(),
                                     dataset_sha256,
                                     "target key size is out of range");
        }
        if (offset != 0 &&
            !converter.ResizeSegment(&segments, conversion_request, 0,
                                     static_cast<int>(offset))) {
            return MakeErrorResponse(sidecar_request.request_id(),
                                     dataset_sha256,
                                     "Mozc could not resize the first segment");
        }
        if (segments.conversion_segments_size() == 0) {
            return MakeErrorResponse(sidecar_request.request_id(),
                                     dataset_sha256,
                                     "Mozc returned no segment after resize");
        }
    }

    const mozc::Segment& segment = segments.conversion_segment(0);
    if (segment.key_len() > std::numeric_limits<uint32_t>::max()) {
        return MakeErrorResponse(sidecar_request.request_id(), dataset_sha256,
                                 "segment key size is out of range");
    }

    SidecarResponse response =
        MakeResponse(sidecar_request.request_id(), dataset_sha256);
    response.set_ok(true);
    const uint32_t segment_key_size = static_cast<uint32_t>(segment.key_len());
    response.set_segment_key_size(segment_key_size);
    const size_t candidate_count = std::min(
        segment.candidates_size(), static_cast<size_t>(max_candidates));
    for (size_t index = 0; index < candidate_count; ++index) {
        const mozc::Segment::Candidate& source =
            segment.candidate(static_cast<int>(index));
        ::hazkey::mozc_sidecar::Candidate* candidate =
            response.add_candidates();
        candidate->set_value(source.value);
        candidate->set_description(source.description);
        candidate->set_consumed_key_size(segment_key_size);
    }
    return response;
}

SidecarResponse HandleRequest(const SidecarRequest& request,
                              const mozc::ConverterInterface& converter,
                              const std::string& dataset_sha256) {
    if (request.protocol_version() != kProtocolVersion) {
        return MakeErrorResponse(request.request_id(), dataset_sha256,
                                 "unsupported protocol version");
    }

    switch (request.operation()) {
        case ::hazkey::mozc_sidecar::OPERATION_PING: {
            SidecarResponse response =
                MakeResponse(request.request_id(), dataset_sha256);
            response.set_ok(true);
            return response;
        }
        case ::hazkey::mozc_sidecar::OPERATION_CONVERT:
            return Convert(request, converter, dataset_sha256);
        case ::hazkey::mozc_sidecar::OPERATION_UNSPECIFIED:
        default:
            return MakeErrorResponse(request.request_id(), dataset_sha256,
                                     "unsupported operation");
    }
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGPIPE, SIG_IGN);

    // InitMozc installs a process-lifetime log sink, so isolate the profile
    // before initialization to keep even debug logs out of the real profile.
    absl::StatusOr<mozc::TempDirectory> profile =
        mozc::TempDirectory::Default().CreateTempDirectory();
    if (!profile.ok()) {
        std::cerr << "could not create an isolated Mozc profile\n";
        return 1;
    }
    mozc::SystemUtil::SetUserProfileDirectory(profile->path());
    mozc::InitMozc(argv[0], &argc, &argv);

    const std::string data_file = absl::GetFlag(FLAGS_data_file);
    if (data_file.empty()) {
        std::cerr << "--data_file is required\n";
        return 1;
    }
    if (absl::GetFlag(FLAGS_dataset_sha256) != kFixedB0DatasetSHA256) {
        std::cerr << "fixed B0 dataset identity is required\n";
        return 1;
    }

    ScopedFileDescriptor source_dataset_descriptor(
        open(data_file.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (source_dataset_descriptor.get() < 0) {
        std::cerr << "fixed B0 dataset file is unavailable\n";
        return 1;
    }

    // Copy, authenticate, and seal the exact B0 bytes before Mozc maps them.
    // The original path and inode can then change without affecting the engine.
    ScopedFileDescriptor verified_dataset_descriptor;
    if (!CreateSealedSnapshot(source_dataset_descriptor.get(),
                              kFixedB0DatasetSize, kFixedB0DatasetSHA256,
                              &verified_dataset_descriptor)) {
        std::cerr << "fixed B0 dataset verification failed\n";
        return 1;
    }

    // Keep the sealed descriptor alive for the lifetime of Mozc's mmap.
    const std::string verified_data_file =
        "/proc/self/fd/" +
        std::to_string(verified_dataset_descriptor.get());
    absl::StatusOr<std::unique_ptr<mozc::Engine>> engine =
        mozc::CreateEvalEngine(verified_data_file, "oss");
    if (!engine.ok()) {
        std::cerr << "could not initialize the Mozc evaluation engine\n";
        return 1;
    }
    const std::shared_ptr<const mozc::ConverterInterface> converter =
        engine.value()->GetConverter();
    if (converter == nullptr) {
        std::cerr << "Mozc converter is unavailable\n";
        return 1;
    }

    const std::string dataset_sha256 = kFixedB0DatasetSHA256;
    for (;;) {
        std::string payload;
        std::string frame_error;
        const FrameReadResult read_result = ReadFrame(&payload, &frame_error);
        if (read_result == FrameReadResult::kEof) {
            return 0;
        }
        if (read_result == FrameReadResult::kError) {
            ClearPayload(&payload);
            const SidecarResponse response =
                MakeErrorResponse(0, dataset_sha256, frame_error.c_str());
            WriteFrame(response);
            return 2;
        }

        SidecarRequest request;
        if (!request.ParseFromString(payload)) {
            ClearPayload(&payload);
            ClearPayload(request.mutable_reading());
            request.Clear();
            const SidecarResponse response = MakeErrorResponse(
                0, dataset_sha256, "request protobuf could not be parsed");
            if (!WriteFrame(response)) {
                return 3;
            }
            continue;
        }
        ClearPayload(&payload);

        SidecarResponse response =
            HandleRequest(request, *converter, dataset_sha256);
        ClearPayload(request.mutable_reading());
        request.Clear();
        if (!WriteFrame(response)) {
            ClearResponsePayload(&response);
            return 3;
        }
        ClearResponsePayload(&response);
    }
}
