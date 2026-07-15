import Foundation
import XCTest

#if canImport(Glibc)
import Glibc
#endif

@testable import hazkey_server

/// Opt-in product-path measurement for the experimental H0 hybrid.
///
/// This deliberately remains separate from the offline H1 quality evaluator:
/// it launches the real server and sidecar, times Protocol v2 round trips, and
/// observes process memory and candidate-window stability.
final class GrimodexHybridProcessSpikeTests: XCTestCase {
  private struct Latency: Encodable {
    let median: Double
    let p95: Double
    let minimum: Double
    let maximum: Double
    let samples: [Double]
  }

  private struct Memory: Encodable {
    let rssKiB: Int?
    let pssKiB: Int?

    enum CodingKeys: String, CodingKey {
      case rssKiB = "rss_kib"
      case pssKiB = "pss_kib"
    }
  }

  private struct Scenario: Encodable {
    let prefetchDelayMilliseconds: Int
    let cases: Int
    let iterationsPerCase: Int
    let mozcFirstDisplayRoundTripMilliseconds: Latency
    let formalConversionRoundTripMilliseconds: Latency
    let candidateAugmentationCount: Int
    let mozcTop1ChangeCount: Int
    let candidateJumpViolations: Int

    enum CodingKeys: String, CodingKey {
      case prefetchDelayMilliseconds = "prefetch_delay_ms"
      case cases
      case iterationsPerCase = "iterations_per_case"
      case mozcFirstDisplayRoundTripMilliseconds =
        "mozc_first_display_round_trip_ms"
      case formalConversionRoundTripMilliseconds =
        "formal_conversion_round_trip_ms"
      case candidateAugmentationCount = "candidate_augmentation_count"
      case mozcTop1ChangeCount = "mozc_top1_change_count"
      case candidateJumpViolations = "candidate_jump_violations"
    }
  }

  private struct Report: Encodable {
    let schema = "hazkey.mozc-hybrid-runtime-spike.v1"
    let runtimePolicy = "preserve-mozc-top1-h0"
    let diagnosticOnly = true
    let timingBoundary = "one_protocol_v2_round_trip"
    let memorySampling =
      "before_and_after_endpoint_snapshots_not_peak_or_simultaneous"
    let diagnosticsScope =
      "sum_of_per_measurement_quiesced_deltas_excluding_warmup_and_cleanup"
    let zenzaiEnabled: Bool
    let zenzaiModelAvailable: Bool
    let corpusCases: Int
    let scenarios: [Scenario]
    let serverMemoryBefore: Memory
    let serverMemoryAfter: Memory
    let helperMemoryBefore: Memory
    let helperMemoryAfter: Memory
    let maxObservedEndpointTotalPssKiB: Int?
    let runtimeDiagnostics: [String: String]

    enum CodingKeys: String, CodingKey {
      case schema
      case runtimePolicy = "runtime_policy"
      case diagnosticOnly = "diagnostic_only"
      case timingBoundary = "timing_boundary"
      case memorySampling = "memory_sampling"
      case diagnosticsScope = "diagnostics_scope"
      case zenzaiEnabled = "zenzai_enabled"
      case zenzaiModelAvailable = "zenzai_model_available"
      case corpusCases = "corpus_cases"
      case scenarios
      case serverMemoryBefore = "server_memory_before"
      case serverMemoryAfter = "server_memory_after"
      case helperMemoryBefore = "helper_memory_before"
      case helperMemoryAfter = "helper_memory_after"
      case maxObservedEndpointTotalPssKiB =
        "max_observed_endpoint_total_pss_kib"
      case runtimeDiagnostics = "runtime_diagnostics"
    }
  }

