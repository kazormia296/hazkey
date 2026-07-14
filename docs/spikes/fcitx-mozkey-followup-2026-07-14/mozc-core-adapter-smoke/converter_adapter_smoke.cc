#include <array>
#include <cstddef>
#include <iostream>
#include <memory>
#include <string>

#include "absl/flags/flag.h"
#include "absl/status/statusor.h"
#include "base/file/temp_dir.h"
#include "base/init_mozc.h"
#include "base/system_util.h"
#include "converter/converter_interface.h"
#include "converter/segments.h"
#include "engine/engine.h"
#include "engine/eval_engine_factory.h"
#include "request/conversion_request.h"

ABSL_FLAG(std::string, data_file, "", "Path to mozc.data");

int main(int argc, char **argv) {
  mozc::InitMozc(argv[0], &argc, &argv);
  auto profile = mozc::TempDirectory::Default().CreateTempDirectory();
  if (!profile.ok()) {
    std::cerr << profile.status() << "\n";
    return 1;
  }
  mozc::SystemUtil::SetUserProfileDirectory(profile->path());

  auto engine = mozc::CreateEvalEngine(absl::GetFlag(FLAGS_data_file), "oss");
  if (!engine.ok()) {
    std::cerr << engine.status() << "\n";
    return 2;
  }
  const std::shared_ptr<const mozc::ConverterInterface> converter =
      engine.value()->GetConverter();

  mozc::ConversionRequest::Options options;
  options.request_type = mozc::ConversionRequest::CONVERSION;
  options.max_conversion_candidates_size = 10;
  options.enable_user_history_for_conversion = false;
  options.incognito_mode = true;
  const mozc::ConversionRequest request =
      mozc::ConversionRequestBuilder()
          .SetOptions(options)
          .SetKey("きょうはいしゃにいく")
          .Build();

  mozc::Segments segments;
  if (!converter->StartConversion(request, &segments) ||
      segments.conversion_segments_size() == 0) {
    std::cerr << "StartConversion produced no segments\n";
    return 3;
  }

  const mozc::Segment &first = segments.conversion_segment(0);
  if (first.candidates_size() == 0) {
    std::cerr << "first segment produced no candidates\n";
    return 4;
  }
  std::cout << "segments=" << segments.conversion_segments_size()
            << " first_key=" << first.key()
            << " first_value=" << first.candidate(0).value << "\n";

  if (segments.conversion_segments_size() > 1) {
    const bool resized = converter->ResizeSegment(&segments, request, 0, 1);
    std::cout << "resize_plus_one=" << resized << "\n";
  }

  const std::array<size_t, 1> selected = {0};
  if (!converter->CommitSegments(&segments, selected)) {
    std::cerr << "CommitSegments failed\n";
    return 5;
  }
  converter->CancelConversion(&segments);
  if (segments.conversion_segments_size() != 0) {
    std::cerr << "CancelConversion retained conversion segments\n";
    return 6;
  }
  std::cout << "learning=off partial_commit_api=ok cancel=ok\n";
  return 0;
}
