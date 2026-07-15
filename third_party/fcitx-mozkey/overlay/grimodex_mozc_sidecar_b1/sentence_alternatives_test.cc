// SPDX-License-Identifier: MIT

#include "grimodex_mozc_sidecar_b1/sentence_alternatives.h"

#include <string>
#include <vector>

#include "testing/gunit.h"

namespace hazkey::mozc_sidecar_b1 {
namespace {

using Segment = std::vector<SegmentCandidate>;

TEST(SentenceAlternativesTest, RanksCompleteSentencesDeterministically) {
  const std::vector<Segment> segments = {
      {{"A", 0}, {"a", 1}},
      {{"B", 0}, {"b", 1}},
  };

  EXPECT_EQ(BuildNaturalSentenceAlternatives(segments, 4),
            (std::vector<std::string>{"AB", "Ab", "aB", "ab"}));
}

TEST(SentenceAlternativesTest, KeepsForcedCandidateFirstAndAppendsNaturalAlternatives) {
  const std::vector<Segment> segments = {
      {{"A", 0}, {"a", 1}},
      {{"B", 0}, {"b", 1}},
  };
  const Segment forced = {{"forced", 0}};

  EXPECT_EQ(BuildB1Candidates(forced, segments, 4),
            (std::vector<std::string>{"forced", "AB", "Ab", "aB"}));
}

TEST(SentenceAlternativesTest, CandidateLimitOneReturnsOnlyForcedCandidate) {
  const std::vector<Segment> segments = {
      {{"natural", 0}},
  };
  const Segment forced = {{"forced", 0}};

  EXPECT_EQ(BuildB1Candidates(forced, segments, 1),
            (std::vector<std::string>{"forced"}));
  EXPECT_TRUE(BuildB1Candidates(forced, segments, 0).empty());
  EXPECT_TRUE(BuildB1Candidates({}, segments, 10).empty());
}

TEST(SentenceAlternativesTest, DoesNotRepeatForcedCanonicalEquivalent) {
  const std::vector<Segment> segments = {
      {{"ガ", 0}, {"か", 1}},
      {{"行", 0}},
  };
  const Segment forced = {{"カ\xE3\x82\x99行", 0}};

  EXPECT_EQ(BuildB1Candidates(forced, segments, 3),
            (std::vector<std::string>{"カ\xE3\x82\x99行", "か行"}));
}

TEST(SentenceAlternativesTest, UsesOriginalRankToOrderEachSegment) {
  const std::vector<Segment> segments = {
      {{"later", 1}, {"first", 0}},
      {{"!", 0}},
  };

  EXPECT_EQ(BuildNaturalSentenceAlternatives(segments, 2),
            (std::vector<std::string>{"first!", "later!"}));
}

TEST(SentenceAlternativesTest, DeduplicatesCanonicalJapaneseVoicing) {
  const std::vector<Segment> segments = {
      {{"カ\xE3\x82\x99", 0}, {"ガ", 1}, {"か", 2}},
      {{"行", 0}},
  };
  const Segment forced = {{"forced", 0}};

  EXPECT_EQ(BuildB1Candidates(forced, segments, 4),
            (std::vector<std::string>{"forced", "カ\xE3\x82\x99行", "か行"}));
}

TEST(SentenceAlternativesTest, RetainsForcedRanksThenAddsUniqueNaturalValues) {
  const Segment forced = {
      {"forced-0", 0}, {"shared", 1}, {"forced-2", 2},
  };
  const std::vector<Segment> segments = {
      {{"shared", 0}, {"natural-1", 1}, {"natural-2", 2}},
  };

  EXPECT_EQ(BuildB1Candidates(forced, segments, 5),
            (std::vector<std::string>{
                "forced-0", "shared", "forced-2", "natural-1", "natural-2"}));
}

TEST(SentenceAlternativesTest, SearchesPastCanonicalDuplicatesToFillWindow) {
  const Segment forced = {{"forced", 0}};
  const std::vector<Segment> segments = {
      {{"カ\xE3\x82\x99", 0}, {"ガ", 1}, {"か", 2}, {"き", 3}},
      {{"行", 0}},
  };

  EXPECT_EQ(BuildB1Candidates(forced, segments, 4),
            (std::vector<std::string>{
                "forced", "カ\xE3\x82\x99行", "か行", "き行"}));
}

TEST(SentenceAlternativesTest, ComposesExplicitTaRowButNotSmallTsu) {
  EXPECT_EQ(CanonicalDedupKey("つ\xE3\x82\x99"), "づ");
  EXPECT_EQ(CanonicalDedupKey("ツ\xE3\x82\x99"), "ヅ");
  EXPECT_EQ(CanonicalDedupKey("っ\xE3\x82\x99"),
            "っ\xE3\x82\x99");
  EXPECT_EQ(CanonicalDedupKey("ッ\xE3\x82\x99"),
            "ッ\xE3\x82\x99");
}

TEST(SentenceAlternativesTest, ReturnsNoSentenceForAnEmptyNaturalSegment) {
  const std::vector<Segment> segments = {
      {{"A", 0}},
      {},
  };

  EXPECT_TRUE(BuildNaturalSentenceAlternatives(segments, 10).empty());
}

TEST(SentenceAlternativesTest, BeamWidthBoundsEveryStage) {
  std::vector<Segment> segments(64);
  for (auto& segment : segments) {
    for (size_t index = 0; index < 100; ++index) {
      segment.push_back(
          {std::to_string(index) + "/", index});
    }
  }

  const std::vector<std::string> result =
      BuildNaturalSentenceAlternatives(segments, 5);
  ASSERT_EQ(result.size(), 5);
  std::string expected_first;
  for (size_t index = 0; index < segments.size(); ++index) {
    expected_first.append("0/");
  }
  EXPECT_EQ(result.front(), expected_first);
}

}  // namespace
}  // namespace hazkey::mozc_sidecar_b1
