import Foundation
import XCTest
#if os(Linux)
import Glibc
#else
import Darwin
#endif

@testable import hazkey_server

final class GrimodexABProbeTests: XCTestCase {
  func testOptionsParseStableDefaultsAndOverrides() throws {
    XCTAssertEqual(
      try ABProbeOptions.parse(arguments: [
        "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
        "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
      ]),
      ABProbeOptions(
        corpusPath: "/tmp/corpus.tsv",
        dictionaryPath: "/tmp/dictionary",
        sourceRef: "abc123",
        warmups: 2,
        iterations: 10,
        topK: 10,
        backendName: "hazkey",
        converterBackend: .hazkey,
        mozcBundlePath: nil
      )
    )

    XCTAssertEqual(
      try ABProbeOptions.parse(arguments: [
        "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
        "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
        "--warmups", "0", "--iterations", "3", "--top-k", "7",
        "--backend-name", "A",
      ]),
      ABProbeOptions(
        corpusPath: "/tmp/corpus.tsv",
        dictionaryPath: "/tmp/dictionary",
        sourceRef: "abc123",
        warmups: 0,
        iterations: 3,
        topK: 7,
        backendName: "A",
        converterBackend: .hazkey,
        mozcBundlePath: nil
      )
    )
  }

  func testOptionsParseMozcBackendAndBundle() throws {
    XCTAssertEqual(
      try ABProbeOptions.parse(arguments: [
        "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
        "--source-ref", "abc123", "--converter-backend", "mozc",
        "--mozc-bundle", "/tmp/mozc-bundle",
      ]),
      ABProbeOptions(
        corpusPath: "/tmp/corpus.tsv",
        dictionaryPath: nil,
        sourceRef: "abc123",
        warmups: 2,
        iterations: 10,
        topK: 10,
        backendName: "mozc",
        converterBackend: .mozc,
        mozcBundlePath: "/tmp/mozc-bundle"
      )
    )
  }

