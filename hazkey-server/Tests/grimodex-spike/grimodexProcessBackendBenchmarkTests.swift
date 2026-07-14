import Foundation
import XCTest

#if canImport(Glibc)
import Glibc
#endif

@testable import hazkey_server

final class GrimodexProcessBackendBenchmarkTests: XCTestCase {
  private struct ArtifactIdentity: Encodable {
    let path: String
    let sha256: String
    let sizeBytes: Int

    private enum CodingKeys: String, CodingKey {
      case path
      case sha256
      case sizeBytes = "size_bytes"
    }
  }

  private struct DictionaryIdentity: Encodable {
    let path: String
    let fingerprint: String
  }

  private struct MemorySnapshot: Encodable {
    let rssKiB: Int?
    let pssKiB: Int?

    private enum CodingKeys: String, CodingKey {
      case rssKiB = "rss_kib"
      case pssKiB = "pss_kib"
    }
  }

  private struct BackendMemory: Encodable {
    let serverBefore: MemorySnapshot
    let serverAfter: MemorySnapshot
    let helperBefore: MemorySnapshot?
    let helperAfter: MemorySnapshot?
    let maxObservedEndpointTotalPssKiB: Int

    private enum CodingKeys: String, CodingKey {
      case serverBefore = "server_before"
      case serverAfter = "server_after"
      case helperBefore = "helper_before"
      case helperAfter = "helper_after"
      case maxObservedEndpointTotalPssKiB = "max_observed_endpoint_total_pss_kib"
    }
  }

  private struct LatencySummary: Encodable {
    let mean: Double
    let median: Double
    let p95: Double
    let minimum: Double
    let maximum: Double
    let samples: [Double]
  }

  private struct CandidateResult: Encodable {
    let id: String
    let category: String
    let candidates: [String]
  }

  private struct ProcessStability: Encodable {
    let serverProcessIdentifier: Int32
    let childProcessIdentifiersBefore: [Int32]
    let childProcessIdentifiersAfter: [Int32]
    let helperExecutablePathBefore: String?
    let helperExecutablePathAfter: String?
    let helperExitedAfterServerStop: Bool?

    private enum CodingKeys: String, CodingKey {
      case serverProcessIdentifier = "server_pid"
      case childProcessIdentifiersBefore = "child_pids_before"
      case childProcessIdentifiersAfter = "child_pids_after"
      case helperExecutablePathBefore = "helper_executable_path_before"
      case helperExecutablePathAfter = "helper_executable_path_after"
      case helperExitedAfterServerStop = "helper_exited_after_server_stop"
    }
  }

  private struct BenchmarkPolicy: Encodable {
    let learning = false
    let zenzai = false
    let autoConversion = false

    private enum CodingKeys: String, CodingKey {
      case learning
      case zenzai
      case autoConversion = "auto_conversion"
    }
  }

  private struct ExecutionMetadata: Encodable {
    let generatedAt: String
    let measurementOrder: [String]
    let buildConfiguration: String
    let toolchain: String
    let operatingSystem: String
    let kernelRelease: String
    let cpuModel: String
    let processorCount: Int
    let activeProcessorCount: Int
    let physicalMemoryBytes: UInt64
    let cpuAffinityList: String
    let memorySampling = "sequential_server_then_helper_after_warmup_and_after_measurement"

    private enum CodingKeys: String, CodingKey {
      case generatedAt = "generated_at"
      case measurementOrder = "measurement_order"
      case buildConfiguration = "build_configuration"
      case toolchain
      case operatingSystem = "operating_system"
      case kernelRelease = "kernel_release"
      case cpuModel = "cpu_model"
      case processorCount = "processor_count"
      case activeProcessorCount = "active_processor_count"
      case physicalMemoryBytes = "physical_memory_bytes"
      case cpuAffinityList = "cpu_affinity_list"
      case memorySampling = "memory_sampling"
    }
  }