  func testMozcFirstHybridProductPathMetrics() throws {
#if canImport(Glibc)
    let environment = ProcessInfo.processInfo.environment
    guard let serverPath = nonempty(environment["GRIMODEX_HYBRID_SPIKE_SERVER"]),
          let helperPath = nonempty(environment["GRIMODEX_HYBRID_SPIKE_MOZC_HELPER"]),
          let dataPath = nonempty(environment["GRIMODEX_HYBRID_SPIKE_MOZC_DATA"]) else {
      throw XCTSkip(
        "Set GRIMODEX_HYBRID_SPIKE_SERVER, GRIMODEX_HYBRID_SPIKE_MOZC_HELPER, "
          + "and GRIMODEX_HYBRID_SPIKE_MOZC_DATA to run the hybrid product probe"
      )
    }
    let caseLimit = try integer(
      environment["GRIMODEX_HYBRID_SPIKE_CASE_LIMIT"],
      defaultValue: 12,
      minimum: 1,
      name: "GRIMODEX_HYBRID_SPIKE_CASE_LIMIT"
    )
    let iterations = try integer(
      environment["GRIMODEX_HYBRID_SPIKE_ITERATIONS"],
      defaultValue: 1,
      minimum: 1,
      name: "GRIMODEX_HYBRID_SPIKE_ITERATIONS"
    )
    let delays = try delayValues(
      environment["GRIMODEX_HYBRID_SPIKE_PREFETCH_DELAYS_MS"] ?? "0,25,100"
    )
    let zenzaiEnabled = try boolean(
      environment["GRIMODEX_HYBRID_SPIKE_ZENZAI_ENABLED"],
      defaultValue: false,
      name: "GRIMODEX_HYBRID_SPIKE_ZENZAI_ENABLED"
    )
    let serverURL = URL(fileURLWithPath: serverPath)
    let helperURL = URL(fileURLWithPath: helperPath)
    let dataURL = URL(fileURLWithPath: dataPath)
    let corpusURL = try XCTUnwrap(Bundle.module.resourceURL)
      .appendingPathComponent("Fixtures/ime-base-ab-v1/conversion-quality-v1.tsv")
    let cases = Array(try ABProbeCorpus.load(path: corpusURL.path).prefix(caseLimit))
    let fixture = try GrimodexProcessSnapshotFixture()
    defer { fixture.remove() }
    let dictionary = try dictionaryURL(environment: environment)

    let mozcBaseline = try collectMozcBaseline(
      serverURL: serverURL,
      helperURL: helperURL,
      dataURL: dataURL,
      fixture: fixture,
      dictionaryURL: dictionary,
      cases: cases
    )

    let server = GrimodexProcessHarness(
      executableURL: serverURL,
      grimodexRootURL: fixture.rootURL,
      converterConfiguration: .mozcHybrid(
        helperURL: helperURL,
        dataURL: dataURL
      ),
      dictionaryURL: dictionary
    )
    try server.start()
    defer { server.stop() }
    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }
    try client.configureHybridSpikeProfile(zenzaiEnabled: zenzaiEnabled)
    let zenzaiModelAvailable = try client.zenzaiModelAvailable()
    if zenzaiEnabled && !zenzaiModelAvailable {
      throw GrimodexProcessE2EError.invalidResponse(
        "Zenzai-enabled hybrid measurement requires an available model"
      )
    }
    let session = try client.openSessionInfo(program: "mozc-hybrid-runtime-spike")

    // Warm the fixed helper and both converter instances before endpoint
    // memory snapshots. The published scenarios remain separately counted.
    let warm = try XCTUnwrap(cases.first)
    try client.insertText(warm.reading, sessionID: session.sessionID)
    usleep(100_000)
    _ = try client.startConversion(sessionID: session.sessionID)
    try client.resetComposition(sessionID: session.sessionID)
    _ = try waitForHybridQuiescence(client: client, server: server)

