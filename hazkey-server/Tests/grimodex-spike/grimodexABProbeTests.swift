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

  func testOptionsResultSchemaDefaultsToV3AndAcceptsSegmentSchemas() throws {
    let baseArguments = [
      "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
      "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
    ]

    let defaultOptions = try ABProbeOptions.parse(arguments: baseArguments)
    XCTAssertEqual(defaultOptions.resultSchema, .v3)
    XCTAssertEqual(defaultOptions.resultSchema.conversionPath, .candidates)

    let v4Options = try ABProbeOptions.parse(
      arguments: baseArguments + ["--result-schema", "v4"]
    )
    XCTAssertEqual(v4Options.resultSchema, .v4)
    XCTAssertEqual(v4Options.resultSchema.conversionPath, .segmentCandidates)

    let v5Options = try ABProbeOptions.parse(
      arguments: baseArguments + ["--result-schema", "v5"]
    )
    XCTAssertEqual(v5Options.resultSchema, .v5)
    XCTAssertEqual(v5Options.resultSchema.conversionPath, .segmentCandidates)
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
      [
        "hazkey-server", "--ab-probe", "--corpus", "x",
        "--dictionary", "dict", "--source-ref", "ref",
        "--result-schema", "v6",
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

  func testCorpusSnapshotHashesExactParsedBytesAndCountsCases() throws {
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
    let bytes = Data(
      "id\treading\texpected\tcategory\ncase\tよみ\t読み\tsample\n".utf8
    )
    try bytes.write(to: corpus)

    let snapshot = try ABProbeCorpus.loadSnapshot(path: corpus.path)

    XCTAssertEqual(
      snapshot.cases,
      [ABProbeCorpusCase(id: "case", reading: "よみ", category: "sample")]
    )
    XCTAssertEqual(
      snapshot.provenance.sha256,
      "sha256:0e2730fa123c3051c89fa00f4cd9c81d83b5593f7e04419e41ec76ca279423b1"
    )
    XCTAssertEqual(snapshot.provenance.cases, 1)
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

  func testFixedMozcProfilesPinB0AndB1AsExactHelperDatasetPairs() {
    XCTAssertEqual(ABProbeMozcTrustedArtifacts.fixed, .fixedB0)
    XCTAssertEqual(ABProbeMozcTrustedArtifacts.fixedProfiles, [.fixedB0, .fixedB1])
    XCTAssertEqual(ABProbeMozcTrustedArtifacts.fixedB0.helper.size, 5_695_048)
    XCTAssertEqual(
      ABProbeMozcTrustedArtifacts.fixedB0.helper.sha256,
      "8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d"
    )
    XCTAssertEqual(ABProbeMozcTrustedArtifacts.fixedB1.helper.size, 5_746_568)
    XCTAssertEqual(
      ABProbeMozcTrustedArtifacts.fixedB1.helper.sha256,
      "728d9a79c0f540a832d3f404a2603f49080e1f9e7ee1d24df1a0a69f5a4a75e8"
    )
    XCTAssertEqual(
      ABProbeMozcTrustedArtifacts.fixedB1.data,
      ABProbeMozcTrustedArtifacts.fixedB0.data
    )
  }

  func testMozcRuntimeRejectsArtifactsThatOnlyMatchDifferentTrustedProfiles() throws {
    let fixture = try makeMozcGeneration()
    defer { try? FileManager.default.removeItem(at: fixture.root) }

    let wrongData = Data("different dataset".utf8)
    let wrongHelper = Data("different helper".utf8)
    let helperOnlyProfile = ABProbeMozcTrustedArtifacts(
      helper: fixture.trustedArtifacts.helper,
      data: ABProbeMozcArtifactIdentity(
        size: wrongData.count,
        sha256: digest(wrongData)
      )
    )
    let dataOnlyProfile = ABProbeMozcTrustedArtifacts(
      helper: ABProbeMozcArtifactIdentity(
        size: wrongHelper.count,
        sha256: digest(wrongHelper)
      ),
      data: fixture.trustedArtifacts.data
    )

    XCTAssertThrowsError(
      try ABProbeMozcRuntimeSnapshot.prepare(
        sourceURL: fixture.generation,
        trustedArtifactProfiles: [helperOnlyProfile, dataOnlyProfile]
      )
    ) { error in
      XCTAssertEqual(
        error as? ABProbeError,
        .mozcBundleInvalid(
          "Mozc helper and dataset do not match one trusted artifact profile"
        )
      )
    }
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
      reading: "よみ",
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
      topK: 7,
      corpus: ABProbeCorpusProvenance(
        sha256: "sha256:" + String(repeating: "1", count: 64),
        cases: 15
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
    XCTAssertEqual(object["schema"] as? String, "hazkey.ab-probe-result.v3")
    XCTAssertEqual(object["reading"] as? String, "よみ")
    XCTAssertEqual(object["top_k"] as? Int, 7)
    XCTAssertEqual(object["converter_backend"] as? String, "hazkey")
    XCTAssertEqual(object["source_ref"] as? String, "abc123")
    XCTAssertEqual(object["candidates"] as? [String], ["候補"])
    XCTAssertNil(object["conversion_path"])
    let corpus = try XCTUnwrap(object["corpus"] as? [String: Any])
    XCTAssertEqual(
      corpus["sha256"] as? String,
      "sha256:" + String(repeating: "1", count: 64)
    )
    XCTAssertEqual(corpus["cases"] as? Int, 15)
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

  func testV4ResultUsesStructuredCandidatesAndSegmentConversionPath() throws {
    let result = ABProbeResultV4(
      v3: sampleV3Result(candidates: ["第一", "第二"]),
      candidates: [
        ABProbeV4Candidate(text: "第一", rank: 1, consumingCount: 4),
        ABProbeV4Candidate(text: "第二", rank: 2, consumingCount: 2),
      ]
    )
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: JSONEncoder().encode(result))
        as? [String: Any]
    )

    XCTAssertEqual(object["schema"] as? String, "hazkey.ab-probe-result.v4")
    XCTAssertEqual(object["conversion_path"] as? String, "segment_candidates")
    XCTAssertEqual(object["reading"] as? String, "よみ")
    XCTAssertEqual(object["top_k"] as? Int, 7)
    XCTAssertEqual(object["converter_backend"] as? String, "hazkey")
    XCTAssertEqual(object["source_ref"] as? String, "abc123")
    XCTAssertNotNil(object["corpus"] as? [String: Any])
    XCTAssertNotNil(object["resource"] as? [String: Any])

    let candidates = try XCTUnwrap(object["candidates"] as? [[String: Any]])
    XCTAssertEqual(candidates.count, 2)
    XCTAssertEqual(candidates[0]["text"] as? String, "第一")
    XCTAssertEqual(candidates[0]["rank"] as? Int, 1)
    XCTAssertEqual(candidates[0]["consuming_count"] as? Int, 4)
    XCTAssertEqual(candidates[1]["text"] as? String, "第二")
    XCTAssertEqual(candidates[1]["rank"] as? Int, 2)
    XCTAssertEqual(candidates[1]["consuming_count"] as? Int, 2)
    XCTAssertNil(object["composition_span"])
  }

  func testV5ResultAddsEntireCompositionSpan() throws {
    let composition = CompositionInput(
      elements: [
        CompositionElement(text: "よ", inputStyle: .direct),
        CompositionElement(text: "み", inputStyle: .direct),
        CompositionElement(text: "を", inputStyle: .direct),
      ],
      cursor: 3,
      leftContext: ""
    )
    let span = ABProbeCompositionSpan.entireComposition(composition)
    XCTAssertEqual(
      span,
      ABProbeCompositionSpan(
        start: 0,
        count: composition.elements.count,
        unit: "composition_element"
      )
    )

    let result = ABProbeResultV5(
      v3: sampleV3Result(candidates: ["第一"]),
      candidates: [
        ABProbeV4Candidate(text: "第一", rank: 1, consumingCount: 2),
      ],
      compositionSpan: span
    )
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: JSONEncoder().encode(result))
        as? [String: Any]
    )

    XCTAssertEqual(object["schema"] as? String, "hazkey.ab-probe-result.v5")
    XCTAssertEqual(object["conversion_path"] as? String, "segment_candidates")
    let encodedSpan = try XCTUnwrap(
      object["composition_span"] as? [String: Any]
    )
    XCTAssertEqual(encodedSpan["start"] as? Int, 0)
    XCTAssertEqual(encodedSpan["count"] as? Int, composition.elements.count)
    XCTAssertEqual(encodedSpan["unit"] as? String, "composition_element")

    let candidates = try XCTUnwrap(object["candidates"] as? [[String: Any]])
    XCTAssertEqual(candidates[0]["text"] as? String, "第一")
    XCTAssertEqual(candidates[0]["rank"] as? Int, 1)
    XCTAssertEqual(candidates[0]["consuming_count"] as? Int, 2)
  }

  func testCandidateObservationCapturesRankAndCompositionElementCount() {
    let observed = ABProbeCandidateObservation.capture(
      [
        ConverterCandidate(text: "第一", consumingCount: 4),
        ConverterCandidate(text: "第二", consumingCount: 2),
      ],
      topK: 2
    )

    XCTAssertEqual(
      observed,
      [
        ABProbeV4Candidate(text: "第一", rank: 1, consumingCount: 4),
        ABProbeV4Candidate(text: "第二", rank: 2, consumingCount: 2),
      ]
    )
    XCTAssertTrue(observed.allSatisfy { $0.consumingCount > 0 })
  }

  func testCandidateDriftIncludesTextRankAndConsumingCount() throws {
    let reference = [
      ABProbeV4Candidate(text: "候補", rank: 1, consumingCount: 3),
    ]
    XCTAssertNoThrow(
      try ABProbeCandidateObservation.validateStable(
        reference: reference,
        observed: reference,
        caseID: "case"
      )
    )

    let drifted = [
      [ABProbeV4Candidate(text: "別候補", rank: 1, consumingCount: 3)],
      [ABProbeV4Candidate(text: "候補", rank: 2, consumingCount: 3)],
      [ABProbeV4Candidate(text: "候補", rank: 1, consumingCount: 2)],
    ]
    for observed in drifted {
      XCTAssertThrowsError(
        try ABProbeCandidateObservation.validateStable(
          reference: reference,
          observed: observed,
          caseID: "case"
        )
      ) {
        XCTAssertEqual(
          $0 as? ABProbeError,
          .candidateDrift("candidate output drifted during case case")
        )
      }
    }
  }

  func testV5CandidateStabilityIncludesTextRankAndConsumingCount() throws {
    let reference = [
      ABProbeV4Candidate(text: "候補", rank: 1, consumingCount: 3),
    ]
    XCTAssertNoThrow(
      try ABProbeCandidateObservation.validateStable(
        reference: reference,
        observed: reference,
        resultSchema: .v5,
        caseID: "v5-case"
      )
    )

    XCTAssertThrowsError(
      try ABProbeCandidateObservation.validateStable(
        reference: reference,
        observed: [
          ABProbeV4Candidate(text: "候補", rank: 1, consumingCount: 2),
        ],
        resultSchema: .v5,
        caseID: "v5-case"
      )
    ) {
      XCTAssertEqual(
        $0 as? ABProbeError,
        .candidateDrift("candidate output drifted during case v5-case")
      )
    }
  }

  func testV3CandidateDriftRetainsSurfaceOnlyCompatibility() throws {
    let reference = [
      ABProbeV4Candidate(text: "候補", rank: 1, consumingCount: 3),
    ]
    let boundaryOnlyDrift = [
      ABProbeV4Candidate(text: "候補", rank: 1, consumingCount: 2),
    ]

    XCTAssertNoThrow(
      try ABProbeCandidateObservation.validateStable(
        reference: reference,
        observed: boundaryOnlyDrift,
        resultSchema: .v3,
        caseID: "case"
      )
    )
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

  private func sampleV3Result(candidates: [String]) -> ABProbeResult {
    ABProbeResult(
      id: "case",
      reading: "よみ",
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
      topK: 7,
      corpus: ABProbeCorpusProvenance(
        sha256: "sha256:" + String(repeating: "1", count: 64),
        cases: 15
      ),
      candidates: candidates,
      measurement: ABProbeMeasurement(
        warmups: 0,
        iterations: 1,
        latencyMilliseconds: ABProbeLatency.summarize([1]),
        residentMemory: ABProbeMemory(beforeKiB: 1, afterKiB: 2),
        backendDiagnostics: ABProbeBackendDiagnosticsResult(
          processLaunchCount: nil,
          cleanupFailureCount: nil
        )
      )
    )
  }

  private func digest(_ data: Data) -> String {
    var hasher = ABProbeSHA256()
    hasher.update(data)
    return hasher.finalize().map { String(format: "%02x", $0) }.joined()
  }
}