  private struct BackendResult: Encodable {
    let backend: String
    let protocolVersion: UInt32
    let warmupsPerCase: Int
    let iterationsPerCase: Int
    let conversionCount: Int
    let latencyMilliseconds: LatencySummary
    let memory: BackendMemory
    let processStability: ProcessStability
    let candidates: [CandidateResult]

    private enum CodingKeys: String, CodingKey {
      case backend
      case protocolVersion = "protocol_version"
      case warmupsPerCase = "warmups_per_case"
      case iterationsPerCase = "iterations_per_case"
      case conversionCount = "conversion_count"
      case latencyMilliseconds = "latency_ms"
      case memory
      case processStability = "process_stability"
      case candidates
    }
  }

  private struct Comparison: Encodable {
    let hazkeyOverMozcMeanLatency: Double
    let hazkeyOverMozcMedianLatency: Double
    let hazkeyOverMozcP95Latency: Double
    let mozcPssDeltaPercent: Double

    private enum CodingKeys: String, CodingKey {
      case hazkeyOverMozcMeanLatency = "hazkey_over_mozc_mean_latency"
      case hazkeyOverMozcMedianLatency = "hazkey_over_mozc_median_latency"
      case hazkeyOverMozcP95Latency = "hazkey_over_mozc_p95_latency"
      case mozcPssDeltaPercent = "mozc_pss_delta_percent"
    }
  }

  private struct BenchmarkReport: Encodable {
    let schema = "hazkey.protocol-v2-backend-benchmark.v1"
    let sourceRef: String
    let timingBoundary = "start_conversion_one_protocol_v2_round_trip"
    let policy = BenchmarkPolicy()
    let execution: ExecutionMetadata
    let server: ArtifactIdentity
    let corpus: ArtifactIdentity
    let dictionary: DictionaryIdentity
    let mozcHelper: ArtifactIdentity
    let mozcData: ArtifactIdentity
    let backends: [BackendResult]
    let comparison: Comparison

    private enum CodingKeys: String, CodingKey {
      case schema
      case sourceRef = "source_ref"
      case timingBoundary = "timing_boundary"
      case policy
      case execution
      case server
      case corpus
      case dictionary
      case mozcHelper = "mozc_helper"
      case mozcData = "mozc_data"
      case backends
      case comparison
    }
  }

