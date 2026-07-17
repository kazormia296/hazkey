import Foundation
import XCTest
#if os(Linux)
import Glibc
#else
import Darwin
#endif

@testable import hazkey_server

private final class ABProbeFullCompositionRecordingConverter: KanaKanjiConverting {
  let output: ConversionOutput
  private(set) var candidateCallCount = 0
  private(set) var segmentCallCount = 0
  private(set) var lastComposition: CompositionInput?
  private(set) var lastOptions: ConversionOptions?

  init(output: ConversionOutput) {
    self.output = output
  }

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    candidateCallCount += 1
    lastComposition = composition
    lastOptions = options
    return output
  }

  func segmentCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    segmentCallCount += 1
    return ConversionOutput(candidates: [], pageSize: 0)
  }

  func setCompletedData(_ candidate: ConverterCandidate) {}
  func updateLearningData(_ candidate: ConverterCandidate) {}
  func commitLearning() {}
  func forget(_ candidate: ConverterCandidate) {}
  func stopComposition() {}
}

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

    let v6Options = try ABProbeOptions.parse(
      arguments: baseArguments + ["--result-schema", "v6"]
    )
    XCTAssertEqual(v6Options.resultSchema, .v6)
    XCTAssertEqual(v6Options.resultSchema.conversionPath, .segmentCandidates)
  }

  func testOptionsParseContextualZenzaiV7Policy() throws {
    let options = try ABProbeOptions.parse(arguments: [
      "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
      "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
      "--result-schema", "v7", "--zenzai-model", "/tmp/zenzai.gguf",
      "--left-contexts", "/tmp/context.jsonl", "--iterations", "1",
    ])

    XCTAssertEqual(options.resultSchema, .v7)
    XCTAssertEqual(options.converterBackend, .hazkey)
    XCTAssertEqual(options.leftContextsPath, "/tmp/context.jsonl")
    XCTAssertEqual(options.zenzaiModelPath, "/tmp/zenzai.gguf")
    XCTAssertEqual(options.zenzaiInferenceLimit, 10)
    XCTAssertEqual(options.boundaryMode, .isolatedDictionary)
    XCTAssertEqual(options.conversionPath, .segmentCandidates)

    let native = try ABProbeOptions.parse(arguments: [
      "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
      "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
      "--result-schema", "v7", "--zenzai-model", "/tmp/zenzai.gguf",
      "--left-contexts", "/tmp/context.jsonl", "--iterations", "1",
      "--boundary-mode", "native_zenzai_first_clause",
    ])
    XCTAssertEqual(native.boundaryMode, .nativeZenzaiFirstClause)
    XCTAssertEqual(native.conversionPath, .nativeSegmentCandidates)

    let fixed = try ABProbeOptions.parse(arguments: [
      "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
      "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
      "--result-schema", "v7", "--zenzai-model", "/tmp/zenzai.gguf",
      "--left-contexts", "/tmp/context.jsonl", "--iterations", "1",
      "--boundary-mode", "mozc_fixed", "--mozc-fixed-boundaries",
      "/tmp/fixed.jsonl",
    ])
    XCTAssertEqual(fixed.boundaryMode, .mozcFixed)
    XCTAssertEqual(fixed.mozcFixedBoundariesPath, "/tmp/fixed.jsonl")
    XCTAssertEqual(fixed.conversionPath, .mozcFixedSegmentCandidates)

    let fullComposition = try ABProbeOptions.parse(arguments: [
      "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
      "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
      "--result-schema", "v7", "--zenzai-model", "/tmp/zenzai.gguf",
      "--left-contexts", "/tmp/context.jsonl", "--iterations", "1",
      "--boundary-mode", "full_composition",
    ])
    XCTAssertEqual(fullComposition.boundaryMode, .fullComposition)
    XCTAssertNil(fullComposition.mozcFixedBoundariesPath)
    XCTAssertEqual(
      fullComposition.conversionPath,
      .fullCompositionCandidates
    )
  }

  func testOptionsParseExplicitZenzaiV6Policy() throws {
    let options = try ABProbeOptions.parse(arguments: [
      "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
      "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
      "--result-schema", "v6", "--zenzai-model", "/tmp/zenzai.gguf",
      "--iterations", "1",
      "--zenzai-inference-limit", "7", "--zenzai-device", "Vulkan0",
    ])

    XCTAssertEqual(options.resultSchema, .v6)
    XCTAssertEqual(options.converterBackend, .hazkey)
    XCTAssertEqual(options.zenzaiModelPath, "/tmp/zenzai.gguf")
    XCTAssertEqual(options.zenzaiInferenceLimit, 7)
    XCTAssertEqual(options.zenzaiDevice, "Vulkan0")

    let defaultLimit = try ABProbeOptions.parse(arguments: [
      "hazkey-server", "--ab-probe", "--corpus", "/tmp/corpus.tsv",
      "--dictionary", "/tmp/dictionary", "--source-ref", "abc123",
      "--result-schema", "v6", "--zenzai-model", "/tmp/zenzai.gguf",
      "--iterations", "1",
    ])
    XCTAssertEqual(defaultLimit.zenzaiInferenceLimit, 10)
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
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--zenzai-model", "model.gguf",
        ],
        .invalidArguments("--zenzai-model requires --result-schema v6 or v7")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--source-ref", "ref", "--converter-backend", "mozc",
          "--mozc-bundle", "bundle", "--result-schema", "v6",
          "--zenzai-model", "model.gguf",
        ],
        .invalidArguments("--zenzai-model requires --converter-backend hazkey")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v6", "--zenzai-device", "CPU",
        ],
        .invalidArguments("--zenzai-device requires --zenzai-model")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v6", "--zenzai-model", "model.gguf",
        ],
        .invalidArguments("--zenzai-model requires --iterations 1")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v7", "--zenzai-model", "model.gguf",
          "--iterations", "1",
        ],
        .invalidArguments("--result-schema v7 requires --left-contexts")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v6", "--zenzai-model", "model.gguf",
          "--iterations", "1", "--left-contexts", "contexts.jsonl",
        ],
        .invalidArguments("--left-contexts requires --result-schema v7")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v6", "--zenzai-model", "model.gguf",
          "--iterations", "1", "--boundary-mode", "native_zenzai_first_clause",
        ],
        .invalidArguments(
          "native_zenzai_first_clause requires --result-schema v7"
        )
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v7", "--zenzai-model", "model.gguf",
          "--iterations", "1", "--left-contexts", "contexts.jsonl",
          "--boundary-mode", "mozc_fixed",
        ],
        .invalidArguments("mozc_fixed requires --mozc-fixed-boundaries")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v7", "--zenzai-model", "model.gguf",
          "--iterations", "1", "--left-contexts", "contexts.jsonl",
          "--mozc-fixed-boundaries", "fixed.jsonl",
        ],
        .invalidArguments(
          "--mozc-fixed-boundaries requires --boundary-mode mozc_fixed"
        )
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v6", "--zenzai-model", "model.gguf",
          "--iterations", "1", "--boundary-mode", "mozc_fixed",
          "--mozc-fixed-boundaries", "fixed.jsonl",
        ],
        .invalidArguments("mozc_fixed requires --result-schema v7")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v6", "--zenzai-model", "model.gguf",
          "--iterations", "1", "--boundary-mode", "full_composition",
        ],
        .invalidArguments("full_composition requires --result-schema v7")
      ),
      (
        [
          "hazkey-server", "--ab-probe", "--corpus", "x",
          "--dictionary", "dict", "--source-ref", "ref",
          "--result-schema", "v7", "--zenzai-model", "model.gguf",
          "--iterations", "1", "--left-contexts", "contexts.jsonl",
          "--boundary-mode", "full_composition",
          "--mozc-fixed-boundaries", "fixed.jsonl",
        ],
        .invalidArguments(
          "--mozc-fixed-boundaries requires --boundary-mode mozc_fixed"
        )
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
        "--boundary-mode", "future",
      ],
      [
        "hazkey-server", "--ab-probe", "--corpus", "x",
        "--dictionary", "dict", "--source-ref", "ref",
        "--result-schema", "v7",
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
      "{metadata\tid\treading\tcategory\nvalue\tcase\tよみ\tsample\n".utf8
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

  func testSegmentProbeJSONLPreservesExplicitElementsAndV5SpanCount() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let corpus = directory.appendingPathComponent("segment-probe.jsonl")
    let bytes = Data(
      (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"きょう","input_style":"direct"},{"text":"🇯🇵","input_style":"direct"}]}"#
        + "\n").utf8
    )
    try bytes.write(to: corpus)

    let snapshot = try ABProbeCorpus.loadSnapshot(path: corpus.path)
    let testCase = try XCTUnwrap(snapshot.cases.first)
    let expectedElements = [
      CompositionElement(text: "きょう", inputStyle: .direct),
      CompositionElement(text: "🇯🇵", inputStyle: .direct),
    ]

    XCTAssertEqual(snapshot.cases.count, 1)
    XCTAssertEqual(testCase.id, "case")
    XCTAssertEqual(testCase.category, "sample")
    XCTAssertEqual(testCase.reading, "きょう🇯🇵")
    XCTAssertEqual(testCase.elements, expectedElements)
    XCTAssertEqual(testCase.elements[1].text.unicodeScalars.count, 2)
    XCTAssertEqual(testCase.composition.elements, expectedElements)
    XCTAssertEqual(testCase.composition.cursor, expectedElements.count)
    XCTAssertEqual(testCase.composition.leftContext, "")
    XCTAssertEqual(
      ABProbeCompositionSpan.entireComposition(testCase.composition),
      ABProbeCompositionSpan(
        start: 0,
        count: 2,
        unit: "composition_element"
      )
    )
    XCTAssertEqual(snapshot.provenance.sha256, "sha256:" + digest(bytes))
    XCTAssertEqual(snapshot.provenance.cases, 1)
  }

  func testLeftContextSidecarBindsExactBytesAndPerCaseEvidence() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let sidecar = directory.appendingPathComponent("context.jsonl")
    let sourceHash = "sha256:" + String(repeating: "a", count: 64)
    let leftContext = "昨日は晴れでした。"
    let contextHash = "sha256:" + digest(Data(leftContext.utf8))
    let emptyHash = "sha256:" + digest(Data())
    let bytes = Data(
      (
        #"{"id":"case-1","left_context":"昨日は晴れでした。","left_context_sha256":""#
          + contextHash
          + #"","schema":"hazkey.blind-silver-left-context.v1","source_content_sha256":""#
          + sourceHash
          + #""}"#
          + "\n"
          + #"{"id":"case-2","left_context":"","left_context_sha256":""#
          + emptyHash
          + #"","schema":"hazkey.blind-silver-left-context.v1","source_content_sha256":""#
          + sourceHash
          + #""}"#
          + "\n"
      ).utf8
    )
    try bytes.write(to: sidecar)
    let cases = [
      ABProbeCorpusCase(id: "case-1", reading: "あめ", category: "sample"),
      ABProbeCorpusCase(id: "case-2", reading: "はれ", category: "sample"),
    ]

    let snapshot = try ABProbeLeftContexts.load(path: sidecar.path, cases: cases)
    XCTAssertEqual(snapshot.source.schema, "hazkey.blind-silver-left-context.v1")
    XCTAssertEqual(snapshot.source.sha256, "sha256:" + digest(bytes))
    XCTAssertEqual(snapshot.source.cases, 2)
    XCTAssertEqual(snapshot.fileIdentity.sizeBytes, bytes.count)

    let contextual = try XCTUnwrap(snapshot.entriesByID["case-1"])
    XCTAssertEqual(contextual.leftContext, leftContext)
    XCTAssertEqual(
      contextual.evidence(source: snapshot.source),
      ABProbeLeftContextEvidence(
        mode: "natural_left",
        leftContextSHA256: contextHash,
        leftContextCodePointCount: leftContext.unicodeScalars.count,
        leftContextUTF8ByteCount: leftContext.utf8.count,
        sourceContentSHA256: sourceHash,
        source: snapshot.source
      )
    )
    XCTAssertEqual(
      try XCTUnwrap(snapshot.entriesByID["case-2"])
        .evidence(source: snapshot.source).mode,
      "empty"
    )
    XCTAssertEqual(
      cases[0].composition(leftContext: leftContext).leftContext,
      leftContext
    )
  }

  func testLeftContextSidecarRejectsHashAndCoverageButAllowsEmptyBaseline() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let sidecar = directory.appendingPathComponent("context.jsonl")
    let sourceHash = "sha256:" + String(repeating: "a", count: 64)
    let cases = [
      ABProbeCorpusCase(id: "case", reading: "あめ", category: "sample"),
    ]
    let invalidLines = [
      #"{"id":"case","left_context":"文脈","left_context_sha256":"sha256:"#
        + String(repeating: "0", count: 64)
        + #"","schema":"hazkey.blind-silver-left-context.v1","source_content_sha256":""#
        + sourceHash + #""}"#,
      #"{"id":"other","left_context":"文脈","left_context_sha256":"sha256:"#
        + digest(Data("文脈".utf8))
        + #"","schema":"hazkey.blind-silver-left-context.v1","source_content_sha256":""#
        + sourceHash + #""}"#,
    ]

    for line in invalidLines {
      try Data((line + "\n").utf8).write(to: sidecar)
      XCTAssertThrowsError(try ABProbeLeftContexts.load(path: sidecar.path, cases: cases))
    }

    let emptyLine = #"{"id":"case","left_context":"","left_context_sha256":"sha256:"#
      + digest(Data())
      + #"","schema":"hazkey.blind-silver-left-context.v1","source_content_sha256":""#
      + sourceHash + #""}"#
    try Data((emptyLine + "\n").utf8).write(to: sidecar)
    let emptySnapshot = try ABProbeLeftContexts.load(
      path: sidecar.path,
      cases: cases
    )
    XCTAssertEqual(
      try XCTUnwrap(emptySnapshot.entriesByID["case"])
        .evidence(source: emptySnapshot.source).mode,
      "empty"
    )
  }

  func testMozcFixedBoundarySidecarBindsExactBytesOrderReadingAndOrigin() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let sidecar = directory.appendingPathComponent("fixed.jsonl")
    let rawMozcHash = "sha256:" + String(repeating: "a", count: 64)
    let reading1 = "きょうは"
    let reading2 = "あめ"
    let readingHash1 = "sha256:" + digest(Data(reading1.utf8))
    let readingHash2 = "sha256:" + digest(Data(reading2.utf8))
    let origin = #"{"cases":2,"conversion_path":"segment_candidates","converter_backend":"mozc","schema":"hazkey.ab-probe-result.v6","sha256":""#
      + rawMozcHash + #""}"#
    let bytes = Data(
      (
        #"{"consuming_count":3,"id":"case-1","origin":"#
          + origin
          + #", "reading":"きょうは","reading_sha256":""#
          + readingHash1
          + #"","schema":"hazkey.mozc-fixed-boundary.v1"}"#
          + "\n"
          + #"{"consuming_count":2,"id":"case-2","origin":"#
          + origin
          + #", "reading":"あめ","reading_sha256":""#
          + readingHash2
          + #"","schema":"hazkey.mozc-fixed-boundary.v1"}"#
          + "\n"
      ).utf8
    )
    try bytes.write(to: sidecar)
    let cases = [
      ABProbeCorpusCase(id: "case-1", reading: reading1, category: "sample"),
      ABProbeCorpusCase(id: "case-2", reading: reading2, category: "sample"),
    ]

    let snapshot = try ABProbeMozcFixedBoundaries.load(
      path: sidecar.path,
      cases: cases
    )
    XCTAssertEqual(snapshot.source.schema, "hazkey.mozc-fixed-boundary.v1")
    XCTAssertEqual(snapshot.source.sha256, "sha256:" + digest(bytes))
    XCTAssertEqual(snapshot.source.cases, 2)
    XCTAssertEqual(snapshot.origin.sha256, rawMozcHash)
    XCTAssertEqual(snapshot.origin.schema, "hazkey.ab-probe-result.v6")
    let entry = try XCTUnwrap(snapshot.entriesByID["case-1"])
    XCTAssertEqual(entry.reading, reading1)
    XCTAssertEqual(entry.consumingCount, 3)
    XCTAssertEqual(
      entry.evidence(source: snapshot.source),
      ABProbeFixedBoundaryEvidence(
        readingSHA256: readingHash1,
        consumingCount: 3,
        source: snapshot.source
      )
    )
    XCTAssertEqual(
      cases[0].composition(leftContext: "文脈", targetCount: 3).targetCount,
      3
    )
  }

  func testMozcFixedBoundarySidecarRejectsTamperRangeAndIDOrder() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let sidecar = directory.appendingPathComponent("fixed.jsonl")
    let readingHash = "sha256:" + digest(Data("あめ".utf8))
    let originHash = "sha256:" + String(repeating: "a", count: 64)
    let cases = [
      ABProbeCorpusCase(id: "case", reading: "あめ", category: "sample"),
    ]
    func record(
      id: String = "case",
      reading: String = "あめ",
      hash: String = readingHash,
      count: String = "2",
      originCases: Int = 1,
      extraOrigin: String = ""
    ) -> String {
      "{\"consuming_count\":" + count
        + ",\"id\":\"" + id
        + "\",\"origin\":{\"cases\":" + String(originCases)
        + ",\"conversion_path\":\"segment_candidates\""
        + ",\"converter_backend\":\"mozc\"" + extraOrigin
        + ",\"schema\":\"hazkey.ab-probe-result.v6\""
        + ",\"sha256\":\"" + originHash
        + "\"},\"reading\":\"" + reading
        + "\",\"reading_sha256\":\"" + hash
        + "\",\"schema\":\"hazkey.mozc-fixed-boundary.v1\"}"
    }
    let invalid = [
      record(hash: "sha256:" + String(repeating: "0", count: 64)),
      record(count: "0"),
      record(count: "3"),
      record(id: "other"),
      record(reading: "はれ"),
      record(originCases: 2),
      record(extraOrigin: #", "unexpected":true"#),
      record(count: "true"),
    ]
    for line in invalid {
      try Data((line + "\n").utf8).write(to: sidecar)
      XCTAssertThrowsError(
        try ABProbeMozcFixedBoundaries.load(path: sidecar.path, cases: cases),
        line
      )
    }

    let secondHash = "sha256:" + digest(Data("はれ".utf8))
    let reordered = record(
      id: "case-2",
      reading: "はれ",
      hash: secondHash,
      originCases: 2
    ) + "\n" + record(originCases: 2) + "\n"
    try Data(reordered.utf8).write(to: sidecar)
    XCTAssertThrowsError(
      try ABProbeMozcFixedBoundaries.load(
        path: sidecar.path,
        cases: cases + [
          ABProbeCorpusCase(id: "case-2", reading: "はれ", category: "sample"),
        ]
      )
    )
  }

  func testSegmentProbeJSONLRejectsNonExactOrUnsafeRecords() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let corpus = directory.appendingPathComponent("segment-probe.jsonl")
    let valid = #"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"よみ","input_style":"direct"}]}"#
    let second = #"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case-2","category":"sample","elements":[{"text":"べつ","input_style":"direct"}]}"#
    let decomposed = "は\u{3099}"
    var withBOM = Data([0xEF, 0xBB, 0xBF])
    withBOM.append(Data((valid + "\n").utf8))
    let invalidInputs: [(String, Data)] = [
      ("BOM", withBOM),
      ("CRLF", Data((valid + "\r\n").utf8)),
      ("empty line", Data((valid + "\n\n" + second + "\n").utf8)),
      (
        "unknown root field",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"よみ","input_style":"direct"}],"expected":"読み"}"#
            + "\n").utf8
        )
      ),
      (
        "unknown element field",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"よみ","input_style":"direct","count":1}]}"#
            + "\n").utf8
        )
      ),
      (
        "duplicate root key",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","id":"case-2","category":"sample","elements":[{"text":"よみ","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "duplicate element key",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"よみ","text":"べつ","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "escaped duplicate root key",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","\u0069d":"case-2","category":"sample","elements":[{"text":"よみ","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "empty id",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"","category":"sample","elements":[{"text":"よみ","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "empty category",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"","elements":[{"text":"よみ","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "empty element text",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "non-NFC element text",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"\#(decomposed)","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "control character",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"\u0001","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "non-direct input style",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[{"text":"よみ","input_style":"mapped"}]}"#
            + "\n").utf8
        )
      ),
      (
        "duplicate id",
        Data((valid + "\n" + valid + "\n").utf8)
      ),
      (
        "empty elements",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":[]}"#
            + "\n").utf8
        )
      ),
      (
        "wrong schema",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v2","id":"case","category":"sample","elements":[{"text":"よみ","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      (
        "missing root field",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","elements":[{"text":"よみ","input_style":"direct"}]}"#
            + "\n").utf8
        )
      ),
      ("non-object root", Data("[]\n".utf8)),
      (
        "non-object element",
        Data(
          (#"{"schema":"hazkey.mozc-hybrid-segment-probe-input.v1","id":"case","category":"sample","elements":["よみ"]}"#
            + "\n").utf8
        )
      ),
    ]

    for (name, data) in invalidInputs {
      try data.write(to: corpus, options: .atomic)
      XCTAssertThrowsError(try ABProbeCorpus.load(path: corpus.path), name) {
        guard let error = $0 as? ABProbeError,
              case .invalidCorpus = error
        else {
          XCTFail("\(name): expected invalidCorpus, got \($0)")
          return
        }
      }
    }
  }

  func testJSONDuplicateKeyValidatorTraversesNestedObjectsAndArrays() throws {
    let valid = Data(
      #"{"items":[{"id":1,"payload":{"value":true}},{"id":2,"payload":{"value":false}}],"metadata":{"count":2,"tags":["a",{"name":"b"}]}}"#.utf8
    )
    XCTAssertNoThrow(try ABProbeJSONDuplicateKeyValidator.validate(valid))

    let escapedNestedDuplicate = Data(
      #"{"items":[{"payload":{"value":true,"v\u0061lue":false}}]}"#.utf8
    )
    XCTAssertThrowsError(
      try ABProbeJSONDuplicateKeyValidator.validate(escapedNestedDuplicate)
    ) {
      XCTAssertEqual(
        $0 as? ABProbeJSONKeyValidationError,
        .duplicateKey
      )
    }
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

  func testFileIdentityHashesCanonicalFileAndFailsClosedAfterMutation() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
      UUID().uuidString,
      isDirectory: true
    )
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    let file = directory.appendingPathComponent("model.gguf")
    let alias = directory.appendingPathComponent("model-alias.gguf")
    let original = Data("model bytes".utf8)
    try original.write(to: file)
    try FileManager.default.createSymbolicLink(
      at: alias,
      withDestinationURL: file
    )

    let identity = try ABProbeFileIdentity.capture(
      path: alias.path,
      label: "test model"
    )
    XCTAssertEqual(identity.path, file.path)
    XCTAssertEqual(identity.sizeBytes, original.count)
    XCTAssertEqual(identity.sha256, "sha256:" + digest(original))
    XCTAssertNoThrow(try identity.revalidate(label: "test model"))

    try Data("changed bytes".utf8).write(to: file)
    XCTAssertThrowsError(try identity.revalidate(label: "test model")) {
      guard let error = $0 as? ABProbeError,
            case .backendInstability(let message) = error else {
        XCTFail("unexpected error: \($0)")
        return
      }
      XCTAssertTrue(message.contains("identity changed"))
    }
  }

  func testZenzaiEvidenceValidationRequiresVerifiedLoadAndObservedScore() throws {
    let modelURL = URL(fileURLWithPath: "/canonical/zenzai.gguf")
    let diagnosticsStore = ZenzaiRuntimeDiagnosticsStore()
    diagnosticsStore.record(
      decision: .enabled(modelURL: modelURL),
      converterStatus: "load \(modelURL.absoluteString)"
    )
    let diagnostics = diagnosticsStore.snapshot()

    XCTAssertNoThrow(
      try ABProbeZenzaiEvidenceValidation.validate(
        requested: true,
        observedScoreCount: 1,
        diagnostics: diagnostics
      )
    )
    XCTAssertThrowsError(
      try ABProbeZenzaiEvidenceValidation.validate(
        requested: true,
        observedScoreCount: 0,
        diagnostics: diagnostics
      )
    ) {
      XCTAssertEqual(
        $0 as? ABProbeError,
        .backendInstability(
          "Zenzai was requested but no candidate evaluation score was observed"
        )
      )
    }
    XCTAssertThrowsError(
      try ABProbeZenzaiEvidenceValidation.validate(
        requested: true,
        observedScoreCount: 1,
        diagnostics: nil
      )
    )
    XCTAssertNoThrow(
      try ABProbeZenzaiEvidenceValidation.validate(
        requested: false,
        observedScoreCount: 0,
        diagnostics: nil
      )
    )
  }

  func testV7ZenzaiEvidenceAllowsNullScoresButRequiresAnEvaluationAttempt() throws {
    let modelURL = URL(fileURLWithPath: "/canonical/zenzai.gguf")
    let diagnosticsStore = ZenzaiRuntimeDiagnosticsStore()
    diagnosticsStore.record(
      decision: .enabled(modelURL: modelURL),
      converterStatus: "load \(modelURL.absoluteString)"
    )
    let diagnostics = diagnosticsStore.snapshot()
    let validEvidence = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 1,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )

    XCTAssertNoThrow(
      try ABProbeZenzaiEvidenceValidation.validate(
        requested: true,
        requiresObservedCandidateScore: false,
        requiresExecutionEvidence: true,
        observedScoreCount: 0,
        executionEvidence: [validEvidence],
        diagnostics: diagnostics
      )
    )

    let noCandidate = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 0,
      attemptOutcomes: .zero,
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 0,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 1
      )
    )
    XCTAssertNoThrow(
      try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
        noCandidate,
        caseID: "no-candidate"
      )
    )
    XCTAssertThrowsError(
      try ABProbeZenzaiEvidenceValidation.validate(
        requested: true,
        requiresObservedCandidateScore: false,
        requiresExecutionEvidence: true,
        observedScoreCount: 0,
        executionEvidence: [noCandidate],
        diagnostics: diagnostics
      )
    ) {
      XCTAssertEqual(
        $0 as? ABProbeError,
        .backendInstability(
          "Zenzai was requested but no model evaluation attempt was observed"
        )
      )
    }
  }

  func testZenzaiExecutionEvidenceValidatesTotalsButAllowsFailureOutcomes() throws {
    let failureEvidence = [
      ZenzaiExecutionEvidence(
        requestCount: 1,
        evaluationAttemptCount: 1,
        attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
          pass: 0,
          fixRequired: 0,
          wholeResult: 0,
          error: 1
        ),
        terminalOutcomes: ZenzaiTerminalOutcomeCounts(
          pass: 0,
          fixRequired: 0,
          wholeResult: 0,
          error: 1,
          inferenceLimit: 0,
          noCandidate: 0
        )
      ),
      ZenzaiExecutionEvidence(
        requestCount: 1,
        evaluationAttemptCount: 2,
        attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
          pass: 1,
          fixRequired: 1,
          wholeResult: 0,
          error: 0
        ),
        terminalOutcomes: ZenzaiTerminalOutcomeCounts(
          pass: 0,
          fixRequired: 0,
          wholeResult: 0,
          error: 0,
          inferenceLimit: 1,
          noCandidate: 0
        )
      ),
      ZenzaiExecutionEvidence(
        requestCount: 1,
        evaluationAttemptCount: 0,
        attemptOutcomes: .zero,
        terminalOutcomes: ZenzaiTerminalOutcomeCounts(
          pass: 0,
          fixRequired: 0,
          wholeResult: 0,
          error: 0,
          inferenceLimit: 0,
          noCandidate: 1
        )
      ),
    ]
    for (index, evidence) in failureEvidence.enumerated() {
      XCTAssertNoThrow(
        try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
          evidence,
          caseID: "failure-\(index)"
        )
      )
    }

    let malformed = [
      ZenzaiExecutionEvidence(
        requestCount: 1,
        evaluationAttemptCount: 0,
        attemptOutcomes: .zero,
        terminalOutcomes: ZenzaiTerminalOutcomeCounts(
          pass: 1,
          fixRequired: 0,
          wholeResult: 0,
          error: 0,
          inferenceLimit: 0,
          noCandidate: 0
        )
      ),
      ZenzaiExecutionEvidence(
        requestCount: 1,
        evaluationAttemptCount: 1,
        attemptOutcomes: .zero,
        terminalOutcomes: ZenzaiTerminalOutcomeCounts(
          pass: 1,
          fixRequired: 0,
          wholeResult: 0,
          error: 0,
          inferenceLimit: 0,
          noCandidate: 0
        )
      ),
      ZenzaiExecutionEvidence(
        requestCount: 1,
        evaluationAttemptCount: 1,
        attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
          pass: 1,
          fixRequired: 0,
          wholeResult: 0,
          error: 0
        ),
        terminalOutcomes: .zero
      ),
    ]
    for (index, evidence) in malformed.enumerated() {
      XCTAssertThrowsError(
        try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
          evidence,
          caseID: "malformed-\(index)"
        )
      ) {
        XCTAssertEqual(
          $0 as? ABProbeError,
          .backendInstability(
            "invalid Zenzai execution evidence for case malformed-\(index)"
          )
        )
      }
    }
  }

  func testZenzaiExecutionEvidenceEnforcesBoundaryModeRequestsAndInferenceLimit() throws {
    let isolated = ZenzaiExecutionEvidence(
      requestCount: 2,
      evaluationAttemptCount: 2,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 2,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 2,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    XCTAssertNoThrow(
      try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
        isolated,
        caseID: "isolated",
        boundaryMode: .isolatedDictionary,
        inferenceLimit: 1
      )
    )
    XCTAssertThrowsError(
      try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
        isolated,
        caseID: "wrong-mode",
        boundaryMode: .nativeZenzaiFirstClause,
        inferenceLimit: 10
      )
    )

    let overLimit = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 2,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 2,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    XCTAssertThrowsError(
      try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
        overLimit,
        caseID: "over-limit",
        boundaryMode: .mozcFixed,
        inferenceLimit: 1
      )
    )

    let fullComposition = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 1,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    XCTAssertNoThrow(
      try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
        fullComposition,
        caseID: "full-composition",
        boundaryMode: .fullComposition,
        inferenceLimit: 1
      )
    )
    XCTAssertThrowsError(
      try ABProbeZenzaiEvidenceValidation.validateExecutionEvidence(
        isolated,
        caseID: "full-composition-wrong-count",
        boundaryMode: .fullComposition,
        inferenceLimit: 10
      )
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

  func testV6ResultAddsCandidateEvidenceProducerAndQualityPolicy() throws {
    let result = ABProbeResultV6(
      v3: sampleV3Result(candidates: ["第一", "第二"]),
      candidates: [
        ABProbeV6Candidate(
          text: "第一",
          rank: 1,
          consumingCount: 2,
          provenance: CandidateProvenance.standard.rawValue,
          rankingInfluence: CandidateRankingInfluence.zenzai.rawValue,
          zenzaiScore: -1.25,
          zenzaiScoreTokenCount: 3,
          zenzaiScoreScope: ZenzaiScoreScope.fullCandidate.rawValue
        ),
        ABProbeV6Candidate(
          text: "第二",
          rank: 2,
          consumingCount: 2,
          provenance: CandidateProvenance.projectDictionary.rawValue,
          rankingInfluence: CandidateRankingInfluence.zenzai.rawValue,
          zenzaiScore: nil,
          zenzaiScoreTokenCount: nil,
          zenzaiScoreScope: nil
        ),
      ],
      compositionSpan: ABProbeCompositionSpan(
        start: 0,
        count: 3,
        unit: "composition_element"
      ),
      producer: ABProbeFileIdentity(
        path: "/canonical/hazkey-server",
        sizeBytes: 123,
        sha256: "sha256:" + String(repeating: "a", count: 64)
      ),
      qualityPolicy: ABProbeQualityPolicy(
        zenzai: ABProbeZenzaiQualityPolicy(
          enabled: true,
          modelPath: "/canonical/zenzai.gguf",
          modelSizeBytes: 456,
          modelSHA256: "sha256:" + String(repeating: "b", count: 64),
          inferenceLimit: 7,
          resolvedDevice: "Vulkan0"
        )
      )
    )
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: JSONEncoder().encode(result))
        as? [String: Any]
    )

    XCTAssertEqual(object["schema"] as? String, "hazkey.ab-probe-result.v6")
    XCTAssertEqual(object["converter_backend"] as? String, "hazkey")
    XCTAssertEqual(object["conversion_path"] as? String, "segment_candidates")
    XCTAssertEqual(
      Set(object.keys),
      Set([
        "schema", "conversion_path", "id", "reading", "category", "backend",
        "backend_version", "converter_backend", "source_ref", "resource", "top_k",
        "corpus", "candidates", "composition_span", "producer", "quality_policy",
        "measurement",
      ])
    )
    let candidates = try XCTUnwrap(object["candidates"] as? [[String: Any]])
    XCTAssertEqual(
      Set(candidates[0].keys),
      Set([
        "text", "rank", "consuming_count", "provenance", "ranking_influence",
        "zenzai_score", "zenzai_score_token_count", "zenzai_score_scope",
      ])
    )
    XCTAssertEqual(candidates[0]["provenance"] as? String, "standard")
    XCTAssertEqual(candidates[0]["ranking_influence"] as? String, "zenzai")
    XCTAssertEqual(candidates[0]["zenzai_score"] as? Double, -1.25)
    XCTAssertEqual(candidates[0]["zenzai_score_token_count"] as? Int, 3)
    XCTAssertEqual(candidates[0]["zenzai_score_scope"] as? String, "full_candidate")
    XCTAssertTrue(candidates[1]["zenzai_score"] is NSNull)
    XCTAssertTrue(candidates[1]["zenzai_score_token_count"] is NSNull)
    XCTAssertTrue(candidates[1]["zenzai_score_scope"] is NSNull)

    let producer = try XCTUnwrap(object["producer"] as? [String: Any])
    XCTAssertEqual(Set(producer.keys), Set(["path", "size_bytes", "sha256"]))
    XCTAssertEqual(producer["size_bytes"] as? Int, 123)

    let policy = try XCTUnwrap(object["quality_policy"] as? [String: Any])
    XCTAssertEqual(Set(policy.keys), Set(["learning", "context", "zenzai"]))
    XCTAssertEqual(policy["learning"] as? Bool, false)
    XCTAssertEqual(policy["context"] as? String, "empty")
    let zenzai = try XCTUnwrap(policy["zenzai"] as? [String: Any])
    XCTAssertEqual(
      Set(zenzai.keys),
      Set([
        "enabled", "model_path", "model_size_bytes", "model_sha256",
        "inference_limit", "resolved_device",
      ])
    )
    XCTAssertEqual(zenzai["enabled"] as? Bool, true)
    XCTAssertEqual(zenzai["model_path"] as? String, "/canonical/zenzai.gguf")
    XCTAssertEqual(zenzai["inference_limit"] as? Int, 7)
    XCTAssertEqual(zenzai["resolved_device"] as? String, "Vulkan0")
  }

  func testV6DisabledZenzaiPolicyEmitsExplicitNulls() throws {
    let policy = ABProbeQualityPolicy(
      zenzai: ABProbeZenzaiQualityPolicy(
        enabled: false,
        modelPath: nil,
        modelSizeBytes: nil,
        modelSHA256: nil,
        inferenceLimit: nil,
        resolvedDevice: nil
      )
    )
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: JSONEncoder().encode(policy))
        as? [String: Any]
    )
    let zenzai = try XCTUnwrap(object["zenzai"] as? [String: Any])

    XCTAssertEqual(zenzai["enabled"] as? Bool, false)
    for key in [
      "model_path", "model_size_bytes", "model_sha256", "inference_limit",
      "resolved_device",
    ] {
      XCTAssertTrue(zenzai[key] is NSNull, key)
    }
  }

  func testV7ResultAddsHashedLeftContextEvidenceWithoutRawText() throws {
    let source = ABProbeLeftContextSource(
      schema: "hazkey.blind-silver-left-context.v1",
      sha256: "sha256:" + String(repeating: "c", count: 64),
      cases: 15
    )
    let result = ABProbeResultV7(
      v3: sampleV3Result(candidates: ["雨"]),
      candidates: [
        ABProbeV6Candidate(
          text: "雨",
          rank: 1,
          consumingCount: 2,
          provenance: CandidateProvenance.standard.rawValue,
          rankingInfluence: CandidateRankingInfluence.zenzai.rawValue,
          zenzaiScore: -0.75,
          zenzaiScoreTokenCount: 2,
          zenzaiScoreScope: ZenzaiScoreScope.fullCandidate.rawValue
        ),
      ],
      compositionSpan: ABProbeCompositionSpan(
        start: 0,
        count: 2,
        unit: "composition_element"
      ),
      producer: ABProbeFileIdentity(
        path: "/canonical/hazkey-server",
        sizeBytes: 123,
        sha256: "sha256:" + String(repeating: "a", count: 64)
      ),
      qualityPolicy: ABProbeQualityPolicy(
        context: "left_context_sidecar",
        zenzai: ABProbeZenzaiQualityPolicy(
          enabled: true,
          modelPath: "/canonical/zenzai.gguf",
          modelSizeBytes: 456,
          modelSHA256: "sha256:" + String(repeating: "b", count: 64),
          inferenceLimit: 7,
          resolvedDevice: "Vulkan0"
        )
      ),
      boundaryPolicy: ABProbeBoundaryPolicy(mode: .nativeZenzaiFirstClause),
      conversionPath: .nativeSegmentCandidates,
      context: ABProbeLeftContextEvidence(
        mode: "natural_left",
        leftContextSHA256: "sha256:" + String(repeating: "d", count: 64),
        leftContextCodePointCount: 8,
        leftContextUTF8ByteCount: 24,
        sourceContentSHA256: "sha256:" + String(repeating: "e", count: 64),
        source: source
      ),
      zenzaiExecution: ZenzaiExecutionEvidence(
        requestCount: 1,
        evaluationAttemptCount: 1,
        attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
          pass: 1,
          fixRequired: 0,
          wholeResult: 0,
          error: 0
        ),
        terminalOutcomes: ZenzaiTerminalOutcomeCounts(
          pass: 1,
          fixRequired: 0,
          wholeResult: 0,
          error: 0,
          inferenceLimit: 0,
          noCandidate: 0
        )
      )
    )
    let encoded = try JSONEncoder().encode(result)
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: encoded) as? [String: Any]
    )

    XCTAssertEqual(object["schema"] as? String, "hazkey.ab-probe-result.v7")
    XCTAssertEqual(
      Set(object.keys),
      Set([
        "schema", "conversion_path", "id", "reading", "category", "backend",
        "backend_version", "converter_backend", "source_ref", "resource", "top_k",
        "corpus", "candidates", "composition_span", "producer", "quality_policy",
        "boundary_policy", "context", "fixed_boundary", "zenzai_execution",
        "measurement",
      ])
    )
    XCTAssertTrue(object["fixed_boundary"] is NSNull)
    XCTAssertEqual(
      (object["zenzai_execution"] as? [String: Any])?["request_count"] as? Int,
      1
    )
    let execution = try XCTUnwrap(
      object["zenzai_execution"] as? [String: Any]
    )
    XCTAssertEqual(
      Set(execution.keys),
      Set([
        "request_count", "evaluation_attempt_count", "attempt_outcomes",
        "terminal_outcomes",
      ])
    )
    XCTAssertEqual(execution["evaluation_attempt_count"] as? Int, 1)
    let attemptOutcomes = try XCTUnwrap(
      execution["attempt_outcomes"] as? [String: Any]
    )
    XCTAssertEqual(
      Set(attemptOutcomes.keys),
      Set(["pass", "fix_required", "whole_result", "error"])
    )
    XCTAssertEqual(attemptOutcomes["pass"] as? Int, 1)
    let terminalOutcomes = try XCTUnwrap(
      execution["terminal_outcomes"] as? [String: Any]
    )
    XCTAssertEqual(
      Set(terminalOutcomes.keys),
      Set([
        "pass", "fix_required", "whole_result", "error", "inference_limit",
        "no_candidate",
      ])
    )
    XCTAssertEqual(terminalOutcomes["pass"] as? Int, 1)
    let policy = try XCTUnwrap(object["quality_policy"] as? [String: Any])
    XCTAssertEqual(policy["context"] as? String, "left_context_sidecar")
    XCTAssertEqual(
      object["conversion_path"] as? String,
      "native_segment_candidates"
    )
    let boundary = try XCTUnwrap(object["boundary_policy"] as? [String: Any])
    XCTAssertEqual(
      Set(boundary.keys),
      Set([
        "mode", "boundary_zenzai_enabled", "surface_zenzai_enabled", "source",
      ])
    )
    XCTAssertEqual(boundary["mode"] as? String, "native_zenzai_first_clause")
    XCTAssertEqual(boundary["boundary_zenzai_enabled"] as? Bool, true)
    XCTAssertEqual(boundary["surface_zenzai_enabled"] as? Bool, true)
    XCTAssertEqual(
      boundary["source"] as? String,
      "primary_converter_first_clause_results"
    )
    let context = try XCTUnwrap(object["context"] as? [String: Any])
    XCTAssertEqual(
      Set(context.keys),
      Set([
        "mode", "left_context_sha256", "left_context_code_point_count",
        "left_context_utf8_byte_count", "source_content_sha256", "source",
      ])
    )
    XCTAssertEqual(context["mode"] as? String, "natural_left")
    XCTAssertEqual(context["left_context_code_point_count"] as? Int, 8)
    let encodedText = try XCTUnwrap(String(data: encoded, encoding: .utf8))
    XCTAssertFalse(encodedText.contains("昨日は晴れでした"))
  }

  func testV7MozcFixedPolicyAndEvidenceEncodeExplicitly() throws {
    let source = ABProbeFixedBoundarySource(
      schema: "hazkey.mozc-fixed-boundary.v1",
      sha256: "sha256:" + String(repeating: "a", count: 64),
      cases: 2
    )
    let evidence = ABProbeFixedBoundaryEvidence(
      readingSHA256: "sha256:" + String(repeating: "b", count: 64),
      consumingCount: 3,
      source: source
    )
    let encodedEvidence = try JSONEncoder().encode(
      ABProbeNullableFixedBoundaryEvidence(evidence)
    )
    let object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: encodedEvidence) as? [String: Any]
    )
    XCTAssertEqual(
      Set(object.keys),
      Set(["reading_sha256", "consuming_count", "source"])
    )
    XCTAssertEqual(object["consuming_count"] as? Int, 3)
    XCTAssertEqual(
      (object["source"] as? [String: Any])?["sha256"] as? String,
      source.sha256
    )

    let policy = try XCTUnwrap(
      JSONSerialization.jsonObject(
        with: JSONEncoder().encode(ABProbeBoundaryPolicy(mode: .mozcFixed))
      ) as? [String: Any]
    )
    XCTAssertEqual(policy["mode"] as? String, "mozc_fixed")
    XCTAssertEqual(policy["boundary_zenzai_enabled"] as? Bool, false)
    XCTAssertEqual(policy["surface_zenzai_enabled"] as? Bool, true)
    XCTAssertEqual(
      policy["source"] as? String,
      "mozc_top1_fixed_boundary_sidecar"
    )
  }

  func testV7FullCompositionPolicyEncodesExplicitlyWithoutFixedBoundary() throws {
    let policy = try XCTUnwrap(
      JSONSerialization.jsonObject(
        with: JSONEncoder().encode(
          ABProbeBoundaryPolicy(mode: .fullComposition)
        )
      ) as? [String: Any]
    )
    XCTAssertEqual(policy["mode"] as? String, "full_composition")
    XCTAssertEqual(policy["boundary_zenzai_enabled"] as? Bool, false)
    XCTAssertEqual(policy["surface_zenzai_enabled"] as? Bool, true)
    XCTAssertEqual(policy["source"] as? String, "entire_composition")

    let fixedBoundary = try JSONSerialization.jsonObject(
      with: JSONEncoder().encode(
        ABProbeNullableFixedBoundaryEvidence(nil)
      ),
      options: [.fragmentsAllowed]
    )
    XCTAssertTrue(fixedBoundary is NSNull)
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

  func testFixedBoundaryConstraintDropsPartialCandidatesAndKeepsExecutionEvidence() {
    let evidence = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 1,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    let constrained = ABProbeCandidateObservation.constrain(
      ConversionOutput(
        candidates: [
          ConverterCandidate(text: "今日", consumingCount: 3),
          ConverterCandidate(text: "今日は", consumingCount: 4),
          ConverterCandidate(text: "今日は医者", consumingCount: 7),
        ],
        pageSize: 3,
        zenzaiExecutionEvidence: evidence
      ),
      toFixedBoundary: 4
    )

    XCTAssertEqual(constrained.candidates.map(\.text), ["今日は"])
    XCTAssertEqual(constrained.pageSize, 1)
    XCTAssertEqual(constrained.zenzaiExecutionEvidence, evidence)
  }

  func testFullCompositionPathUsesCandidatesPropagatesContextAndKeepsOnlyFullSpan() throws {
    let evidence = ZenzaiExecutionEvidence(
      requestCount: 1,
      evaluationAttemptCount: 1,
      attemptOutcomes: ZenzaiEvaluationOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0
      ),
      terminalOutcomes: ZenzaiTerminalOutcomeCounts(
        pass: 1,
        fixRequired: 0,
        wholeResult: 0,
        error: 0,
        inferenceLimit: 0,
        noCandidate: 0
      )
    )
    let converter = ABProbeFullCompositionRecordingConverter(
      output: ConversionOutput(
        candidates: [
          ConverterCandidate(text: "部分", consumingCount: 2),
          ConverterCandidate(text: "全文第一", consumingCount: 4),
          ConverterCandidate(text: "超過", consumingCount: 5),
          ConverterCandidate(text: "全文第二", consumingCount: 4),
        ],
        pageSize: 4,
        zenzaiExecutionEvidence: evidence
      )
    )
    let elements = "よみかな".map {
      CompositionElement(text: String($0), inputStyle: .direct)
    }
    let composition = CompositionInput(
      elements: elements,
      cursor: elements.count,
      leftContext: "直前に確定した文脈。"
    )
    let options = ConversionOptions(
      allowLearning: false,
      zenzaiEnabled: true,
      leftContext: composition.leftContext,
      rightContext: "",
      suggestionListMode: .normal,
      suggestionListLimit: 5
    )

    let output = try ABProbeCommand.requestCandidates(
      from: converter,
      for: composition,
      options: options,
      path: .fullCompositionCandidates
    )

    XCTAssertEqual(converter.candidateCallCount, 1)
    XCTAssertEqual(converter.segmentCallCount, 0)
    XCTAssertEqual(converter.lastComposition?.leftContext, composition.leftContext)
    XCTAssertEqual(converter.lastOptions?.leftContext, options.leftContext)
    XCTAssertEqual(output.candidates.map(\.text), ["全文第一", "全文第二"])
    XCTAssertTrue(output.candidates.allSatisfy {
      $0.consumingCount == composition.elements.count
    })
    XCTAssertEqual(output.pageSize, 2)
    XCTAssertEqual(output.zenzaiExecutionEvidence, evidence)
  }

  func testV6CandidateObservationCapturesEvidenceAndIgnoresScoreDrift() throws {
    let observed = try ABProbeCandidateObservation.captureV6(
      [
        ConverterCandidate(
          text: "第一",
          consumingCount: 4,
          provenance: .personalDictionary,
          rankingInfluence: .zenzai,
          zenzaiScore: -2.5,
          zenzaiScoredTokenCount: 4,
          zenzaiScoreScope: .fullCandidate
        ),
        ConverterCandidate(text: "第二", consumingCount: 2),
      ],
      topK: 2
    )

    XCTAssertEqual(observed[0].provenance, "personalDictionary")
    XCTAssertEqual(observed[0].rankingInfluence, "zenzai")
    XCTAssertEqual(observed[0].zenzaiScore, -2.5)
    XCTAssertEqual(observed[0].zenzaiScoreTokenCount, 4)
    XCTAssertEqual(observed[0].zenzaiScoreScope, "full_candidate")
    XCTAssertEqual(observed[1].rankingInfluence, "standard")
    XCTAssertNil(observed[1].zenzaiScore)
    XCTAssertNil(observed[1].zenzaiScoreTokenCount)
    XCTAssertNil(observed[1].zenzaiScoreScope)

    var scoreDrift = observed
    scoreDrift[0] = ABProbeV6Candidate(
      text: observed[0].text,
      rank: observed[0].rank,
      consumingCount: observed[0].consumingCount,
      provenance: observed[0].provenance,
      rankingInfluence: observed[0].rankingInfluence,
      zenzaiScore: -3.0,
      zenzaiScoreTokenCount: observed[0].zenzaiScoreTokenCount,
      zenzaiScoreScope: observed[0].zenzaiScoreScope
    )
    XCTAssertNoThrow(
      try ABProbeCandidateObservation.validateStable(
        reference: observed,
        observed: scoreDrift,
        caseID: "case"
      )
    )

    let evidenceDrifts = [
      ABProbeV6Candidate(
        text: observed[0].text,
        rank: observed[0].rank,
        consumingCount: observed[0].consumingCount,
        provenance: observed[0].provenance,
        rankingInfluence: observed[0].rankingInfluence,
        zenzaiScore: observed[0].zenzaiScore,
        zenzaiScoreTokenCount: 5,
        zenzaiScoreScope: observed[0].zenzaiScoreScope
      ),
      ABProbeV6Candidate(
        text: observed[0].text,
        rank: observed[0].rank,
        consumingCount: observed[0].consumingCount,
        provenance: observed[0].provenance,
        rankingInfluence: observed[0].rankingInfluence,
        zenzaiScore: observed[0].zenzaiScore,
        zenzaiScoreTokenCount: observed[0].zenzaiScoreTokenCount,
        zenzaiScoreScope: "constraint_suffix"
      ),
      ABProbeV6Candidate(
        text: observed[0].text,
        rank: observed[0].rank,
        consumingCount: observed[0].consumingCount,
        provenance: observed[0].provenance,
        rankingInfluence: observed[0].rankingInfluence,
        zenzaiScore: nil,
        zenzaiScoreTokenCount: nil,
        zenzaiScoreScope: nil
      ),
    ]
    for driftedFirstCandidate in evidenceDrifts {
      XCTAssertThrowsError(
        try ABProbeCandidateObservation.validateStable(
          reference: observed,
          observed: [driftedFirstCandidate, observed[1]],
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

  func testV6CandidateObservationRejectsInvalidScoreEvidence() throws {
    for score in [Float.nan, Float.infinity, -Float.infinity] {
      XCTAssertThrowsError(
        try ABProbeCandidateObservation.captureV6(
          [
            ConverterCandidate(
              text: "候補",
              consumingCount: 2,
              rankingInfluence: .zenzai,
              zenzaiScore: score,
              zenzaiScoredTokenCount: 2,
              zenzaiScoreScope: .fullCandidate
            ),
          ],
          topK: 1
        )
      ) {
        XCTAssertEqual(
          $0 as? ABProbeError,
          .backendInstability("candidate rank 1 has a non-finite Zenzai score")
        )
      }
    }

    let incomplete = ABProbeV6Candidate(
      text: "候補",
      rank: 1,
      consumingCount: 2,
      provenance: CandidateProvenance.standard.rawValue,
      rankingInfluence: CandidateRankingInfluence.zenzai.rawValue,
      zenzaiScore: -1,
      zenzaiScoreTokenCount: nil,
      zenzaiScoreScope: nil
    )
    XCTAssertThrowsError(try JSONEncoder().encode(incomplete)) {
      XCTAssertEqual(
        $0 as? ABProbeError,
        .backendInstability(
          "candidate rank 1 must emit Zenzai score, token count, and scope together"
        )
      )
    }

    let invalidTokenCount = ABProbeV6Candidate(
      text: "候補",
      rank: 1,
      consumingCount: 2,
      provenance: CandidateProvenance.standard.rawValue,
      rankingInfluence: CandidateRankingInfluence.zenzai.rawValue,
      zenzaiScore: -1,
      zenzaiScoreTokenCount: 0,
      zenzaiScoreScope: "full_candidate"
    )
    XCTAssertThrowsError(try JSONEncoder().encode(invalidTokenCount)) {
      XCTAssertEqual(
        $0 as? ABProbeError,
        .backendInstability(
          "candidate rank 1 has a non-positive Zenzai score token count"
        )
      )
    }

    let invalidScope = ABProbeV6Candidate(
      text: "候補",
      rank: 1,
      consumingCount: 2,
      provenance: CandidateProvenance.standard.rawValue,
      rankingInfluence: CandidateRankingInfluence.zenzai.rawValue,
      zenzaiScore: -1,
      zenzaiScoreTokenCount: 2,
      zenzaiScoreScope: "unknown"
    )
    XCTAssertThrowsError(try JSONEncoder().encode(invalidScope)) {
      XCTAssertEqual(
        $0 as? ABProbeError,
        .backendInstability(
          "candidate rank 1 has an invalid Zenzai score scope: unknown"
        )
      )
    }
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
