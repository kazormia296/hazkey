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
        backendName: "hazkey"
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
        backendName: "A"
      )
    )
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
      backendName: "hazkey"
    )
    let provenance = try ABProbeProvenance.resolve(options: options)
    XCTAssertEqual(provenance.sourceRef, "source-ref")
    XCTAssertEqual(provenance.dictionaryPath, dictionary.path)
    XCTAssertTrue(provenance.dictionaryFingerprint.hasPrefix("sha256:"))
    XCTAssertEqual(provenance.dictionaryFingerprint.count, 71)
    XCTAssertEqual(
      provenance.dictionaryFingerprint,
      try ABProbeDictionaryFingerprint.sha256(directoryURL: mirror)
    )

    // Same filename and byte length, different bytes: the fingerprint must change.
    try Data("ALPHA".utf8).write(to: dictionary.appendingPathComponent("a.bin"))
    XCTAssertNotEqual(
      provenance.dictionaryFingerprint,
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
          backendName: "hazkey"
        )
      )
    )
  }

  func testResultJSONContainsProvenanceFields() throws {
    let result = ABProbeResult(
      id: "case",
      category: "sample",
      backend: "hazkey",
      backendVersion: "test",
      sourceRef: "abc123",
      dictionaryPath: "/canonical/dictionary",
      dictionaryFingerprint: "sha256:abcdef",
      candidates: ["候補"],
      measurement: ABProbeMeasurement(
        warmups: 0,
        iterations: 1,
        latencyMilliseconds: ABProbeLatency.summarize([1]),
        residentMemory: ABProbeMemory(beforeKiB: 1, afterKiB: 2)
      )
    )
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: JSONEncoder().encode(result))
        as? [String: Any]
    )
    XCTAssertEqual(object["source_ref"] as? String, "abc123")
    XCTAssertEqual(object["dictionary_path"] as? String, "/canonical/dictionary")
    XCTAssertEqual(object["dictionary_fingerprint"] as? String, "sha256:abcdef")
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
}