  func testProtocolV2BackendComparisonKeepsLongLivedProcessesStable() throws {
#if canImport(Glibc)
    let environment = ProcessInfo.processInfo.environment
    guard
      let executablePath = environment["GRIMODEX_PROCESS_E2E_SERVER"],
      !executablePath.isEmpty,
      let helperPath = environment["GRIMODEX_PROCESS_E2E_MOZC_HELPER"],
      !helperPath.isEmpty,
      let dataPath = environment["GRIMODEX_PROCESS_E2E_MOZC_DATA"],
      !dataPath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER, GRIMODEX_PROCESS_E2E_MOZC_HELPER, "
          + "and GRIMODEX_PROCESS_E2E_MOZC_DATA to run the Protocol v2 A/B benchmark"
      )
    }
    let warmups = try integerEnvironmentValue(
      "GRIMODEX_PROCESS_E2E_AB_WARMUPS",
      defaultValue: 3,
      minimum: 0,
      environment: environment
    )
    let iterations = try integerEnvironmentValue(
      "GRIMODEX_PROCESS_E2E_AB_ITERATIONS",
      defaultValue: 20,
      minimum: 1,
      environment: environment
    )
    let sourceRef = environment["GRIMODEX_PROCESS_E2E_AB_SOURCE_REF"] ?? "unspecified"
    let outputPath = environment["GRIMODEX_PROCESS_E2E_AB_OUTPUT"].flatMap {
      $0.isEmpty ? nil : $0
    }
    let buildConfiguration = environment[
      "GRIMODEX_PROCESS_E2E_AB_BUILD_CONFIGURATION"
    ] ?? "unspecified"
    let toolchain = environment["GRIMODEX_PROCESS_E2E_AB_TOOLCHAIN"] ?? "unspecified"
    if outputPath != nil {
      let hexadecimal = CharacterSet(charactersIn: "0123456789abcdef")
      guard sourceRef.count == 40,
            sourceRef.unicodeScalars.allSatisfy(hexadecimal.contains) else {
        throw GrimodexProcessE2EError.invalidResponse(
          "GRIMODEX_PROCESS_E2E_AB_SOURCE_REF must be a full lowercase Git commit"
        )
      }
      guard buildConfiguration != "unspecified", toolchain != "unspecified" else {
        throw GrimodexProcessE2EError.invalidResponse(
          "build configuration and toolchain are required when publishing benchmark output"
        )
      }
    }
    let executableURL = URL(fileURLWithPath: executablePath)
    let helperURL = URL(fileURLWithPath: helperPath)
    let dataURL = URL(fileURLWithPath: dataPath)
    guard FileManager.default.isExecutableFile(atPath: executableURL.path) else {
      throw GrimodexProcessE2EError.invalidResponse(
        "Protocol v2 benchmark server is not executable: \(executableURL.path)"
      )
    }

    let corpusURL = try XCTUnwrap(Bundle.module.resourceURL)
      .appendingPathComponent("Fixtures/ime-base-ab-v1/conversion-quality-v1.tsv")
    let cases = try ABProbeCorpus.load(path: corpusURL.path)
    let dictionaryURL = try dictionaryURL(environment: environment)
    let fixture = try GrimodexProcessSnapshotFixture()
    defer { fixture.remove() }

    let hazkey = try measureBackend(
      name: "hazkey",
      configuration: .hazkey,
      executableURL: executableURL,
      fixture: fixture,
      cases: cases,
      warmups: warmups,
      iterations: iterations,
      dictionaryURL: dictionaryURL,
      expectedHelperURL: nil
    )
    let mozc = try measureBackend(
      name: "mozc",
      configuration: .mozc(helperURL: helperURL, dataURL: dataURL),
      executableURL: executableURL,
      fixture: fixture,
      cases: cases,
      warmups: warmups,
      iterations: iterations,
      dictionaryURL: dictionaryURL,
      expectedHelperURL: helperURL
    )
    let report = BenchmarkReport(
      sourceRef: sourceRef,
      execution: executionMetadata(
        buildConfiguration: buildConfiguration,
        toolchain: toolchain
      ),
      server: try artifactIdentity(executableURL),
      corpus: try artifactIdentity(corpusURL),
      dictionary: DictionaryIdentity(
        path: dictionaryURL.path,
        fingerprint: try ABProbeDictionaryFingerprint.sha256(
          directoryURL: dictionaryURL
        )
      ),
      mozcHelper: try artifactIdentity(helperURL),
      mozcData: try artifactIdentity(dataURL),
      backends: [hazkey, mozc],
      comparison: comparison(hazkey: hazkey, mozc: mozc)
    )
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    var data = try encoder.encode(report)
    data.append(0x0A)
    if let outputPath {
      try data.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
      FileHandle.standardError.write(
        Data("GRIMODEX_PROTOCOL_V2_BENCHMARK output=\(outputPath)\n".utf8)
      )
    } else {
      FileHandle.standardError.write(
        Data("GRIMODEX_PROTOCOL_V2_BENCHMARK ".utf8) + data
      )
    }
#else
    throw XCTSkip("Protocol v2 process benchmarking requires Linux /proc")
#endif
  }

