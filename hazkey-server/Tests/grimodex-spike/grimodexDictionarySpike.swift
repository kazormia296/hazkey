import Foundation
import KanaKanjiConverterModuleWithDefaultDictionary
import XCTest

@testable import hazkey_server

final class GrimodexDictionarySpikeTests: XCTestCase {
  func testFixedEntriesMapTheLinuxReferenceVocabulary() {
    let entries = GrimodexDictionarySpike.fixedEntries

    XCTAssertEqual(entries.count, 2)
    XCTAssertEqual(entries[0].ruby, "セツナ")
    XCTAssertEqual(entries[0].word, "刹那")
    XCTAssertEqual(entries[0].lcid, 1289)
    XCTAssertEqual(entries[0].rcid, 1289)
    XCTAssertEqual(entries[0].mid, 501)
    XCTAssertEqual(entries[0].value(), -5)
    XCTAssertEqual(entries[1].ruby, "リュウセイコウ")
    XCTAssertEqual(entries[1].word, "龍星港")
    XCTAssertEqual(entries[1].lcid, 1293)
    XCTAssertEqual(entries[1].value(), -9)
  }

  func testCategoryAndPriorityMappingIsTableDriven() {
    let cases: [(GrimodexDictionaryCategory, Int, Int, Float)] = [
      (.person, 1, 1289, -8),
      (.person, 2, 1289, -5),
      (.person, 3, 1289, -4),
      (.place, 1, 1293, -9),
      (.place, 2, 1293, -6),
      (.noun, 1, 1288, -9),
      (.noun, 2, 1288, -6),
    ]

    for (category, priority, cid, value) in cases {
      let entry = GrimodexDictionarySpike.map(
        .init(
          yomi: "てすと",
          surface: "試験語",
          category: category,
          priority: priority,
          entryID: "entry-test"
        )
      )
      XCTAssertEqual(entry.lcid, cid)
      XCTAssertEqual(entry.rcid, cid)
      XCTAssertEqual(entry.mid, 501)
      XCTAssertEqual(entry.value(), value)
    }
  }

  func testBenchmarkCorpusCoversEveryRequiredScaleWithUniqueEntries() {
    XCTAssertEqual(GrimodexDictionarySpike.benchmarkCounts, [100, 500, 2_000, 5_000, 10_000])

    for count in GrimodexDictionarySpike.benchmarkCounts {
      let entries = GrimodexDictionarySpike.makeBenchmarkEntries(count: count)
      XCTAssertEqual(entries.count, count)
      XCTAssertEqual(Set(entries.map(\.ruby)).count, count)
      XCTAssertEqual(Set(entries.map(\.word)).count, count)
      XCTAssertEqual(entries[0].ruby, "セツナ")
      XCTAssertEqual(entries[0].word, "刹那")
      XCTAssertEqual(entries[1].ruby, "リュウセイコウ")
      XCTAssertEqual(entries[1].word, "龍星港")
    }
  }

  func testSpikeActivationIsExplicitAndFailClosed() {
    XCTAssertFalse(GrimodexDictionarySpike.isEnabled(environment: [:]))
    XCTAssertFalse(
      GrimodexDictionarySpike.isEnabled(
        environment: ["GRIMODEX_IME_DICTIONARY_SPIKE": "true"]
      )
    )
    XCTAssertTrue(
      GrimodexDictionarySpike.isEnabled(
        environment: ["GRIMODEX_IME_DICTIONARY_SPIKE": "1"]
      )
    )
  }

  func testRequiredBenchmarkScalesRecordRankLatencyAndMemory() throws {
    let converter = KanaKanjiConverter.withDefaultDictionary()
    let options = HazkeyServerConfig().genBaseConvertRequestOptions()

    for count in GrimodexDictionarySpike.benchmarkCounts {
      let report = try XCTUnwrap(
        GrimodexDictionarySpike.runBenchmarkIfConfigured(
          converter: converter,
          options: options,
          environment: ["GRIMODEX_IME_DICTIONARY_BENCHMARK_COUNT": String(count)]
        )
      )
      XCTAssertEqual(report.entryCount, count)
      XCTAssertEqual(report.candidateRank, 1)
      XCTAssertTrue(report.importMilliseconds.isFinite)
      XCTAssertTrue(report.warmP95Milliseconds.isFinite)
      print(
        "GRIMODEX_BENCHMARK entries=\(report.entryCount) "
          + "import_ms=\(report.importMilliseconds) "
          + "warm_p95_ms=\(report.warmP95Milliseconds) "
          + "rss_kib=\(report.residentMemoryKilobytes.map(String.init) ?? \"unavailable\") "
          + "rss_delta_kib=\(report.residentMemoryDeltaKilobytes.map(String.init) ?? \"unavailable\") "
          + "candidate_rank=\(report.candidateRank.map(String.init) ?? \"missing\")"
      )
    }
  }
}
