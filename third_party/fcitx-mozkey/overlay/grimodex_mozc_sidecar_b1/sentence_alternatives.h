// SPDX-License-Identifier: MIT

#ifndef GRIMODEX_MOZC_SIDECAR_B1_SENTENCE_ALTERNATIVES_H_
#define GRIMODEX_MOZC_SIDECAR_B1_SENTENCE_ALTERNATIVES_H_

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

namespace hazkey::mozc_sidecar_b1 {

struct SegmentCandidate {
  std::string value;
  size_t original_index = 0;
};

namespace internal {

inline bool DecodeUtf8(const std::string& input, size_t* offset,
                       uint32_t* code_point) {
  const auto* bytes = reinterpret_cast<const unsigned char*>(input.data());
  const size_t remaining = input.size() - *offset;
  if (remaining == 0) {
    return false;
  }

  const unsigned char first = bytes[*offset];
  size_t width = 0;
  uint32_t value = 0;
  if (first < 0x80) {
    width = 1;
    value = first;
  } else if ((first & 0xe0) == 0xc0) {
    width = 2;
    value = first & 0x1f;
  } else if ((first & 0xf0) == 0xe0) {
    width = 3;
    value = first & 0x0f;
  } else if ((first & 0xf8) == 0xf0) {
    width = 4;
    value = first & 0x07;
  } else {
    return false;
  }
  if (width > remaining) {
    return false;
  }
  for (size_t index = 1; index < width; ++index) {
    const unsigned char next = bytes[*offset + index];
    if ((next & 0xc0) != 0x80) {
      return false;
    }
    value = (value << 6) | (next & 0x3f);
  }
  *offset += width;
  *code_point = value;
  return true;
}

inline void AppendUtf8(uint32_t code_point, std::string* output) {
  if (code_point <= 0x7f) {
    output->push_back(static_cast<char>(code_point));
  } else if (code_point <= 0x7ff) {
    output->push_back(static_cast<char>(0xc0 | (code_point >> 6)));
    output->push_back(static_cast<char>(0x80 | (code_point & 0x3f)));
  } else if (code_point <= 0xffff) {
    output->push_back(static_cast<char>(0xe0 | (code_point >> 12)));
    output->push_back(
        static_cast<char>(0x80 | ((code_point >> 6) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | (code_point & 0x3f)));
  } else {
    output->push_back(static_cast<char>(0xf0 | (code_point >> 18)));
    output->push_back(
        static_cast<char>(0x80 | ((code_point >> 12) & 0x3f)));
    output->push_back(
        static_cast<char>(0x80 | ((code_point >> 6) & 0x3f)));
    output->push_back(static_cast<char>(0x80 | (code_point & 0x3f)));
  }
}

inline uint32_t ComposeJapaneseVoicing(uint32_t base, uint32_t mark) {
  const bool voiced = mark == 0x3099;
  const bool semi_voiced = mark == 0x309a;
  if (!voiced && !semi_voiced) {
    return 0;
  }

  if (voiced && base == 0x3046) return 0x3094;  // ゔ -> ゔ
  if (voiced && base == 0x30a6) return 0x30f4;  // ヴ -> ヴ
  if (voiced && base == 0x309d) return 0x309e;  // ゞ -> ゞ
  if (voiced && base == 0x30fd) return 0x30fe;  // ヾ -> ヾ

  const auto in_every_other = [base](uint32_t first, uint32_t last) {
    return base >= first && base <= last && (base - first) % 2 == 0;
  };
  const bool hiragana_k_or_s = in_every_other(0x304b, 0x3053) ||
                               in_every_other(0x3055, 0x305d);
  const bool hiragana_t = base == 0x305f || base == 0x3061 ||
                          base == 0x3064 || base == 0x3066 ||
                          base == 0x3068;
  const bool katakana_k_or_s = in_every_other(0x30ab, 0x30b3) ||
                               in_every_other(0x30b5, 0x30bd);
  const bool katakana_t = base == 0x30bf || base == 0x30c1 ||
                          base == 0x30c4 || base == 0x30c6 ||
                          base == 0x30c8;
  if (voiced && (hiragana_k_or_s || hiragana_t || katakana_k_or_s ||
                 katakana_t)) {
    return base + 1;
  }
  if (base >= 0x306f && base <= 0x307b && (base - 0x306f) % 3 == 0) {
    return base + (voiced ? 1 : 2);
  }
  if (base >= 0x30cf && base <= 0x30db && (base - 0x30cf) % 3 == 0) {
    return base + (voiced ? 1 : 2);
  }
  if (voiced && base >= 0x30ef && base <= 0x30f2) {
    return base + 8;  // ワ/ヰ/ヱ/ヲ + ゙ -> ヷ/ヸ/ヹ/ヺ
  }
  return 0;
}

// Mozc output is Japanese text. Normalize the canonically equivalent forms
// that can occur in that domain (precomposed kana versus U+3099/U+309A) so
// equivalent candidate surfaces cannot occupy separate beam slots. Invalid
// UTF-8 is returned unchanged; the helper has already validated Mozc input.
inline std::string JapaneseNfcDedupKey(const std::string& input) {
  std::vector<uint32_t> code_points;
  code_points.reserve(input.size());
  size_t offset = 0;
  while (offset < input.size()) {
    uint32_t code_point = 0;
    if (!DecodeUtf8(input, &offset, &code_point)) {
      return input;
    }
    if ((code_point == 0x3099 || code_point == 0x309a) &&
        !code_points.empty()) {
      const uint32_t composed =
          ComposeJapaneseVoicing(code_points.back(), code_point);
      if (composed != 0) {
        code_points.back() = composed;
        continue;
      }
    }
    code_points.push_back(code_point);
  }

  std::string output;
  output.reserve(input.size());
  for (const uint32_t code_point : code_points) {
    AppendUtf8(code_point, &output);
  }
  return output;
}

// One compact link in the layered beam. Candidate values and complete paths
// deliberately stay in the immutable input until final materialization, so a
// long reading cannot make every O(beam_width^2) expansion copy its prefix.
struct BeamLink {
  uint64_t rank_sum = 0;
  size_t previous_index = 0;
  size_t candidate_index = 0;
  size_t stable_order = 0;
};

inline bool LinkLess(const BeamLink& left, const BeamLink& right) {
  if (left.rank_sum != right.rank_sum) {
    return left.rank_sum < right.rank_sum;
  }
  return left.stable_order < right.stable_order;
}

}  // namespace internal

inline std::string CanonicalDedupKey(const std::string& input) {
  return internal::JapaneseNfcDedupKey(input);
}

// Produces at most beam_width deterministic full-sentence alternatives from
// Mozc's natural segments. This is intentionally not described as Mozc's
// global sentence N-best: Segment::Candidate::cost already contains contextual
// terms and cannot be added across newly combined segment choices. Instead,
// the beam minimizes the sum of each segment candidate's original rank and
// uses stable expansion order for ties.
//
// Each layer expands at most beam_width^2 compact links, sorts them, and keeps
// at most beam_width. Temporary storage is O(beam_width^2), retained
// backpointers are O(segment_count * beam_width), and candidate strings are
// copied only while materializing the final beam. This never enumerates the
// full Cartesian product.
inline std::vector<std::string> BuildNaturalSentenceAlternatives(
    const std::vector<std::vector<SegmentCandidate>>& natural_segments,
    size_t beam_width) {
  if (natural_segments.empty() || beam_width == 0) {
    return {};
  }

  std::vector<std::vector<internal::BeamLink>> layers;
  layers.reserve(natural_segments.size());
  for (size_t segment_index = 0; segment_index < natural_segments.size();
       ++segment_index) {
    const auto& source_segment = natural_segments[segment_index];
    if (source_segment.empty()) {
      return {};
    }

    std::vector<size_t> candidate_order(source_segment.size());
    for (size_t index = 0; index < candidate_order.size(); ++index) {
      candidate_order[index] = index;
    }
    std::stable_sort(
        candidate_order.begin(), candidate_order.end(),
        [&source_segment](size_t left, size_t right) {
          if (source_segment[left].original_index !=
              source_segment[right].original_index) {
            return source_segment[left].original_index <
                   source_segment[right].original_index;
          }
          return source_segment[left].value < source_segment[right].value;
        });
    candidate_order.resize(std::min(beam_width, candidate_order.size()));

    const size_t previous_count = layers.empty() ? 1 : layers.back().size();
    std::vector<internal::BeamLink> expanded;
    expanded.reserve(previous_count * candidate_order.size());
    size_t stable_order = 0;
    for (size_t previous_index = 0; previous_index < previous_count;
         ++previous_index) {
      const uint64_t previous_rank_sum =
          layers.empty() ? 0 : layers.back()[previous_index].rank_sum;
      for (const size_t candidate_index : candidate_order) {
        const size_t original_index =
            source_segment[candidate_index].original_index;
        expanded.push_back(internal::BeamLink{
            previous_rank_sum + static_cast<uint64_t>(original_index),
            previous_index,
            candidate_index,
            stable_order++,
        });
      }
    }
    std::stable_sort(expanded.begin(), expanded.end(), internal::LinkLess);
    if (expanded.empty()) {
      return {};
    }
    expanded.resize(std::min(beam_width, expanded.size()));
    layers.push_back(std::move(expanded));
  }

  std::vector<std::string> result;
  result.reserve(layers.back().size());
  std::vector<size_t> path(natural_segments.size());
  for (size_t final_index = 0; final_index < layers.back().size();
       ++final_index) {
    size_t link_index = final_index;
    for (size_t layer_index = layers.size(); layer_index > 0; --layer_index) {
      const internal::BeamLink& link = layers[layer_index - 1][link_index];
      path[layer_index - 1] = link.candidate_index;
      link_index = link.previous_index;
    }
    std::string value;
    for (size_t index = 0; index < natural_segments.size(); ++index) {
      value.append(natural_segments[index][path[index]].value);
    }
    result.push_back(std::move(value));
  }
  return result;
}

inline std::vector<std::string> BuildB1Candidates(
    const std::vector<SegmentCandidate>& forced_candidates,
    const std::vector<std::vector<SegmentCandidate>>& natural_segments,
    size_t max_candidates) {
  if (max_candidates == 0 || forced_candidates.empty()) {
    return {};
  }

  std::vector<std::string> result;
  result.reserve(max_candidates);
  result.push_back(forced_candidates.front().value);
  if (result.size() == max_candidates) {
    return result;
  }
  std::unordered_set<std::string> seen;
  seen.reserve(max_candidates);
  seen.insert(CanonicalDedupKey(forced_candidates.front().value));

  // B1 is a strict Top-K extension whenever the B0 forced list leaves room.
  // Preserve every distinct B0 candidate in its original order before
  // spending remaining response slots on natural sentence alternatives.
  for (size_t index = 1;
       index < forced_candidates.size() && result.size() < max_candidates;
       ++index) {
    const std::string& value = forced_candidates[index].value;
    if (seen.insert(CanonicalDedupKey(value)).second) {
      result.push_back(value);
    }
  }
  if (result.size() == max_candidates) {
    return result;
  }

  // Search more paths than the response can hold so duplicates do not hide
  // lower-ranked unique alternatives. The helper itself bounds
  // max_candidates to 100; cap here too so this utility remains safe in tests.
  const size_t bounded_max = std::min(max_candidates, size_t{100});
  const size_t search_width = std::min(bounded_max * 4, size_t{100});
  std::vector<std::string> natural =
      BuildNaturalSentenceAlternatives(natural_segments, search_width);

  size_t natural_index = 0;
  while (result.size() < max_candidates && natural_index < natural.size()) {
    std::string& value = natural[natural_index++];
    if (seen.insert(CanonicalDedupKey(value)).second) {
      result.push_back(std::move(value));
    }
  }
  return result;
}

}  // namespace hazkey::mozc_sidecar_b1

#endif  // GRIMODEX_MOZC_SIDECAR_B1_SENTENCE_ALTERNATIVES_H_