#if canImport(Glibc)
  private func measureBackend(
    name: String,
    configuration: GrimodexProcessConverterConfiguration,
    executableURL: URL,
    fixture: GrimodexProcessSnapshotFixture,
    cases: [ABProbeCorpusCase],
    warmups: Int,
    iterations: Int,
    dictionaryURL: URL,
    expectedHelperURL: URL?
  ) throws -> BackendResult {
    let server = GrimodexProcessHarness(
      executableURL: executableURL,
      grimodexRootURL: fixture.rootURL,
      converterConfiguration: configuration,
      dictionaryURL: dictionaryURL
    )
    try server.start()
    var didStop = false
    defer {
      if !didStop {
        server.stop()
      }
    }
    try server.assertPrivateIPC()
    let serverProcessIdentifier = try XCTUnwrap(server.processIdentifier)
    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    var didCloseClient = false
    defer {
      if !didCloseClient {
        client.close()
      }
    }
    try client.configureBenchmarkProfile()
    let session = try client.openSessionInfo(program: "protocol-v2-benchmark")
    guard session.protocolVersion == 2 else {
      throw GrimodexProcessE2EError.invalidResponse(
        "Protocol v2 benchmark negotiated version \(session.protocolVersion)"
      )
    }

    for testCase in cases {
      for _ in 0..<warmups {
        try client.resetComposition(sessionID: session.sessionID)
        try client.insertText(testCase.reading, sessionID: session.sessionID)
        _ = try client.startConversion(sessionID: session.sessionID)
      }
    }
    let childPIDsBefore = try server.childProcessIdentifiers()
    if expectedHelperURL != nil {
      guard childPIDsBefore.count == 1 else {
        throw GrimodexProcessE2EError.invalidResponse(
          "Mozc did not keep exactly one helper: \(childPIDsBefore)"
        )
      }
    } else if !childPIDsBefore.isEmpty {
      throw GrimodexProcessE2EError.invalidResponse(
        "Hazkey unexpectedly launched child processes: \(childPIDsBefore)"
      )
    }
    let serverBefore = processMemory(processIdentifier: serverProcessIdentifier)
    let helperBefore = childPIDsBefore.first.map {
      processMemory(processIdentifier: $0)
    }
    let helperExecutablePathBefore = try childPIDsBefore.first.map {
      try FileManager.default.destinationOfSymbolicLink(
        atPath: "/proc/\($0)/exe"
      )
    }
    if let expectedHelperURL {
      guard helperExecutablePathBefore == expectedHelperURL.standardizedFileURL.path else {
        throw GrimodexProcessE2EError.invalidResponse(
          "server child is not the configured Mozc helper: "
            + "\(helperExecutablePathBefore ?? "missing")"
        )
      }
    }

    var samples: [Double] = []
    samples.reserveCapacity(cases.count * iterations)
    var candidateResults: [CandidateResult] = []
    candidateResults.reserveCapacity(cases.count)
    for testCase in cases {
      var stableCandidates: [String]?
      for _ in 0..<iterations {
        try client.resetComposition(sessionID: session.sessionID)
        try client.insertText(testCase.reading, sessionID: session.sessionID)
        let started = DispatchTime.now().uptimeNanoseconds
        let candidates = try client.startConversion(sessionID: session.sessionID)
        let finished = DispatchTime.now().uptimeNanoseconds
        samples.append(Double(finished - started) / 1_000_000)
        if let stableCandidates {
          guard candidates == stableCandidates else {
            throw GrimodexProcessE2EError.invalidResponse(
              "candidate output drifted for \(name) case \(testCase.id)"
            )
          }
        } else {
          stableCandidates = candidates
        }
      }
      candidateResults.append(
        CandidateResult(
          id: testCase.id,
          category: testCase.category,
          candidates: stableCandidates ?? []
        )
      )
    }

    let childPIDsAfter = try server.childProcessIdentifiers()
    guard childPIDsAfter == childPIDsBefore else {
      throw GrimodexProcessE2EError.invalidResponse(
        "backend child process set changed during the Protocol v2 benchmark"
      )
    }
    guard server.isRunning else {
      throw GrimodexProcessE2EError.invalidResponse(
        "real server exited during the Protocol v2 benchmark"
      )
    }
    let serverAfter = processMemory(processIdentifier: serverProcessIdentifier)
    let helperAfter = childPIDsAfter.first.map {
      processMemory(processIdentifier: $0)
    }
    let helperExecutablePathAfter = try childPIDsAfter.first.map {
      try FileManager.default.destinationOfSymbolicLink(
        atPath: "/proc/\($0)/exe"
      )
    }
    if let expectedHelperURL {
      guard helperExecutablePathAfter == expectedHelperURL.standardizedFileURL.path else {
        throw GrimodexProcessE2EError.invalidResponse(
          "server child changed from the configured Mozc helper: "
            + "\(helperExecutablePathAfter ?? "missing")"
        )
      }
    }
    guard helperExecutablePathAfter == helperExecutablePathBefore else {
      throw GrimodexProcessE2EError.invalidResponse(
        "backend child executable changed during the Protocol v2 benchmark"
      )
    }
    let beforeTotalPss = try requiredTotalPss(
      server: serverBefore,
      helper: helperBefore
    )
    let afterTotalPss = try requiredTotalPss(
      server: serverAfter,
      helper: helperAfter
    )
    let helperProcessIdentifiers = childPIDsAfter

    client.close()
    didCloseClient = true
    server.stop()
    didStop = true
    let helperExited = helperProcessIdentifiers.isEmpty
      ? nil
      : waitForProcessExit(helperProcessIdentifiers, timeout: 3)
    if helperExited == false {
      throw GrimodexProcessE2EError.invalidResponse(
        "Mozc helper outlived the private real server"
      )
    }

    let latency = ABProbeLatency.summarize(samples)
    return BackendResult(
      backend: name,
      protocolVersion: session.protocolVersion,
      warmupsPerCase: warmups,
      iterationsPerCase: iterations,
      conversionCount: samples.count,
      latencyMilliseconds: LatencySummary(
        mean: samples.reduce(0, +) / Double(samples.count),
        median: latency.median,
        p95: latency.p95,
        minimum: latency.minimum,
        maximum: latency.maximum,
        samples: samples
      ),
      memory: BackendMemory(
        serverBefore: serverBefore,
        serverAfter: serverAfter,
        helperBefore: helperBefore,
        helperAfter: helperAfter,
        maxObservedEndpointTotalPssKiB: max(beforeTotalPss, afterTotalPss)
      ),
      processStability: ProcessStability(
        serverProcessIdentifier: serverProcessIdentifier,
        childProcessIdentifiersBefore: childPIDsBefore,
        childProcessIdentifiersAfter: childPIDsAfter,
        helperExecutablePathBefore: helperExecutablePathBefore,
        helperExecutablePathAfter: helperExecutablePathAfter,
        helperExitedAfterServerStop: helperExited
      ),
      candidates: candidateResults
    )
  }

  private func comparison(
    hazkey: BackendResult,
    mozc: BackendResult
  ) -> Comparison {
    let hazkeyPss = hazkey.memory.maxObservedEndpointTotalPssKiB
    let mozcPss = mozc.memory.maxObservedEndpointTotalPssKiB
    let pssDelta = (Double(mozcPss - hazkeyPss) / Double(hazkeyPss)) * 100
    return Comparison(
      hazkeyOverMozcMeanLatency:
        hazkey.latencyMilliseconds.mean / mozc.latencyMilliseconds.mean,
      hazkeyOverMozcMedianLatency:
        hazkey.latencyMilliseconds.median / mozc.latencyMilliseconds.median,
      hazkeyOverMozcP95Latency:
        hazkey.latencyMilliseconds.p95 / mozc.latencyMilliseconds.p95,
      mozcPssDeltaPercent: pssDelta
    )
  }

  private func integerEnvironmentValue(
    _ name: String,
    defaultValue: Int,
    minimum: Int,
    environment: [String: String]
  ) throws -> Int {
    guard let raw = environment[name] else { return defaultValue }
    guard let value = Int(raw), value >= minimum else {
      throw GrimodexProcessE2EError.invalidResponse(
        "\(name) must be an integer greater than or equal to \(minimum)"
      )
    }
    return value
  }

  private func executionMetadata(
    buildConfiguration: String,
    toolchain: String
  ) -> ExecutionMetadata {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let processInfo = ProcessInfo.processInfo
    return ExecutionMetadata(
      generatedAt: formatter.string(from: Date()),
      measurementOrder: ["hazkey", "mozc"],
      buildConfiguration: buildConfiguration,
      toolchain: toolchain,
      operatingSystem: processInfo.operatingSystemVersionString,
      kernelRelease: firstLine(path: "/proc/sys/kernel/osrelease") ?? "unknown",
      cpuModel: procField(path: "/proc/cpuinfo", field: "model name") ?? "unknown",
      processorCount: processInfo.processorCount,
      activeProcessorCount: processInfo.activeProcessorCount,
      physicalMemoryBytes: processInfo.physicalMemory,
      cpuAffinityList: procField(
        path: "/proc/self/status",
        field: "Cpus_allowed_list"
      ) ?? "unknown"
    )
  }

  private func firstLine(path: String) -> String? {
    guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else {
      return nil
    }
    return contents.split(whereSeparator: \.isNewline).first.map(String.init)
  }

  private func procField(path: String, field: String) -> String? {
    guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else {
      return nil
    }
    for line in contents.split(whereSeparator: \.isNewline) {
      guard let separator = line.firstIndex(of: ":") else { continue }
      let key = line[..<separator].trimmingCharacters(in: .whitespaces)
      guard key == field else { continue }
      return line[line.index(after: separator)...]
        .trimmingCharacters(in: .whitespaces)
    }
    return nil
  }

  private func dictionaryURL(environment: [String: String]) throws -> URL {
    if let path = environment["FCITX5_GRIMODEX_DICTIONARY"], !path.isEmpty {
      return URL(fileURLWithPath: path, isDirectory: true)
    }
    let url = URL(fileURLWithPath: #filePath)
      .deletingLastPathComponent()
      .deletingLastPathComponent()
      .deletingLastPathComponent()
      .appendingPathComponent("azooKey_dictionary_storage/Dictionary", isDirectory: true)
    guard FileManager.default.fileExists(atPath: url.path) else {
      throw GrimodexProcessE2EError.invalidResponse(
        "Protocol v2 benchmark dictionary does not exist: \(url.path)"
      )
    }
    return url
  }

  private func artifactIdentity(_ url: URL) throws -> ArtifactIdentity {
    let attributes = try FileManager.default.attributesOfItem(atPath: url.path)
    let size = (attributes[.size] as? NSNumber)?.intValue ?? 0
    let handle = try FileHandle(forReadingFrom: url)
    defer { try? handle.close() }
    var hasher = ABProbeSHA256()
    while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty {
      hasher.update(data)
    }
    let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
    return ArtifactIdentity(path: url.path, sha256: digest, sizeBytes: size)
  }

  private func processMemory(processIdentifier: Int32) -> MemorySnapshot {
    MemorySnapshot(
      rssKiB: memoryKilobytes(
        path: "/proc/\(processIdentifier)/status",
        field: "VmRSS:"
      ),
      pssKiB: memoryKilobytes(
        path: "/proc/\(processIdentifier)/smaps_rollup",
        field: "Pss:"
      )
    )
  }

  private func memoryKilobytes(path: String, field: String) -> Int? {
    guard let contents = try? String(contentsOfFile: path, encoding: .utf8),
          let line = contents.split(separator: "\n").first(where: {
            $0.hasPrefix(field)
          }) else {
      return nil
    }
    return line.split(whereSeparator: \.isWhitespace).dropFirst().first.flatMap {
      Int($0)
    }
  }

  private func requiredTotalPss(
    server: MemorySnapshot,
    helper: MemorySnapshot?
  ) throws -> Int {
    guard let serverPss = server.pssKiB else {
      throw GrimodexProcessE2EError.invalidResponse(
        "unable to read server PSS from /proc"
      )
    }
    guard let helper else { return serverPss }
    guard let helperPss = helper.pssKiB else {
      throw GrimodexProcessE2EError.invalidResponse(
        "unable to read Mozc helper PSS from /proc"
      )
    }
    return serverPss + helperPss
  }

  private func waitForProcessExit(
    _ processIdentifiers: [Int32],
    timeout: TimeInterval
  ) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
      if processIdentifiers.allSatisfy({ kill($0, 0) != 0 && errno == ESRCH }) {
        return true
      }
      usleep(10_000)
    }
    return processIdentifiers.allSatisfy({ kill($0, 0) != 0 && errno == ESRCH })
  }
#endif
}