  func testOptionsRejectBackendSpecificArgumentCombinations() {
    let invalidArguments: [([String], ABProbeError)] = [
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--mozc-bundle", "bundle",
        ],
        .invalidArguments("--mozc-bundle requires --converter-backend mozc")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--source-ref", "ref", "--converter-backend", "mozc",
        ],
        .invalidArguments("--mozc-bundle is required for the Mozc backend")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--converter-backend", "mozc", "--mozc-bundle", "bundle",
        ],
        .invalidArguments("--dictionary is not used by the Mozc backend")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--converter-backend", "unknown",
        ],
        .invalidArguments("--converter-backend must be hazkey or mozc")
      ),
    ]

    for (arguments, expectedError) in invalidArguments {
      XCTAssertThrowsError(try ABProbeOptions.parse(arguments: arguments)) {
        XCTAssertEqual($0 as? ABProbeError, expectedError)
      }
    }
  }

  func testOptionsRejectInvalidOrUnknownArguments() {
    let invalidArguments = [
      ["hazkey-server", "--ab-probe"],
      ["hazkey-server", "--ab-probe", "--corpus", "x", "--source-ref", "ref"],
      ["hazkey-server", "--ab-probe", "--corpus", "x", "--dictionary", "dict"],
      [
        "hazkey-server", "--ab-probe", "--corpus", "x",
        "--dictionary", "dict", "--source-ref", "ref", "--iterations", "0",
      ],
      [
        "hazkey-server", "--ab-probe", "--corpus", "x",
        "--dictionary", "dict", "--source-ref", "ref", "--top-k", "11",
      ],
      [
        "hazkey-server", "--ab-probe", "--corpus", "x",
        "--dictionary", "dict", "--source-ref", "ref", "--unknown",
      ],
    ]
    for arguments in invalidArguments {
      XCTAssertThrowsError(try ABProbeOptions.parse(arguments: arguments))
    }
  }

  func testCorpusLoaderValidatesRowsAndDuplicateIDs() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let corpus = directory.appendingPathComponent("corpus.tsv")
    try Data(
      "id\treading\texpected\tcategory\ncase\tよみ\t読み\tsample\n".utf8
    ).write(to: corpus)

    XCTAssertEqual(
      try ABProbeCorpus.load(path: corpus.path),
      [ABProbeCorpusCase(id: "case", reading: "よみ", category: "sample")]
    )

    try Data(
      ("id\treading\texpected\tcategory\n"
        + "case\tよみ\t読み\tsample\n"
        + "case\tべつ\t別\tsample\n").utf8
    ).write(to: corpus)
    XCTAssertThrowsError(try ABProbeCorpus.load(path: corpus.path))
  }

  func testLatencySummaryUsesMedianAndNearestRankP95() {
    let summary = ABProbeLatency.summarize([8, 1, 5, 2])
    XCTAssertEqual(summary.median, 3.5)
    XCTAssertEqual(summary.p95, 8)
    XCTAssertEqual(summary.minimum, 1)
    XCTAssertEqual(summary.maximum, 8)
    XCTAssertEqual(summary.samples, [8, 1, 5, 2])
  }

  func testSHA256MatchesKnownVector() {
    var hasher = ABProbeSHA256()
    hasher.update(Data("abc".utf8))
    XCTAssertEqual(
      hasher.finalize().map { String(format: "%02x", $0) }.joined(),
      "ba7816bf8f01cfea414140de5dae2223"
        + "b00361a396177a9cb410ff61f20015ad"
    )
  }

  func testProvenanceCanonicalizesDirectoryAndHashesFileContents() throws {
    let temporaryRoot = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    let dictionary = temporaryRoot.appendingPathComponent("dictionary", isDirectory: true)
    let mirror = temporaryRoot.appendingPathComponent("mirror", isDirectory: true)
    try FileManager.default.createDirectory(
      at: dictionary.appendingPathComponent("nested", isDirectory: true),
      withIntermediateDirectories: true
    )
    try FileManager.default.createDirectory(
      at: mirror.appendingPathComponent("nested", isDirectory: true),
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: temporaryRoot) }
    try Data("alpha".utf8).write(to: dictionary.appendingPathComponent("a.bin"))
    try Data("beta".utf8).write(
      to: dictionary.appendingPathComponent("nested/b.bin")
    )
    // Create the mirror in reverse order: creation order must not affect the digest.
    try Data("beta".utf8).write(to: mirror.appendingPathComponent("nested/b.bin"))
    try Data("alpha".utf8).write(to: mirror.appendingPathComponent("a.bin"))

    let options = ABProbeOptions(
      corpusPath: "/tmp/corpus.tsv",
      dictionaryPath: dictionary.appendingPathComponent("../dictionary").path,
      sourceRef: "source-ref",
      warmups: 0,
      iterations: 1,
      topK: 1,
      backendName: "hazkey",
      converterBackend: .hazkey,
      mozcBundlePath: nil
    )
    let provenance = try ABProbeProvenance.resolve(options: options)
    XCTAssertEqual(provenance.sourceRef, "source-ref")
    XCTAssertEqual(provenance.resource.kind, "hazkey_dictionary")
    XCTAssertEqual(provenance.resource.path, dictionary.path)
    XCTAssertTrue(provenance.resource.fingerprint.hasPrefix("sha256:"))
    XCTAssertEqual(provenance.resource.fingerprint.count, 71)
    XCTAssertEqual(
      provenance.resource.fingerprint,
      try ABProbeDictionaryFingerprint.sha256(directoryURL: mirror)
    )

    // Same filename and byte length, different bytes: the fingerprint must change.
    try Data("ALPHA".utf8).write(to: dictionary.appendingPathComponent("a.bin"))
    XCTAssertNotEqual(
      provenance.resource.fingerprint,
      try ABProbeDictionaryFingerprint.sha256(directoryURL: dictionary)
    )

    let regularFile = temporaryRoot.appendingPathComponent("not-a-directory")
    try Data().write(to: regularFile)
    XCTAssertThrowsError(
      try ABProbeProvenance.resolve(
        options: ABProbeOptions(
          corpusPath: "/tmp/corpus.tsv",
          dictionaryPath: regularFile.path,
          sourceRef: "source-ref",
          warmups: 0,
          iterations: 1,
          topK: 1,
          backendName: "hazkey",
          converterBackend: .hazkey,
          mozcBundlePath: nil
        )
      )
    )
  }

  func testMozcProvenancePinsVerifiedBytesInPrivateReadOnlyRuntime() throws {
    let fixture = try makeMozcGeneration()
    defer { try? FileManager.default.removeItem(at: fixture.root) }

    let provenance = try ABProbeProvenance.resolve(
      options: mozcOptions(bundlePath: fixture.generation.path),
      trustedMozcArtifacts: fixture.trustedArtifacts
    )
    let runtimePath = try XCTUnwrap(provenance.mozcRuntimePath)
    defer { try? ABProbeMozcRuntimeSnapshot.remove(runtimePath: runtimePath) }
    let runtime = URL(fileURLWithPath: runtimePath, isDirectory: true)

    XCTAssertEqual(provenance.resource.path, fixture.generation.path)
    XCTAssertEqual(provenance.resource.kind, "mozc_runtime_inputs")
    XCTAssertNotEqual(runtimePath, fixture.generation.path)
    XCTAssertEqual(
      try Data(contentsOf: runtime.appendingPathComponent("fcitx5-grimodex-mozc-helper")),
      fixture.helper
    )
    XCTAssertEqual(
      (try FileManager.default.attributesOfItem(atPath: runtimePath)[.posixPermissions])
        as? NSNumber,
      NSNumber(value: 0o555)
    )
    XCTAssertEqual(
      provenance.resource.fingerprint,
      try ABProbeDictionaryFingerprint.sha256(
        directoryURL: runtime,
        domain: "hazkey.mozc-runtime-fingerprint.v1"
      )
    )

    let sourceHelper = fixture.generation.appendingPathComponent(
      "fcitx5-grimodex-mozc-helper"
    )
    XCTAssertEqual(chmod(sourceHelper.path, 0o644), 0)
    try Data("tampered after provenance".utf8).write(to: sourceHelper)
    XCTAssertEqual(
      try Data(contentsOf: runtime.appendingPathComponent("fcitx5-grimodex-mozc-helper")),
      fixture.helper,
      "the measured executable must remain the exact bytes fingerprinted during provenance"
    )
  }

  func testMozcProvenanceRejectsSymlinkAndManifestIdentityMismatch() throws {
    let symlinkFixture = try makeMozcGeneration()
    defer { try? FileManager.default.removeItem(at: symlinkFixture.root) }
    let sourceHelper = symlinkFixture.generation.appendingPathComponent(
      "fcitx5-grimodex-mozc-helper"
    )
    let externalHelper = symlinkFixture.root.appendingPathComponent("external-helper")
    try Data("external".utf8).write(to: externalHelper)
    try FileManager.default.removeItem(at: sourceHelper)
    try FileManager.default.createSymbolicLink(
      at: sourceHelper,
      withDestinationURL: externalHelper
    )
    XCTAssertThrowsError(
      try ABProbeProvenance.resolve(
        options: mozcOptions(bundlePath: symlinkFixture.generation.path),
        trustedMozcArtifacts: symlinkFixture.trustedArtifacts
      )
    )

    let manifestFixture = try makeMozcGeneration()
    defer { try? FileManager.default.removeItem(at: manifestFixture.root) }
    let manifest = manifestFixture.generation.appendingPathComponent("manifest.json")
    XCTAssertEqual(chmod(manifest.path, 0o644), 0)
    let badManifest = try JSONSerialization.data(
      withJSONObject: [
        "schema": "grimodex.mozc-artifact-bundle.v1",
        "artifacts": [
          "fcitx5-grimodex-mozc-helper": [
            "size": manifestFixture.helper.count,
            "sha256": String(repeating: "0", count: 64),
          ],
          "mozc.data": [
            "size": manifestFixture.data.count,
            "sha256": digest(manifestFixture.data),
          ],
        ],
      ],
      options: [.sortedKeys]
    )
    try badManifest.write(to: manifest)
    XCTAssertEqual(chmod(manifest.path, 0o444), 0)
    XCTAssertThrowsError(
      try ABProbeProvenance.resolve(
        options: mozcOptions(bundlePath: manifestFixture.generation.path),
        trustedMozcArtifacts: manifestFixture.trustedArtifacts
      )
    )
  }

  func testResultJSONContainsProvenanceFields() throws {
    let result = ABProbeResult(
      id: "case",
      category: "sample",
      backend: "hazkey",
      backendVersion: "test",
      converterBackend: "hazkey",
      sourceRef: "abc123",
      resource: ABProbeResourceProvenance(
        kind: "hazkey_dictionary",
        path: "/canonical/dictionary",
        fingerprint: "sha256:abcdef"
      ),
      candidates: ["候補"],
      measurement: ABProbeMeasurement(
        warmups: 0,
        iterations: 1,
        latencyMilliseconds: ABProbeLatency.summarize([1]),
        residentMemory: ABProbeMemory(
          beforeKiB: 1,
          afterKiB: 2,
          beforePssKiB: 3,
          afterPssKiB: 4,
          backendBeforeKiB: 5,
          backendAfterKiB: 6,
          backendBeforePssKiB: 7,
          backendAfterPssKiB: 8
        ),
        backendDiagnostics: ABProbeBackendDiagnosticsResult(
          processLaunchCount: 1,
          cleanupFailureCount: 0
        )
      )
    )
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: JSONEncoder().encode(result))
        as? [String: Any]
    )
    XCTAssertEqual(object["schema"] as? String, "hazkey.ab-probe-result.v2")
    XCTAssertEqual(object["converter_backend"] as? String, "hazkey")
    XCTAssertEqual(object["source_ref"] as? String, "abc123")
    let resource = try XCTUnwrap(object["resource"] as? [String: Any])
    XCTAssertEqual(resource["kind"] as? String, "hazkey_dictionary")
    XCTAssertEqual(resource["path"] as? String, "/canonical/dictionary")
    XCTAssertEqual(resource["fingerprint"] as? String, "sha256:abcdef")
    XCTAssertNil(object["dictionary_path"])
    XCTAssertNil(object["dictionary_fingerprint"])
    let measurement = try XCTUnwrap(object["measurement"] as? [String: Any])
    let diagnostics = try XCTUnwrap(
      measurement["backend_diagnostics"] as? [String: Any]
    )
    XCTAssertEqual(diagnostics["process_launch_count"] as? Int, 1)
    XCTAssertEqual(diagnostics["cleanup_failure_count"] as? Int, 0)
  }

  func testJSONOutputIsolationKeepsDebugTextOffStandardOutput() throws {
    let capture = Pipe()
    _ = fflush(nil)
    let savedStandardOutput = dup(STDOUT_FILENO)
    guard savedStandardOutput >= 0 else {
      XCTFail("unable to duplicate stdout")
      return
    }
    guard dup2(capture.fileHandleForWriting.fileDescriptor, STDOUT_FILENO) >= 0 else {
      _ = close(savedStandardOutput)
      XCTFail("unable to redirect stdout")
      return
    }

    do {
      try ABProbeJSONOutput.withIsolatedStandardOutput { jsonOutput in
        print("debug text that must not enter JSONL")
        jsonOutput.write(Data("{\"schema\":\"probe\"}\n".utf8))
      }
    } catch {
      _ = fflush(nil)
      _ = dup2(savedStandardOutput, STDOUT_FILENO)
      _ = close(savedStandardOutput)
      try? capture.fileHandleForWriting.close()
      throw error
    }

    _ = fflush(nil)
    XCTAssertGreaterThanOrEqual(dup2(savedStandardOutput, STDOUT_FILENO), 0)
    _ = close(savedStandardOutput)
    try capture.fileHandleForWriting.close()
    let captured = capture.fileHandleForReading.readDataToEndOfFile()
    let output = try XCTUnwrap(String(data: captured, encoding: .utf8))
    XCTAssertEqual(output, "{\"schema\":\"probe\"}\n")
    for line in output.split(separator: "\n") {
      XCTAssertNoThrow(try JSONSerialization.jsonObject(with: Data(line.utf8)))
    }
  }

  func testBufferedJSONOutputPublishesNothingWhenCleanupFails() throws {
    let output = Pipe()
    XCTAssertThrowsError(
      try ABProbeJSONOutput.publishBuffered(
        [Data("{\"schema\":\"probe\"}\n".utf8)],
        to: output.fileHandleForWriting
      ) {
        throw ABProbeError.backendInstability("cleanup failed")
      }
    )
    try output.fileHandleForWriting.close()
    XCTAssertEqual(output.fileHandleForReading.readDataToEndOfFile(), Data())
  }

  private struct MozcGenerationFixture {
    let root: URL
    let generation: URL
    let helper: Data
    let data: Data
    let trustedArtifacts: ABProbeMozcTrustedArtifacts
  }

  private func makeMozcGeneration() throws -> MozcGenerationFixture {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    let generation = root.appendingPathComponent(
      "sha256-" + String(repeating: "a", count: 64),
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: generation,
      withIntermediateDirectories: true
    )
    XCTAssertEqual(chmod(generation.path, 0o755), 0)

    let helper = Data("test helper bytes".utf8)
    let data = Data("test mozc data bytes".utf8)
    let trustedArtifacts = ABProbeMozcTrustedArtifacts(
      helper: ABProbeMozcArtifactIdentity(
        size: helper.count,
        sha256: digest(helper)
      ),
      data: ABProbeMozcArtifactIdentity(
        size: data.count,
        sha256: digest(data)
      )
    )
    let manifest = try JSONSerialization.data(
      withJSONObject: [
        "schema": "grimodex.mozc-artifact-bundle.v1",
        "artifacts": [
          "fcitx5-grimodex-mozc-helper": [
            "size": helper.count,
            "sha256": trustedArtifacts.helper.sha256,
          ],
          "mozc.data": [
            "size": data.count,
            "sha256": trustedArtifacts.data.sha256,
          ],
        ],
      ],
      options: [.sortedKeys]
    )

    let helperURL = generation.appendingPathComponent("fcitx5-grimodex-mozc-helper")
    let dataURL = generation.appendingPathComponent("mozc.data")
    let manifestURL = generation.appendingPathComponent("manifest.json")
    try helper.write(to: helperURL)
    try data.write(to: dataURL)
    try manifest.write(to: manifestURL)
    XCTAssertEqual(chmod(helperURL.path, 0o555), 0)
    XCTAssertEqual(chmod(dataURL.path, 0o444), 0)
    XCTAssertEqual(chmod(manifestURL.path, 0o444), 0)

    return MozcGenerationFixture(
      root: root,
      generation: generation,
      helper: helper,
      data: data,
      trustedArtifacts: trustedArtifacts
    )
  }

  private func mozcOptions(bundlePath: String) -> ABProbeOptions {
    ABProbeOptions(
      corpusPath: "/tmp/corpus.tsv",
      dictionaryPath: nil,
      sourceRef: "source-ref",
      warmups: 0,
      iterations: 1,
      topK: 1,
      backendName: "mozc",
      converterBackend: .mozc,
      mozcBundlePath: bundlePath
    )
  }

  private func digest(_ data: Data) -> String {
    var hasher = ABProbeSHA256()
    hasher.update(data)
    return hasher.finalize().map { String(format: "%02x", $0) }.joined()
  }
}