    let serverPID = try XCTUnwrap(server.processIdentifier)
    let helperPID = try XCTUnwrap(server.childProcessIdentifiers().first)
    let serverBefore = processMemory(serverPID)
    let helperBefore = processMemory(helperPID)
    var scenarios: [Scenario] = []
    var measuredDiagnostics: [String: UInt64] = [:]
    var measuredInvalidations: [String: UInt64] = [:]
    for delay in delays {
      var firstDisplaySamples: [Double] = []
      var formalSamples: [Double] = []
      var augmentationCount = 0
      var top1Changes = 0
      var jumpViolations = 0
      for testCase in cases {
        let baseline = mozcBaseline[testCase.id] ?? []
        for _ in 0..<iterations {
          try client.resetComposition(sessionID: session.sessionID)
          let diagnosticsBefore = try waitForHybridQuiescence(
            client: client,
            server: server
          )
          let displayStarted = DispatchTime.now().uptimeNanoseconds
          let display = try client.insertTextSnapshot(
            testCase.reading,
            sessionID: session.sessionID
          )
          firstDisplaySamples.append(milliseconds(since: displayStarted))
          guard !display.candidateWindow.items.isEmpty else {
            throw GrimodexProcessE2EError.invalidResponse(
              "hybrid insert did not synchronously publish Mozc candidates"
            )
          }
          if delay > 0 { usleep(useconds_t(delay * 1_000)) }
          let formalStarted = DispatchTime.now().uptimeNanoseconds
          let formal = try client.startConversionSnapshot(
            sessionID: session.sessionID
          )
          formalSamples.append(milliseconds(since: formalStarted))
          let hybridCandidates = formal.candidateWindow.items.map(\.text)
          let baselineSurfaces = Set(baseline.map {
            $0.precomposedStringWithCanonicalMapping
          })
          let addsUniqueSurface = hybridCandidates.contains {
            !baselineSurfaces.contains($0.precomposedStringWithCanonicalMapping)
          }
          augmentationCount += addsUniqueSurface ? 1 : 0
          if hybridCandidates.first != baseline.first { top1Changes += 1 }

          let generation = formal.candidateWindow.generation
          let items = formal.candidateWindow.items
          usleep(50_000)
          let navigated = try client.navigateCandidateSnapshot(
            0,
            sessionID: session.sessionID
          )
          if navigated.candidateWindow.generation != generation
              || navigated.candidateWindow.items != items {
            jumpViolations += 1
          }
          let diagnosticsAfter = try waitForHybridQuiescence(
            client: client,
            server: server
          )
          try accumulateDiagnosticsDelta(
            before: diagnosticsBefore,
            after: diagnosticsAfter,
            counters: &measuredDiagnostics,
            invalidations: &measuredInvalidations
          )
        }
      }
      scenarios.append(Scenario(
        prefetchDelayMilliseconds: delay,
        cases: cases.count,
        iterationsPerCase: iterations,
        mozcFirstDisplayRoundTripMilliseconds: latency(firstDisplaySamples),
        formalConversionRoundTripMilliseconds: latency(formalSamples),
        candidateAugmentationCount: augmentationCount,
        mozcTop1ChangeCount: top1Changes,
        candidateJumpViolations: jumpViolations
      ))
    }
    measuredDiagnostics["outstanding_work"] = 0
    if !measuredInvalidations.isEmpty {
      measuredDiagnostics.removeValue(forKey: "invalidations")
    }
    var diagnostics = measuredDiagnostics.mapValues(String.init)
    diagnostics["invalidations"] = measuredInvalidations
      .map { "\($0.key)=\($0.value)" }
      .sorted()
      .joined(separator: ",")
    let serverAfter = processMemory(serverPID)
    let helperAfter = processMemory(helperPID)
    let maxPss = [
      totalPss(serverBefore, helperBefore),
      totalPss(serverAfter, helperAfter),
    ].compactMap { $0 }.max()
    let report = Report(
      zenzaiEnabled: zenzaiEnabled,
      zenzaiModelAvailable: zenzaiModelAvailable,
      corpusCases: cases.count,
      scenarios: scenarios,
      serverMemoryBefore: serverBefore,
      serverMemoryAfter: serverAfter,
      helperMemoryBefore: helperBefore,
      helperMemoryAfter: helperAfter,
      maxObservedEndpointTotalPssKiB: maxPss,
      runtimeDiagnostics: diagnostics
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    var output = try encoder.encode(report)
    output.append(0x0A)
    if let outputPath = nonempty(environment["GRIMODEX_HYBRID_SPIKE_OUTPUT"]) {
      try output.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
    } else {
      FileHandle.standardError.write(output)
    }

    XCTAssertTrue(scenarios.allSatisfy { $0.mozcTop1ChangeCount == 0 })
    XCTAssertTrue(scenarios.allSatisfy { $0.candidateJumpViolations == 0 })
    XCTAssertFalse(diagnostics.isEmpty)
#else
    throw XCTSkip("Hybrid process measurements require Linux /proc")
#endif
  }

#if canImport(Glibc)
  private func collectMozcBaseline(
    serverURL: URL,
    helperURL: URL,
    dataURL: URL,
    fixture: GrimodexProcessSnapshotFixture,
    dictionaryURL: URL,
    cases: [ABProbeCorpusCase]
  ) throws -> [String: [String]] {
    let server = GrimodexProcessHarness(
      executableURL: serverURL,
      grimodexRootURL: fixture.rootURL,
      converterConfiguration: .mozc(helperURL: helperURL, dataURL: dataURL),
      dictionaryURL: dictionaryURL
    )
    try server.start()
    defer { server.stop() }
    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }
    try client.configureBenchmarkProfile()
    let session = try client.openSessionInfo(program: "mozc-hybrid-baseline")
    var result: [String: [String]] = [:]
    for testCase in cases {
      try client.resetComposition(sessionID: session.sessionID)
      try client.insertText(testCase.reading, sessionID: session.sessionID)
      result[testCase.id] = try client.startConversion(sessionID: session.sessionID)
    }
    return result
  }

  private func latency(_ samples: [Double]) -> Latency {
    let summary = ABProbeLatency.summarize(samples)
    return Latency(
      median: summary.median,
      p95: summary.p95,
      minimum: summary.minimum,
      maximum: summary.maximum,
      samples: samples
    )
  }

  private func milliseconds(since started: UInt64) -> Double {
    let now = DispatchTime.now().uptimeNanoseconds
    return Double(now >= started ? now - started : 0) / 1_000_000
  }

  private func processMemory(_ pid: Int32) -> Memory {
    Memory(
      rssKiB: memoryValue("/proc/\(pid)/status", field: "VmRSS:"),
      pssKiB: memoryValue("/proc/\(pid)/smaps_rollup", field: "Pss:")
    )
  }

  private func memoryValue(_ path: String, field: String) -> Int? {
    guard let contents = try? String(contentsOfFile: path, encoding: .utf8),
          let line = contents.split(separator: "\n").first(where: {
            $0.hasPrefix(field)
          }) else { return nil }
    return line.split(whereSeparator: \.isWhitespace).dropFirst().first.flatMap {
      Int($0)
    }
  }

  private func totalPss(_ server: Memory, _ helper: Memory) -> Int? {
    guard let serverPss = server.pssKiB, let helperPss = helper.pssKiB else {
      return nil
    }
    return serverPss + helperPss
  }

  private func parseDiagnostics(_ log: String) -> [String: String] {
    guard let line = log.split(whereSeparator: \.isNewline).last(where: {
      $0.contains("MOZC_HYBRID_DIAGNOSTICS ")
    }), let marker = line.range(of: "MOZC_HYBRID_DIAGNOSTICS ") else {
      return [:]
    }
    let diagnostics = Dictionary(uniqueKeysWithValues: line[marker.upperBound...]
      .split(separator: " ")
      .compactMap { field -> (String, String)? in
        guard let separator = field.firstIndex(of: "=") else { return nil }
        return (
          String(field[..<separator]),
          String(field[field.index(after: separator)...])
        )
      })
    guard diagnostics["outstanding_work"] != nil,
          diagnostics["invalidations"] != nil else {
      // The server may still be appending a single UTF-8 log record while the
      // probe samples it. Ignore that incomplete tail and retry.
      return [:]
    }
    return diagnostics
  }

  private func waitForHybridQuiescence(
    client: GrimodexProcessClient,
    server: GrimodexProcessHarness,
    timeout: TimeInterval = 10
  ) throws -> [String: String] {
    let deadline = Date().addingTimeInterval(timeout)
    repeat {
      try client.flushHybridDiagnosticsToServerLog()
      let diagnostics = parseDiagnostics(try server.logTailContents())
      if diagnostics["outstanding_work"] == "0" {
        return diagnostics
      }
      usleep(25_000)
    } while Date() < deadline
    throw GrimodexProcessE2EError.timeout(
      "waiting for speculative hybrid work to become quiescent"
    )
  }

  private func accumulateDiagnosticsDelta(
    before: [String: String],
    after: [String: String],
    counters: inout [String: UInt64],
    invalidations: inout [String: UInt64]
  ) throws {
    for (key, afterRaw) in after
      where key != "invalidations" && key != "outstanding_work" {
      guard let afterValue = UInt64(afterRaw),
            let beforeValue = before[key].flatMap(UInt64.init),
            afterValue >= beforeValue else {
        throw GrimodexProcessE2EError.invalidResponse(
          "hybrid diagnostic counter \(key) is missing or non-monotonic"
        )
      }
      counters[key, default: 0] += afterValue - beforeValue
    }
    let beforeInvalidations = try parseInvalidations(before["invalidations"] ?? "")
    let afterInvalidations = try parseInvalidations(after["invalidations"] ?? "")
    for (reason, afterValue) in afterInvalidations {
      let beforeValue = beforeInvalidations[reason, default: 0]
      guard afterValue >= beforeValue else {
        throw GrimodexProcessE2EError.invalidResponse(
          "hybrid invalidation counter \(reason) is non-monotonic"
        )
      }
      invalidations[reason, default: 0] += afterValue - beforeValue
    }
  }

  private func parseInvalidations(_ raw: String) throws -> [String: UInt64] {
    guard !raw.isEmpty else { return [:] }
    return try Dictionary(uniqueKeysWithValues: raw.split(separator: ",").map {
      field -> (String, UInt64) in
      guard let separator = field.firstIndex(of: "="),
            let value = UInt64(field[field.index(after: separator)...]) else {
        throw GrimodexProcessE2EError.invalidResponse(
          "invalid hybrid invalidation diagnostics"
        )
      }
      return (String(field[..<separator]), value)
    })
  }

  private func delayValues(_ raw: String) throws -> [Int] {
    let values = try raw.split(separator: ",").map { value -> Int in
      guard let parsed = Int(value), parsed >= 0, parsed <= 5_000 else {
        throw GrimodexProcessE2EError.invalidResponse(
          "GRIMODEX_HYBRID_SPIKE_PREFETCH_DELAYS_MS must contain 0...5000"
        )
      }
      return parsed
    }
    guard !values.isEmpty else {
      throw GrimodexProcessE2EError.invalidResponse(
        "GRIMODEX_HYBRID_SPIKE_PREFETCH_DELAYS_MS must not be empty"
      )
    }
    return values
  }

  private func integer(
    _ raw: String?,
    defaultValue: Int,
    minimum: Int,
    name: String
  ) throws -> Int {
    guard let raw else { return defaultValue }
    guard let value = Int(raw), value >= minimum else {
      throw GrimodexProcessE2EError.invalidResponse(
        "\(name) must be an integer >= \(minimum)"
      )
    }
    return value
  }

  private func boolean(
    _ raw: String?,
    defaultValue: Bool,
    name: String
  ) throws -> Bool {
    guard let raw else { return defaultValue }
    switch raw.lowercased() {
    case "1", "true", "yes":
      return true
    case "0", "false", "no":
      return false
    default:
      throw GrimodexProcessE2EError.invalidResponse(
        "\(name) must be true or false"
      )
    }
  }

  private func dictionaryURL(environment: [String: String]) throws -> URL {
    if let path = nonempty(environment["FCITX5_GRIMODEX_DICTIONARY"]) {
      return URL(fileURLWithPath: path, isDirectory: true)
    }
    let url = URL(fileURLWithPath: #filePath)
      .deletingLastPathComponent()
      .deletingLastPathComponent()
      .deletingLastPathComponent()
      .appendingPathComponent("azooKey_dictionary_storage/Dictionary", isDirectory: true)
    guard FileManager.default.fileExists(atPath: url.path) else {
      throw GrimodexProcessE2EError.invalidResponse(
        "hybrid process dictionary does not exist: \(url.path)"
      )
    }
    return url
  }

  private func nonempty(_ value: String?) -> String? {
    guard let value, !value.isEmpty else { return nil }
    return value
  }
#endif
}
