import Foundation
import Glibc
import XCTest

@testable import hazkey_server

private final class RecordingMozcCore: MozcCoreConverting {
  struct Request: Equatable {
    let reading: String
    let targetKeySize: Int?
    let maxCandidates: Int
  }

  enum Fault: Error { case requested }

  var requests: [Request] = []
  var purgeCount = 0
  var shouldFail = false
  var handler: ((Request) throws -> MozcCoreConversion)?

  func convert(
    reading: String,
    targetKeySize: Int?,
    maxCandidates: Int
  ) throws -> MozcCoreConversion {
    let request = Request(
      reading: reading,
      targetKeySize: targetKeySize,
      maxCandidates: maxCandidates
    )
    requests.append(request)
    if shouldFail { throw Fault.requested }
    if let handler { return try handler(request) }
    let keySize = targetKeySize ?? reading.unicodeScalars.count
    return MozcCoreConversion(
      candidates: [
        MozcCoreCandidate(
          value: reading == "かな" ? "仮名" : reading,
          description: "Mozc",
          consumedKeySize: keySize
        )
      ],
      segmentKeySize: keySize
    )
  }

  func purgeSensitiveState() {
    purgeCount += 1
  }
}

final class GrimodexMozcSidecarTests: XCTestCase {
  func testBackendSelectionIsExactOptInAndPathsAreInjectable() {
    let hazkey = HazkeyServerConfig(
      zenzaiBackendDevicesProvider: { [] },
      environment: [:]
    )
    XCTAssertEqual(hazkey.converterBackend, .hazkey)
    XCTAssertTrue(
      hazkey.mozcHelperPath.hasSuffix("/fcitx5-grimodex-mozc-helper")
    )

    let unknown = HazkeyServerConfig(
      zenzaiBackendDevicesProvider: { [] },
      environment: ["FCITX5_GRIMODEX_CONVERTER": "Mozc"]
    )
    XCTAssertEqual(unknown.converterBackend, .hazkey)

    let mozc = HazkeyServerConfig(
      zenzaiBackendDevicesProvider: { [] },
      environment: [
        "FCITX5_GRIMODEX_CONVERTER": "mozc",
        "FCITX5_GRIMODEX_MOZC_HELPER": "/test/helper",
        "FCITX5_GRIMODEX_MOZC_DATA": "/test/mozc.data",
      ]
    )
    XCTAssertEqual(mozc.converterBackend, .mozc)
    XCTAssertEqual(mozc.mozcHelperPath, "/test/helper")
    XCTAssertEqual(mozc.mozcDataPath, "/test/mozc.data")
  }

  func testAdapterMapsRomajiReadingAndMozcSegmentBackToInputElements() throws {
    let core = RecordingMozcCore()
    core.handler = { request in
      XCTAssertEqual(request.reading, "きょうは")
      XCTAssertNil(request.targetKeySize)
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: "今日は",
            description: "Mozc",
            consumedKeySize: 3
          )
        ],
        segmentKeySize: 3
      )
    }
    let adapter = MozcKanaKanjiConverterAdapter(core: core)
    let input = mappedInput("kyouha")

    let output = try adapter.segmentCandidates(
      for: input,
      options: .default
    )

    XCTAssertEqual(adapter.display(for: input).text, "きょうは")
    XCTAssertEqual(output.candidates.first?.text, "今日は")
    XCTAssertEqual(output.candidates.first?.consumingCount, 4)
    XCTAssertEqual(core.requests.count, 1)
  }

  func testForcedSegmentResizeUsesRenderedKeySize() throws {
    let core = RecordingMozcCore()
    core.handler = { request in
      XCTAssertEqual(request.reading, "きょうは")
      XCTAssertEqual(request.targetKeySize, 3)
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: "今日",
            description: nil,
            consumedKeySize: 3
          )
        ],
        segmentKeySize: 3
      )
    }
    let adapter = MozcKanaKanjiConverterAdapter(core: core)
    let original = mappedInput("kyouha")
    let resized = CompositionInput(
      elements: original.elements,
      cursor: original.cursor,
      leftContext: "private context",
      targetCount: 4
    )

    let output = try adapter.candidates(for: resized, options: .default)

    XCTAssertEqual(output.candidates.first?.consumingCount, 4)
    XCTAssertEqual(core.requests.count, 1)
  }

  func testAdapterRejectsCoreBoundaryInsideACombinedCharacter() throws {
    let core = RecordingMozcCore()
    core.handler = { request in
      XCTAssertEqual(request.reading, "は\u{3099}")
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: "ば",
            description: nil,
            consumedKeySize: 1
          )
        ],
        segmentKeySize: 1
      )
    }
    let adapter = MozcKanaKanjiConverterAdapter(core: core)
    let input = CompositionInput(
      elements: [
        CompositionElement(text: "は\u{3099}", inputStyle: .direct)
      ],
      cursor: 1,
      leftContext: ""
    )

    XCTAssertThrowsError(
      try adapter.segmentCandidates(for: input, options: .default)
    ) { error in
      XCTAssertEqual(
        error as? MozcConverterAdapterError,
        .invalidCoreResponse
      )
    }
  }

  func testAdapterProtectsOnlyTheConsumedNaturalSegment() throws {
    let core = RecordingMozcCore()
    core.handler = { request in
      XCTAssertEqual(request.reading, "かな。")
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: "仮名",
            description: nil,
            consumedKeySize: 2
          )
        ],
        segmentKeySize: 2
      )
    }
    let adapter = MozcKanaKanjiConverterAdapter(core: core)

    let output = try adapter.segmentCandidates(
      for: directInput("かな。"),
      options: .default
    )

    XCTAssertEqual(output.candidates.first?.text, "仮名")
    XCTAssertEqual(output.candidates.first?.consumingCount, 2)
  }

  func testDictionaryOverlayRanksScopedProjectAndPersonalBeforeMozc() throws {
    let core = RecordingMozcCore()
    core.handler = { request in
      XCTAssertEqual(request.reading, "かな")
      XCTAssertEqual(request.targetKeySize, 2)
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: "カ\u{3099}",
            description: "duplicate",
            consumedKeySize: 2
          ),
          MozcCoreCandidate(
            value: "Mozc候補",
            description: "Mozc",
            consumedKeySize: 2
          ),
        ],
        segmentKeySize: 2
      )
    }
    let projectIndex = GrimodexProjectDictionaryIndex(entries: [
      projectEntry(
        ruby: "カナ",
        word: "低優先",
        priority: 1,
        entryID: "project-low"
      ),
      projectEntry(
        ruby: "カナ",
        word: "ガ",
        priority: 3,
        entryID: "project-high"
      ),
    ])
    let userStore = UserDictionaryStore(entries: [
      UserDictionaryEntry(
        id: "personal",
        reading: "かな",
        surface: "個人候補",
        partOfSpeech: "noun"
      ),
      UserDictionaryEntry(
        id: "personal-duplicate",
        reading: "かな",
        surface: "カ\u{3099}",
        partOfSpeech: "noun"
      ),
    ])
    let adapter = MozcKanaKanjiConverterAdapter(
      core: core,
      projectDictionaryIndexProvider: { projectIndex },
      userDictionaryIndexProvider: { userStore.candidateIndexSnapshot }
    )

    let output = try adapter.segmentCandidates(
      for: directInput("かな"),
      options: .default
    )

    XCTAssertEqual(
      output.candidates.map(\.text),
      ["ガ", "低優先", "個人候補", "Mozc候補"]
    )
    XCTAssertEqual(
      output.candidates.map(\.provenance),
      [.projectDictionary, .projectDictionary, .personalDictionary, .standard]
    )
    XCTAssertEqual(output.pageSize, 4)
    XCTAssertEqual(core.requests.count, 1)

    let limited = try adapter.segmentCandidates(
      for: directInput("かな"),
      options: ConversionOptions(
        allowLearning: false,
        zenzaiEnabled: false,
        leftContext: "",
        rightContext: "",
        suggestionListMode: .normal,
        suggestionListLimit: 2
      )
    )
    XCTAssertEqual(limited.candidates.map(\.text), ["ガ", "低優先"])
    XCTAssertEqual(limited.pageSize, 2)
    XCTAssertEqual(core.requests.count, 2)
  }

  func testLongestDictionaryPrefixOwnsNaturalSegmentButResizeStaysAuthoritative() throws {
    let core = RecordingMozcCore()
    core.handler = { request in
      let keySize = try XCTUnwrap(request.targetKeySize)
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: "core-\(keySize)",
            description: "Mozc",
            consumedKeySize: keySize
          )
        ],
        segmentKeySize: keySize
      )
    }
    let projectIndex = GrimodexProjectDictionaryIndex(entries: [
      projectEntry(
        ruby: "セツナ",
        word: "刹那",
        priority: 3,
        entryID: "project-short"
      )
    ])
    let userStore = UserDictionaryStore(entries: [
      UserDictionaryEntry(
        id: "personal-long",
        reading: "せつなで",
        surface: "刹那で",
        partOfSpeech: "noun"
      )
    ])
    let adapter = MozcKanaKanjiConverterAdapter(
      core: core,
      projectDictionaryIndexProvider: { projectIndex },
      userDictionaryIndexProvider: { userStore.candidateIndexSnapshot }
    )
    let input = directInput("せつなです")

    let natural = try adapter.segmentCandidates(for: input, options: .default)
    XCTAssertEqual(natural.candidates.first?.text, "刹那で")
    XCTAssertEqual(natural.candidates.first?.consumingCount, 4)

    let resized = CompositionInput(
      elements: input.elements,
      cursor: input.cursor,
      leftContext: "",
      targetCount: 3
    )
    let forced = try adapter.candidates(for: resized, options: .default)
    XCTAssertEqual(forced.candidates.first?.text, "刹那")
    XCTAssertEqual(forced.candidates.first?.consumingCount, 3)
    XCTAssertEqual(core.requests.map(\.targetKeySize), [4, 3] as [Int?])
  }

  func testEmptyDictionaryOverlayLeavesLongNaturalConversionUnforced() throws {
    let core = RecordingMozcCore()
    let adapter = MozcKanaKanjiConverterAdapter(core: core)
    let input = directInput(String(repeating: "あ", count: 128))

    let output = try adapter.segmentCandidates(for: input, options: .default)

    XCTAssertEqual(core.requests.count, 1)
    XCTAssertNil(core.requests.first?.targetKeySize)
    XCTAssertEqual(output.candidates.first?.consumingCount, 128)
  }

  func testSecureAndPredictionPathsDoNotCallCoreAndPurgeTerminatesIt() throws {
    let core = RecordingMozcCore()
    let adapter = MozcKanaKanjiConverterAdapter(
      core: core,
      projectDictionaryIndexProvider: {
        XCTFail("secure input must not read the project dictionary overlay")
        return .empty
      },
      userDictionaryIndexProvider: {
        XCTFail("secure input must not read the personal dictionary overlay")
        return .empty
      }
    )
    let input = directInput("秘密")
    let secure = ConversionOptions(
      allowLearning: false,
      zenzaiEnabled: false,
      secureInput: true,
      leftContext: "must-not-cross",
      rightContext: "must-not-cross",
      suggestionListMode: .predictive
    )

    XCTAssertTrue(
      try adapter.candidates(for: input, options: secure).candidates.isEmpty
    )
    XCTAssertTrue(
      try adapter.segmentCandidates(for: input, options: secure).candidates.isEmpty
    )
    XCTAssertTrue(
      try adapter.realtimeCandidates(for: input, options: secure).candidates.isEmpty
    )
    XCTAssertTrue(
      try adapter.predictions(for: input, options: .default).candidates.isEmpty
    )
    XCTAssertTrue(core.requests.isEmpty)

    adapter.stopComposition()
    XCTAssertEqual(core.purgeCount, 0)
    adapter.purgeSensitiveState()
    XCTAssertEqual(core.purgeCount, 1)
  }

  func testDisabledSuggestionListStillProvidesLiveCandidate() throws {
    let core = RecordingMozcCore()
    let adapter = MozcKanaKanjiConverterAdapter(core: core)
    let options = ConversionOptions(
      allowLearning: false,
      zenzaiEnabled: false,
      leftContext: "",
      rightContext: "",
      suggestionListMode: .disabled
    )

    let output = try adapter.realtimeCandidates(
      for: directInput("かな"),
      options: options
    )

    XCTAssertEqual(output.liveCandidate?.text, "仮名")
    XCTAssertTrue(output.candidates.isEmpty)
    XCTAssertEqual(output.pageSize, 0)
    XCTAssertEqual(core.requests.count, 1)
  }

  func testCoreFailurePreservesEditableComposition() {
    let core = RecordingMozcCore()
    core.shouldFail = true
    let reducer = ImeReducer(
      converter: MozcKanaKanjiConverterAdapter(core: core)
    )
    _ = reducer.reduce(.insertText("かな"), requestID: "insert")

    let result = reducer.reduce(.startConversion, requestID: "convert")

    XCTAssertEqual(result.status, .converterUnavailable)
    XCTAssertEqual(result.snapshot.phase, .composing)
    XCTAssertEqual(result.snapshot.preedit.map(\.text).joined(), "かな")
    XCTAssertEqual(core.requests.count, 1)
  }

  func testReducerUsesProductionSegmentedPathForResizeAndPartialCommit() {
    let core = RecordingMozcCore()
    core.handler = { request in
      let keySize: Int
      if let targetKeySize = request.targetKeySize {
        keySize = targetKeySize
      } else if request.reading.hasPrefix("とうきょう") {
        keySize = 5
      } else if request.reading.hasPrefix("に") {
        keySize = 1
      } else if request.reading.hasPrefix("いく") {
        keySize = 2
      } else {
        keySize = request.reading.count
      }
      let consumed = String(request.reading.prefix(keySize))
      let value = switch consumed {
      case "とうきょう": "東京"
      case "とうきょうに": "東京に"
      case "に": "に"
      case "いく": "行く"
      default: consumed
      }
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: value,
            description: "Mozc",
            consumedKeySize: keySize
          )
        ],
        segmentKeySize: keySize
      )
    }
    let reducer = ImeReducer(
      converter: MozcKanaKanjiConverterAdapter(core: core)
    )
    _ = reducer.reduce(
      .insertText("とうきょうにいく"),
      requestID: "insert"
    )

    let converted = reducer.reduce(.startConversion, requestID: "convert")

    XCTAssertEqual(converted.status, .success)
    XCTAssertEqual(converted.snapshot.preedit.map(\.text), ["東京", "に", "行く"])
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [5, 1, 2])

    let expanded = reducer.reduce(.resizeSegment(1), requestID: "expand")
    XCTAssertEqual(expanded.status, .success)
    XCTAssertEqual(expanded.snapshot.preedit.map(\.text), ["東京に", "行く"])
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [6, 2])

    let restored = reducer.reduce(.resizeSegment(-1), requestID: "restore")
    XCTAssertEqual(restored.status, .success)
    XCTAssertEqual(restored.snapshot.preedit.map(\.text), ["東京", "に", "行く"])
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [5, 1, 2])

    let committed = reducer.reduce(.commitSelected, requestID: "commit-first")
    XCTAssertEqual(committed.status, .success)
    XCTAssertEqual(
      committed.snapshot.effects,
      [.commitText(effectID: 1, text: "東京")]
    )
    XCTAssertEqual(committed.snapshot.preedit.map(\.text), ["に", "行く"])
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [1, 2])
  }

  func testReducerResizeMovesAcrossStableRomajiBoundaries() {
    let core = RecordingMozcCore()
    core.handler = { request in
      let keySize = request.targetKeySize ?? request.reading.unicodeScalars.count
      let consumed = String(request.reading.unicodeScalars.prefix(keySize))
      let value = switch consumed {
      case "きょう": "今日"
      case "きょうは": "今日は"
      default: consumed
      }
      return MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: value,
            description: "Mozc",
            consumedKeySize: keySize
          )
        ],
        segmentKeySize: keySize
      )
    }
    let reducer = ImeReducer(
      converter: MozcKanaKanjiConverterAdapter(core: core)
    )
    _ = reducer.reduce(.insertText("kyouha"), requestID: "insert-romaji")

    let converted = reducer.reduce(
      .startConversion,
      requestID: "convert-romaji"
    )
    XCTAssertEqual(converted.status, .success)
    XCTAssertEqual(converted.snapshot.preedit.map(\.text), ["今日は"])
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [6])

    let shrunk = reducer.reduce(
      .resizeSegment(-1),
      requestID: "shrink-romaji"
    )
    XCTAssertEqual(shrunk.status, .success)
    XCTAssertEqual(shrunk.snapshot.preedit.map(\.text), ["今日", "は"])
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [4, 2])

    let restored = reducer.reduce(
      .resizeSegment(1),
      requestID: "restore-romaji"
    )
    XCTAssertEqual(restored.status, .success)
    XCTAssertEqual(restored.snapshot.preedit.map(\.text), ["今日は"])
    XCTAssertEqual(reducer.session.segments.map(\.inputCount), [6])
    XCTAssertEqual(
      core.requests.map(\.targetKeySize),
      [nil, 3, nil, 4] as [Int?]
    )
  }

  func testStaleCandidateRequestCannotReachCore() throws {
    let core = RecordingMozcCore()
    let reducer = ImeReducer(
      converter: MozcKanaKanjiConverterAdapter(core: core)
    )
    let inserted = reducer.reduce(.insertText("かな"), requestID: "insert")
    let converted = reducer.reduce(
      .startConversion,
      requestID: "convert",
      expectedRevision: inserted.snapshot.revision
    )
    let candidate = try XCTUnwrap(converted.snapshot.candidateWindow.items.first)
    XCTAssertEqual(core.requests.count, 1)

    let stale = reducer.reduce(
      .selectCandidate(
        id: candidate.id,
        generation: converted.snapshot.candidateWindow.generation
      ),
      requestID: "stale",
      expectedRevision: 0
    )

    XCTAssertEqual(stale.status, .staleRevision)
    XCTAssertEqual(core.requests.count, 1)
  }

  func testRegistryPinsMozcConversionOnlyProfileAcrossCommitAndResolve() throws {
    let config = HazkeyServerConfig(
      zenzaiBackendDevicesProvider: { [] },
      environment: ["FCITX5_GRIMODEX_CONVERTER": "mozc"]
    )
    let core = RecordingMozcCore()
    let registry = HazkeySessionRegistry(
      serverConfig: config,
      mozcCore: core
    )
    XCTAssertEqual(registry.learningCapability, .conversionOnly)
    let sessionID = registry.open(
      clientContext: GrimodexClientContext(
        program: "grimodex",
        frontend: "wayland",
        secureInput: false
      ),
      ownerFd: 41
    )
    let controller = try XCTUnwrap(
      registry.semanticController(for: sessionID, ownerFd: 41)
    )

    let inserted = controller.handle(ImeV2Request(
      requestID: "insert",
      expectedRevision: 0,
      action: .insertText("かな")
    ))
    let policy = try XCTUnwrap(inserted.snapshot.recovery?.policy)
    XCTAssertFalse(policy.allowsLearning)
    XCTAssertFalse(policy.zenzaiEnabled)
    XCTAssertFalse(inserted.snapshot.pendingLearning)
    XCTAssertTrue(core.requests.isEmpty, "B0 predictions must remain local/no-op")

    let converted = controller.handle(ImeV2Request(
      requestID: "convert",
      expectedRevision: inserted.snapshot.revision,
      action: .startConversion
    ))
    XCTAssertEqual(converted.status, .success)
    XCTAssertFalse(converted.snapshot.pendingLearning)
    let committed = controller.handle(ImeV2Request(
      requestID: "commit",
      expectedRevision: converted.snapshot.revision,
      action: .commitSelected
    ))
    XCTAssertEqual(committed.status, .success)
    XCTAssertFalse(committed.snapshot.pendingLearning)
    XCTAssertEqual(core.requests.count, 1)
    let resolved = controller.handle(ImeV2Request(
      requestID: "resolve-noop",
      expectedRevision: committed.snapshot.revision,
      action: .resolvePendingLearning(commit: true)
    ))
    XCTAssertEqual(resolved.status, .success)
    XCTAssertFalse(resolved.snapshot.pendingLearning)
    let replayed = controller.handle(ImeV2Request(
      requestID: "resolve-noop",
      expectedRevision: committed.snapshot.revision,
      action: .resolvePendingLearning(commit: true)
    ))
    XCTAssertEqual(replayed, resolved)
    XCTAssertEqual(core.requests.count, 1)
    XCTAssertEqual(
      registry.zenzaiRuntimeDiagnostics().status,
      .policyDisabled
    )
    registry.reinitializeAll()
    XCTAssertEqual(
      registry.zenzaiRuntimeDiagnostics().status,
      .policyDisabled
    )
    let nextInserted = controller.handle(ImeV2Request(
      requestID: "insert-after-reinitialize",
      expectedRevision: resolved.snapshot.revision,
      action: .insertText("つぎ")
    ))
    XCTAssertEqual(nextInserted.status, .success)
    let nextPolicy = try XCTUnwrap(nextInserted.snapshot.recovery?.policy)
    XCTAssertFalse(nextPolicy.allowsLearning)
    XCTAssertFalse(nextPolicy.zenzaiEnabled)
    XCTAssertFalse(nextInserted.snapshot.pendingLearning)
    XCTAssertEqual(core.requests.count, 1)
  }

  func testProtocolV2RealServerOverlaysScopedProjectAndLivePersonalDictionary() throws {
    guard
      let executablePath = ProcessInfo.processInfo.environment[
        "GRIMODEX_PROCESS_E2E_SERVER"
      ],
      !executablePath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER to run the Mozc dictionary overlay test"
      )
    }
    let sidecarFixture = try makeProcessFixture(mode: "ok")
    defer { try? FileManager.default.removeItem(at: sidecarFixture.directory) }
    let snapshotFixture = try GrimodexProcessSnapshotFixture()
    defer { snapshotFixture.remove() }
    try snapshotFixture.publish(
      projectID: "mozc-project-a",
      surface: "Mozc工程A"
    )
    let configuredDictionary = ProcessInfo.processInfo.environment[
      "FCITX5_GRIMODEX_DICTIONARY"
    ].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0, isDirectory: true) }
    let server = GrimodexProcessHarness(
      executableURL: URL(fileURLWithPath: executablePath),
      grimodexRootURL: snapshotFixture.rootURL,
      converterConfiguration: .mozc(
        helperURL: sidecarFixture.helper,
        dataURL: sidecarFixture.data
      ),
      dictionaryURL: configuredDictionary
    )
    try server.start()
    defer { server.stop() }
    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }

    let grimodexOpen = try client.openSessionInfo(program: "grimodex")
    XCTAssertTrue(grimodexOpen.hasPersistentLearningAvailable)
    XCTAssertFalse(grimodexOpen.persistentLearningAvailable)
    let grimodexSession = grimodexOpen.sessionID
    XCTAssertEqual(
      try client.convertDirect("せつな", sessionID: grimodexSession).first,
      "Mozc工程A"
    )
    try snapshotFixture.publish(
      projectID: "mozc-project-b",
      surface: "Mozc工程B"
    )
    _ = try client.waitForDiagnostics {
      $0.snapshotStatus == "loaded" && $0.activeProjectID == "mozc-project-b"
    }
    let pinnedBeforeResize = try client.confirmedSnapshot(
      sessionID: grimodexSession
    )
    let shrunk = try client.transactV2(
      sessionID: grimodexSession,
      requestID: "mozc-project-pin-shrink",
      expectedRevision: pinnedBeforeResize.revision
    ) {
      $0.resizeSegment = Hazkey_Commands_ResizeSegment.with { $0.delta = -1 }
    }
    XCTAssertEqual(shrunk.status, .success)
    let expanded = try client.transactV2(
      sessionID: grimodexSession,
      requestID: "mozc-project-pin-expand",
      expectedRevision: shrunk.handleImeActionResult.snapshot.revision
    ) {
      $0.resizeSegment = Hazkey_Commands_ResizeSegment.with { $0.delta = 1 }
    }
    XCTAssertEqual(expanded.status, .success)
    XCTAssertEqual(
      expanded.handleImeActionResult.snapshot.candidateWindow.items.first?.text,
      "Mozc工程A",
      "a server-side resize round-trip must retain the project pinned to the active composition"
    )
    XCTAssertEqual(
      try client.convertDirect("せつな", sessionID: grimodexSession).first,
      "Mozc工程B",
      "the next composition must use the newly pinned project overlay"
    )

    let firefoxSession = try client.openSession(program: "firefox")
    XCTAssertFalse(
      try client.convertDirect("せつな", sessionID: firefoxSession)
        .contains("Mozc工程B"),
      "the scoped project overlay must not leak to another application"
    )
    let beforePersonal = try client.convertDirect(
      "かな",
      sessionID: firefoxSession
    )
    XCTAssertFalse(beforePersonal.contains("Mozc個人辞書"))
    let snapshotBeforePersonal = try client.confirmedSnapshot(
      sessionID: firefoxSession
    )
    _ = try client.addUserDictionaryEntry(
      id: "mozc-personal",
      reading: "かな",
      surface: "Mozc個人辞書"
    )
    let stale = try client.transactV2(
      sessionID: firefoxSession,
      requestID: "mozc-personal-stale-cancel",
      expectedRevision: snapshotBeforePersonal.revision
    ) {
      $0.cancel = .init()
    }
    XCTAssertEqual(stale.status, .staleRevision)
    XCTAssertEqual(stale.handleImeActionResult.snapshot.phase, .composing)
    XCTAssertTrue(
      stale.handleImeActionResult.snapshot.candidateWindow.items.isEmpty
    )
    let recovered = try client.transactV2(
      sessionID: firefoxSession,
      requestID: "mozc-personal-fresh-cancel",
      expectedRevision: stale.handleImeActionResult.snapshot.revision
    ) {
      $0.cancel = .init()
    }
    XCTAssertEqual(recovered.status, .success)
    XCTAssertEqual(recovered.handleImeActionResult.snapshot.phase, .idle)
    XCTAssertEqual(
      try client.convertDirect("かな", sessionID: firefoxSession).first,
      "Mozc個人辞書",
      "personal CRUD must invalidate and refresh an existing Mozc session"
    )
    XCTAssertEqual(try lineCount(sidecarFixture.conversions), 8)
    XCTAssertTrue(server.isRunning)
  }

  func testProtocolV2RealServerLazilyRespawnsExternallyKilledHelper() throws {
    guard
      let executablePath = ProcessInfo.processInfo.environment[
        "GRIMODEX_PROCESS_E2E_SERVER"
      ],
      !executablePath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER to run the Mozc SIGKILL recovery test"
      )
    }
    let fixture = try makeProcessFixture(mode: "ok")
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let snapshotFixture = try GrimodexProcessSnapshotFixture()
    defer { snapshotFixture.remove() }
    let configuredDictionary = ProcessInfo.processInfo.environment[
      "FCITX5_GRIMODEX_DICTIONARY"
    ].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0, isDirectory: true) }
    let server = GrimodexProcessHarness(
      executableURL: URL(fileURLWithPath: executablePath),
      grimodexRootURL: snapshotFixture.rootURL,
      converterConfiguration: .mozc(
        helperURL: fixture.helper,
        dataURL: fixture.data
      ),
      dictionaryURL: configuredDictionary
    )
    try server.start()
    var serverStopped = false
    defer {
      if !serverStopped {
        server.stop()
      }
    }
    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }

    let targetSession = try client.openSession(
      program: "mozc-sigkill-recovery-target"
    )
    let inserted = try client.transactV2(
      sessionID: targetSession,
      requestID: "mozc-sigkill-insert",
      expectedRevision: 0
    ) {
      $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "かな" }
    }
    XCTAssertEqual(inserted.status, .success)
    let insertedSnapshot = inserted.handleImeActionResult.snapshot
    XCTAssertEqual(insertedSnapshot.phase, .composing)
    XCTAssertEqual(insertedSnapshot.preedit.map(\.text).joined(), "かな")

    let warmupSession = try client.openSession(
      program: "mozc-sigkill-recovery-warmup"
    )
    XCTAssertEqual(
      try client.convertDirect("かな", sessionID: warmupSession).first,
      "仮名"
    )
    let initialChildren = try server.childProcessIdentifiers()
    guard initialChildren.count == 1, let killedHelperPID = initialChildren.first else {
      throw GrimodexProcessE2EError.invalidResponse(
        "expected exactly one Mozc fixture helper before SIGKILL"
      )
    }
    let killedHelperCommandLine = try XCTUnwrap(
      processCommandLine(killedHelperPID)
    )
    guard killedHelperCommandLine.contains(fixture.helper.path) else {
      throw GrimodexProcessE2EError.invalidResponse(
        "refusing to SIGKILL a child that is not the Mozc fixture helper"
      )
    }
    let initialRoots = try recordedTemporaryRoots(in: fixture)
    XCTAssertEqual(initialRoots.count, 1)
    let killedHelperRoot = try XCTUnwrap(initialRoots.first)
    XCTAssertTrue(FileManager.default.fileExists(atPath: killedHelperRoot))
    XCTAssertEqual(try lineCount(fixture.marker), 1)
    XCTAssertEqual(try lineCount(fixture.conversions), 1)

    guard try server.childProcessIdentifiers() == [killedHelperPID] else {
      throw GrimodexProcessE2EError.invalidResponse(
        "Mozc fixture helper identity changed immediately before SIGKILL"
      )
    }
    guard Glibc.kill(killedHelperPID, SIGKILL) == 0 else {
      throw GrimodexProcessE2EError.invalidResponse(
        "unable to SIGKILL the Mozc fixture helper: errno=\(errno)"
      )
    }
    XCTAssertTrue(
      try waitForChildProcessToDisappear(
        killedHelperPID,
        from: server,
        timeout: 3
      ),
      "the killed helper must disappear from the server child set before recovery starts"
    )
    XCTAssertTrue(FileManager.default.fileExists(atPath: killedHelperRoot))
    XCTAssertTrue(server.isRunning)

    let recovered = try client.transactV2(
      sessionID: targetSession,
      requestID: "mozc-sigkill-convert",
      expectedRevision: insertedSnapshot.revision
    ) {
      $0.startConversion = .init()
    }
    XCTAssertEqual(recovered.status, .success)
    let recoveredSnapshot = recovered.handleImeActionResult.snapshot
    XCTAssertEqual(recoveredSnapshot.phase, .previewing)
    XCTAssertEqual(recoveredSnapshot.preedit.map(\.text).joined(), "仮名")
    XCTAssertEqual(
      recoveredSnapshot.candidateWindow.items.first?.text,
      "仮名"
    )
    XCTAssertTrue(recoveredSnapshot.effects.isEmpty)
    XCTAssertGreaterThan(recoveredSnapshot.revision, insertedSnapshot.revision)
    XCTAssertTrue(server.isRunning)

    let recoveredChildren = try server.childProcessIdentifiers()
    XCTAssertEqual(recoveredChildren.count, 1)
    let recoveredHelperPID = try XCTUnwrap(recoveredChildren.first)
    let recoveredHelperCommandLine = try XCTUnwrap(
      processCommandLine(recoveredHelperPID)
    )
    XCTAssertTrue(recoveredHelperCommandLine.contains(fixture.helper.path))
    XCTAssertEqual(try lineCount(fixture.marker), 2)
    XCTAssertEqual(try lineCount(fixture.conversions), 2)
    XCTAssertEqual(try lineCount(fixture.stalls), 0)
    let recoveredRoots = try recordedTemporaryRoots(in: fixture)
    XCTAssertEqual(recoveredRoots.count, 2)
    XCTAssertEqual(recoveredRoots.first, killedHelperRoot)
    let recoveredHelperRoot = try XCTUnwrap(recoveredRoots.last)
    XCTAssertNotEqual(recoveredHelperRoot, killedHelperRoot)
    XCTAssertFalse(FileManager.default.fileExists(atPath: killedHelperRoot))
    XCTAssertTrue(FileManager.default.fileExists(atPath: recoveredHelperRoot))

    let replayed = try client.transactV2(
      sessionID: targetSession,
      requestID: "mozc-sigkill-convert",
      expectedRevision: insertedSnapshot.revision
    ) {
      $0.startConversion = .init()
    }
    XCTAssertEqual(
      try replayed.serializedData(),
      try recovered.serializedData()
    )
    XCTAssertEqual(try server.childProcessIdentifiers(), [recoveredHelperPID])
    XCTAssertEqual(try lineCount(fixture.marker), 2)
    XCTAssertEqual(try lineCount(fixture.conversions), 2)
    XCTAssertEqual(try recordedTemporaryRoots(in: fixture), recoveredRoots)

    client.close()
    server.stop()
    serverStopped = true
    XCTAssertTrue(
      waitForProcessIdentityToDisappear(
        recoveredHelperPID,
        commandLine: recoveredHelperCommandLine,
        timeout: 3
      ),
      "server shutdown must terminate the exact respawned helper identity"
    )
    XCTAssertTrue(
      recoveredRoots.allSatisfy {
        !FileManager.default.fileExists(atPath: $0)
      },
      "server shutdown must remove the killed and respawned private Mozc roots"
    )
  }

  func testProtocolV2RealServerDoesNotReplayEOFAndRecoversFreshRequest() throws {
    try assertProtocolV2RealServerRecoversAfterSidecarFailure(
      faultName: "eof",
      fixtureMode: "eof_after_convert_once"
    )
  }

  func testProtocolV2RealServerDoesNotReplayTimeoutAndRecoversFreshRequest() throws {
    try assertProtocolV2RealServerRecoversAfterSidecarFailure(
      faultName: "timeout",
      fixtureMode: "timeout_after_convert_once"
    )
  }

  func testProtocolV2RealServerDoesNotReplayPartialFrameEOFAndRecoversFreshRequest() throws {
    try assertProtocolV2RealServerRecoversAfterSidecarFailure(
      faultName: "partial-frame-eof",
      fixtureMode: "partial_body_eof_after_convert_once"
    )
  }

  private func assertProtocolV2RealServerRecoversAfterSidecarFailure(
    faultName: String,
    fixtureMode: String
  ) throws {
    guard
      let executablePath = ProcessInfo.processInfo.environment[
        "GRIMODEX_PROCESS_E2E_SERVER"
      ],
      !executablePath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER to run the Mozc \(faultName) process recovery test"
      )
    }
    let fixture = try makeProcessFixture(mode: fixtureMode)
    let expectedStallCount = fixtureMode == "timeout_after_convert_once" ? 1 : 0
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let snapshotFixture = try GrimodexProcessSnapshotFixture()
    defer { snapshotFixture.remove() }
    let configuredDictionary = ProcessInfo.processInfo.environment[
      "FCITX5_GRIMODEX_DICTIONARY"
    ].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0, isDirectory: true) }
    let server = GrimodexProcessHarness(
      executableURL: URL(fileURLWithPath: executablePath),
      grimodexRootURL: snapshotFixture.rootURL,
      converterConfiguration: .mozc(
        helperURL: fixture.helper,
        dataURL: fixture.data
      ),
      dictionaryURL: configuredDictionary
    )
    try server.start()
    var serverStopped = false
    defer {
      if !serverStopped {
        server.stop()
      }
    }
    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }
    let session = try client.openSessionInfo(
      program: "mozc-\(faultName)-recovery"
    )
    XCTAssertEqual(session.protocolVersion, 2)

    let inserted = try client.transactV2(
      sessionID: session.sessionID,
      requestID: "mozc-\(faultName)-insert",
      expectedRevision: 0
    ) {
      $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "かな" }
    }
    XCTAssertEqual(inserted.status, .success)
    let insertedSnapshot = inserted.handleImeActionResult.snapshot
    XCTAssertEqual(insertedSnapshot.phase, .composing)

    let failed = try client.transactV2(
      sessionID: session.sessionID,
      requestID: "mozc-\(faultName)-convert",
      expectedRevision: insertedSnapshot.revision
    ) {
      $0.startConversion = .init()
    }
    XCTAssertEqual(failed.status, .converterUnavailable)
    let failedSnapshot = failed.handleImeActionResult.snapshot
    XCTAssertEqual(failedSnapshot.phase, .composing)
    XCTAssertEqual(failedSnapshot.preedit.map(\.text).joined(), "かな")
    XCTAssertTrue(failedSnapshot.effects.isEmpty)
    XCTAssertTrue(failedSnapshot.candidateWindow.items.isEmpty)
    XCTAssertGreaterThan(failedSnapshot.revision, insertedSnapshot.revision)
    XCTAssertTrue(server.isRunning)
    XCTAssertEqual(try lineCount(fixture.marker), 1)
    XCTAssertEqual(try lineCount(fixture.conversions), 1)
    XCTAssertEqual(try lineCount(fixture.stalls), expectedStallCount)
    let rootsAfterFailure = try recordedTemporaryRoots(in: fixture)
    XCTAssertEqual(rootsAfterFailure.count, 1)
    let failedRoot = try XCTUnwrap(rootsAfterFailure.first)
    XCTAssertFalse(FileManager.default.fileExists(atPath: failedRoot))

    let duplicate = try client.transactV2(
      sessionID: session.sessionID,
      requestID: "mozc-\(faultName)-convert",
      expectedRevision: insertedSnapshot.revision
    ) {
      $0.startConversion = .init()
    }
    XCTAssertEqual(try duplicate.serializedData(), try failed.serializedData())
    XCTAssertEqual(try lineCount(fixture.marker), 1)
    XCTAssertEqual(try lineCount(fixture.conversions), 1)
    XCTAssertEqual(try lineCount(fixture.stalls), expectedStallCount)

    let recovered = try client.transactV2(
      sessionID: session.sessionID,
      requestID: "mozc-\(faultName)-convert-retry",
      expectedRevision: failedSnapshot.revision
    ) {
      $0.startConversion = .init()
    }
    XCTAssertEqual(recovered.status, .success)
    let recoveredSnapshot = recovered.handleImeActionResult.snapshot
    XCTAssertEqual(recoveredSnapshot.phase, .previewing)
    XCTAssertEqual(recoveredSnapshot.candidateWindow.items.first?.text, "仮名")
    XCTAssertGreaterThan(recoveredSnapshot.revision, failedSnapshot.revision)
    XCTAssertEqual(try lineCount(fixture.marker), 2)
    XCTAssertEqual(try lineCount(fixture.conversions), 2)
    XCTAssertEqual(try lineCount(fixture.stalls), expectedStallCount)
    XCTAssertTrue(server.isRunning)

    let temporaryRoots = try recordedTemporaryRoots(in: fixture)
    XCTAssertEqual(temporaryRoots.count, 2)
    XCTAssertEqual(temporaryRoots.first, failedRoot)
    let recoveredRoot = try XCTUnwrap(temporaryRoots.dropFirst().first)
    XCTAssertFalse(FileManager.default.fileExists(atPath: failedRoot))
    XCTAssertTrue(FileManager.default.fileExists(atPath: recoveredRoot))

    client.close()
    server.stop()
    serverStopped = true
    XCTAssertTrue(
      temporaryRoots.allSatisfy {
        !FileManager.default.fileExists(atPath: $0)
      },
      "server shutdown must remove every private Mozc root"
    )
  }

  func testProcessTransportValidatesFrameAndDataset() throws {
    let fixture = try makeProcessFixture(mode: "ok")
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let client = MozcSidecarClient(
      helperPath: fixture.helper.path,
      dataPath: fixture.data.path,
      timeoutMilliseconds: 500
    )
    XCTAssertEqual(
      client.diagnostics(),
      MozcSidecarDiagnostics(
        processIdentifier: nil,
        processLaunchCount: 0,
        temporaryDirectoryCleanupFailureCount: 0
      )
    )

    let result = try client.convert(
      reading: "かな",
      targetKeySize: nil,
      maxCandidates: 3
    )

    XCTAssertEqual(
      result,
      MozcCoreConversion(
        candidates: [
          MozcCoreCandidate(
            value: "仮名",
            description: "fixture",
            consumedKeySize: 2
          )
        ],
        segmentKeySize: 2
      )
    )
    let activeDiagnostics = client.diagnostics()
    XCTAssertNotNil(activeDiagnostics.processIdentifier)
    XCTAssertEqual(activeDiagnostics.processLaunchCount, 1)
    XCTAssertEqual(activeDiagnostics.temporaryDirectoryCleanupFailureCount, 0)

    client.purgeSensitiveState()

    XCTAssertEqual(
      client.diagnostics(),
      MozcSidecarDiagnostics(
        processIdentifier: nil,
        processLaunchCount: 1,
        temporaryDirectoryCleanupFailureCount: 0
      )
    )
  }

  func testHandshakeRejectsWrongDatasetBeforeReadingCrossesBoundary() throws {
    let fixture = try makeProcessFixture(mode: "wrong_dataset_ping")
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let client = MozcSidecarClient(
      helperPath: fixture.helper.path,
      dataPath: fixture.data.path,
      timeoutMilliseconds: 500
    )

    XCTAssertThrowsError(
      try client.convert(reading: "かな", targetKeySize: nil, maxCandidates: 3)
    ) { error in
      XCTAssertEqual(error as? MozcSidecarError, .datasetMismatch)
    }
    XCTAssertFalse(
      FileManager.default.fileExists(atPath: fixture.conversions.path),
      "the reading must not be sent before the helper proves its dataset"
    )
  }

  func testProcessTransportDoesNotReplayFailureAndRespawnsNextRequest() throws {
    let fixture = try makeProcessFixture(mode: "eof_after_convert_once")
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let client = MozcSidecarClient(
      helperPath: fixture.helper.path,
      dataPath: fixture.data.path,
      timeoutMilliseconds: 500
    )

    XCTAssertThrowsError(
      try client.convert(reading: "かな", targetKeySize: nil, maxCandidates: 3)
    ) { error in
      XCTAssertEqual(error as? MozcSidecarError, .disconnected)
    }
    let firstConversions = try String(
      contentsOf: fixture.conversions,
      encoding: .utf8
    ).split(separator: "\n")
    XCTAssertEqual(firstConversions.count, 1)
    let recovered = try client.convert(
      reading: "かな",
      targetKeySize: nil,
      maxCandidates: 3
    )
    XCTAssertEqual(recovered.candidates.first?.value, "仮名")
    let launches = try String(contentsOf: fixture.marker, encoding: .utf8)
      .split(separator: "\n")
    XCTAssertEqual(launches.count, 2)
    let conversions = try String(
      contentsOf: fixture.conversions,
      encoding: .utf8
    ).split(separator: "\n")
    XCTAssertEqual(conversions.count, 2)
  }

  func testMalformedCrossFieldBoundariesTerminateAndRespawnHelper() throws {
    for mode in ["candidate_mismatch", "forced_target_mismatch"] {
      let fixture = try makeProcessFixture(mode: mode)
      defer { try? FileManager.default.removeItem(at: fixture.directory) }
      let client = MozcSidecarClient(
        helperPath: fixture.helper.path,
        dataPath: fixture.data.path,
        timeoutMilliseconds: 500
      )

      for _ in 0..<2 {
        XCTAssertThrowsError(
          try client.convert(
            reading: "かな",
            targetKeySize: mode == "forced_target_mismatch" ? 2 : nil,
            maxCandidates: 3
          ),
          mode
        ) { error in
          XCTAssertEqual(error as? MozcSidecarError, .malformedResponse, mode)
        }
      }
      let launches = try String(contentsOf: fixture.marker, encoding: .utf8)
        .split(separator: "\n")
      XCTAssertEqual(launches.count, 2, mode)
    }
  }

  func testProcessPurgeTerminatesHelperAndNextRequestRespawns() throws {
    let fixture = try makeProcessFixture(mode: "ok")
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let client = MozcSidecarClient(
      helperPath: fixture.helper.path,
      dataPath: fixture.data.path,
      timeoutMilliseconds: 500
    )

    _ = try client.convert(
      reading: "かな",
      targetKeySize: nil,
      maxCandidates: 3
    )
    client.purgeSensitiveState()
    _ = try client.convert(
      reading: "かな",
      targetKeySize: nil,
      maxCandidates: 3
    )

    let launches = try String(contentsOf: fixture.marker, encoding: .utf8)
      .split(separator: "\n")
    XCTAssertEqual(launches.count, 2)
  }

  func testPrivateTemporaryRootIsRemovedAfterPurgeAndFailure() throws {
    let purgeFixture = try makeProcessFixture(mode: "ok")
    defer { try? FileManager.default.removeItem(at: purgeFixture.directory) }
    let client = MozcSidecarClient(
      helperPath: purgeFixture.helper.path,
      dataPath: purgeFixture.data.path,
      timeoutMilliseconds: 500
    )
    _ = try client.convert(
      reading: "かな",
      targetKeySize: nil,
      maxCandidates: 3
    )
    let purgeRoot = try XCTUnwrap(
      recordedTemporaryRoots(in: purgeFixture).last
    )
    let databaseMarker = URL(fileURLWithPath: purgeRoot, isDirectory: true)
      .appendingPathComponent("mozc-profile/segment.db")
    XCTAssertTrue(FileManager.default.fileExists(atPath: databaseMarker.path))
    let attributes = try FileManager.default.attributesOfItem(atPath: purgeRoot)
    let permissions = try XCTUnwrap(
      attributes[.posixPermissions] as? NSNumber
    )
    XCTAssertEqual(permissions.intValue & 0o777, 0o700)

    client.purgeSensitiveState()

    XCTAssertFalse(FileManager.default.fileExists(atPath: purgeRoot))

    let failureFixture = try makeProcessFixture(mode: "mismatch")
    defer { try? FileManager.default.removeItem(at: failureFixture.directory) }
    let failingClient = MozcSidecarClient(
      helperPath: failureFixture.helper.path,
      dataPath: failureFixture.data.path,
      timeoutMilliseconds: 500
    )
    XCTAssertThrowsError(
      try failingClient.convert(
        reading: "かな",
        targetKeySize: nil,
        maxCandidates: 3
      )
    ) { error in
      XCTAssertEqual(error as? MozcSidecarError, .responseMismatch)
    }
    let failureRoot = try XCTUnwrap(
      recordedTemporaryRoots(in: failureFixture).last
    )
    XCTAssertFalse(FileManager.default.fileExists(atPath: failureRoot))
  }

  func testPurgeStopsActiveProfileWriterBeforeRemovingPrivateRoot() throws {
    let fixture = try makeProcessFixture(mode: "profile_writer")
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let client = MozcSidecarClient(
      helperPath: fixture.helper.path,
      dataPath: fixture.data.path,
      timeoutMilliseconds: 500
    )
    _ = try client.convert(
      reading: "かな",
      targetKeySize: nil,
      maxCandidates: 3
    )
    let privateRoot = try XCTUnwrap(recordedTemporaryRoots(in: fixture).last)
    defer { try? FileManager.default.removeItem(atPath: privateRoot) }
    let counter = URL(fileURLWithPath: privateRoot, isDirectory: true)
      .appendingPathComponent("mozc-profile/writer-counter")
    var observedCounters = Set<Int>()
    let deadline = Date().addingTimeInterval(1)
    while Date() < deadline, observedCounters.count < 2 {
      if let contents = try? String(contentsOf: counter, encoding: .utf8),
         let value = Int(contents.trimmingCharacters(in: .whitespacesAndNewlines)) {
        observedCounters.insert(value)
      }
      Thread.sleep(forTimeInterval: 0.002)
    }
    XCTAssertGreaterThanOrEqual(
      observedCounters.count,
      2,
      "the fixture must still be mutating profile entries immediately before purge"
    )

    client.purgeSensitiveState()

    XCTAssertFalse(FileManager.default.fileExists(atPath: privateRoot))
    XCTAssertEqual(client.temporaryDirectoryCleanupFailureCount, 0)
  }

  func testProcessTransportRejectsMismatchOversizeAndTimeout() throws {
    for (mode, expected) in [
      ("mismatch", MozcSidecarError.responseMismatch),
      ("oversized", MozcSidecarError.oversizedFrame),
      ("timeout", MozcSidecarError.timeout),
    ] {
      let fixture = try makeProcessFixture(mode: mode)
      defer { try? FileManager.default.removeItem(at: fixture.directory) }
      let client = MozcSidecarClient(
        helperPath: fixture.helper.path,
        dataPath: fixture.data.path,
        timeoutMilliseconds: mode == "timeout" ? 25 : 500
      )
      XCTAssertThrowsError(
        try client.convert(reading: "かな", targetKeySize: nil, maxCandidates: 3),
        mode
      ) { error in
        XCTAssertEqual(error as? MozcSidecarError, expected, mode)
      }
    }
  }

  func testProcessTransportRejectsUnsupportedReadingBeforeLaunch() throws {
    let fixture = try makeProcessFixture(mode: "ok")
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    let client = MozcSidecarClient(
      helperPath: fixture.helper.path,
      dataPath: fixture.data.path,
      timeoutMilliseconds: 500
    )

    XCTAssertThrowsError(
      try client.convert(
        reading: String(repeating: "あ", count: 256),
        targetKeySize: nil,
        maxCandidates: 3
      )
    ) { error in
      XCTAssertEqual(error as? MozcSidecarError, .invalidRequest)
    }
    XCTAssertThrowsError(
      try client.convert(reading: "かな", targetKeySize: 0, maxCandidates: 3)
    ) { error in
      XCTAssertEqual(error as? MozcSidecarError, .invalidRequest)
    }
    XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.marker.path))
  }

  func testActualFixedSidecarBundleWhenConfigured() throws {
    guard let bundlePath = ProcessInfo.processInfo.environment[
      "GRIMODEX_MOZC_TEST_BUNDLE"
    ] else {
      throw XCTSkip(
        "Set GRIMODEX_MOZC_TEST_BUNDLE to run the fixed B0 helper smoke"
      )
    }
    let bundle = URL(fileURLWithPath: bundlePath, isDirectory: true)
    let client = MozcSidecarClient(
      helperPath: bundle
        .appendingPathComponent("fcitx5-grimodex-mozc-helper")
        .path,
      dataPath: bundle.appendingPathComponent("mozc.data").path,
      timeoutMilliseconds: 10_000
    )
    XCTAssertEqual(
      client.diagnostics(),
      MozcSidecarDiagnostics(
        processIdentifier: nil,
        processLaunchCount: 0,
        temporaryDirectoryCleanupFailureCount: 0
      )
    )

    let natural = try client.convert(
      reading: "きょうはいしゃにいく",
      targetKeySize: nil,
      maxCandidates: 5
    )
    XCTAssertEqual(natural.segmentKeySize, 4)
    XCTAssertEqual(natural.candidates.first?.value, "今日は")
    let activeDiagnostics = client.diagnostics()
    XCTAssertNotNil(activeDiagnostics.processIdentifier)
    XCTAssertEqual(activeDiagnostics.processLaunchCount, 1)
    XCTAssertEqual(activeDiagnostics.temporaryDirectoryCleanupFailureCount, 0)

    let resized = try client.convert(
      reading: "きょうは",
      targetKeySize: 3,
      maxCandidates: 5
    )
    XCTAssertEqual(resized.segmentKeySize, 3)
    XCTAssertEqual(resized.candidates.first?.value, "今日")
    XCTAssertEqual(client.diagnostics().processLaunchCount, 1)

    client.purgeSensitiveState()

    XCTAssertEqual(
      client.diagnostics(),
      MozcSidecarDiagnostics(
        processIdentifier: nil,
        processLaunchCount: 1,
        temporaryDirectoryCleanupFailureCount: 0
      )
    )
  }

  private func mappedInput(_ text: String) -> CompositionInput {
    CompositionInput(
      elements: text.map { CompositionElement(text: String($0)) },
      cursor: text.count,
      leftContext: ""
    )
  }

  private func projectEntry(
    ruby: String,
    word: String,
    priority: Int,
    entryID: String
  ) -> GrimodexMappedDictionaryEntry {
    GrimodexMappedDictionaryEntry(
      ruby: ruby,
      word: word,
      cid: 1288,
      mid: 501,
      value: priority == 3 ? -4 : -8,
      priority: priority,
      entryID: entryID
    )
  }

  private func directInput(_ text: String) -> CompositionInput {
    CompositionInput(
      elements: text.map {
        CompositionElement(text: String($0), inputStyle: .direct)
      },
      cursor: text.count,
      leftContext: ""
    )
  }

  private struct ProcessFixture {
    let directory: URL
    let helper: URL
    let data: URL
    let marker: URL
    let conversions: URL
    let stalls: URL
    let temporaryRoots: URL
  }

  private func recordedTemporaryRoots(
    in fixture: ProcessFixture
  ) throws -> [String] {
    guard FileManager.default.fileExists(atPath: fixture.temporaryRoots.path) else {
      return []
    }
    return try String(
      contentsOf: fixture.temporaryRoots,
      encoding: .utf8
    ).split(separator: "\n").map(String.init)
  }

  private func lineCount(_ url: URL) throws -> Int {
    guard FileManager.default.fileExists(atPath: url.path) else { return 0 }
    return try String(contentsOf: url, encoding: .utf8)
      .split(separator: "\n").count
  }

  private func waitForChildProcessToDisappear(
    _ processIdentifier: Int32,
    from server: GrimodexProcessHarness,
    timeout: TimeInterval
  ) throws -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
      let children = try server.childProcessIdentifiers()
      if !children.contains(processIdentifier) {
        return true
      }
      usleep(10_000)
    }
    let children = try server.childProcessIdentifiers()
    return !children.contains(processIdentifier)
  }

  private func processCommandLine(_ processIdentifier: Int32) -> String? {
    let url = URL(
      fileURLWithPath: "/proc/\(processIdentifier)/cmdline",
      isDirectory: false
    )
    guard let data = try? Data(contentsOf: url), !data.isEmpty else {
      return nil
    }
    return String(decoding: data, as: UTF8.self)
      .replacingOccurrences(of: "\0", with: " ")
  }

  private func waitForProcessIdentityToDisappear(
    _ processIdentifier: Int32,
    commandLine: String,
    timeout: TimeInterval
  ) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
      if processCommandLine(processIdentifier) != commandLine {
        return true
      }
      usleep(10_000)
    }
    return processCommandLine(processIdentifier) != commandLine
  }

  private func makeProcessFixture(mode: String) throws -> ProcessFixture {
    let directory = FileManager.default.temporaryDirectory
      .appendingPathComponent("hazkey-mozc-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(
      at: directory,
      withIntermediateDirectories: true
    )
    let helper = directory.appendingPathComponent("helper.py")
    let data = directory.appendingPathComponent("mozc.data")
    let marker = directory.appendingPathComponent("launches.txt")
    let conversions = directory.appendingPathComponent("conversions.txt")
    let stalls = directory.appendingPathComponent("stalls.txt")
    let temporaryRoots = directory.appendingPathComponent("temporary-roots.txt")
    try Data("fixture".utf8).write(to: data)
    let script = """
      #!/usr/bin/python3
      import os, struct, sys, threading, time

      MODE = "\(mode)"
      MARKER = "\(marker.path)"
      CONVERSIONS = "\(conversions.path)"
      STALLS = "\(stalls.path)"
      TEMPORARY_ROOTS = "\(temporaryRoots.path)"
      SHA = "\(MozcSidecarClient.fixedB0DatasetSHA256)"

      with open(MARKER, "a", encoding="utf-8") as f:
          f.write("launch\\n")
      temporary_root = os.environ["TMPDIR"]
      profile = os.path.join(temporary_root, "mozc-profile")
      os.makedirs(profile, mode=0o700, exist_ok=True)
      with open(os.path.join(profile, "segment.db"), "wb") as f:
          f.write(b"fixture-db")
      with open(TEMPORARY_ROOTS, "a", encoding="utf-8") as f:
          f.write(temporary_root + "\\n")

      if MODE == "profile_writer":
          def churn_profile():
              index = 0
              while True:
                  os.makedirs(profile, mode=0o700, exist_ok=True)
                  entry = os.path.join(profile, f"entry-{index % 64}.db")
                  with open(entry, "wb") as f:
                      f.write(str(index).encode("ascii"))
                  with open(os.path.join(profile, "writer-counter"), "w",
                            encoding="ascii") as f:
                      f.write(str(index))
                  index += 1
                  time.sleep(0.0005)

          threading.Thread(target=churn_profile, daemon=True).start()

      def read_exact(n):
          out = b""
          while len(out) < n:
              chunk = sys.stdin.buffer.read(n - len(out))
              if not chunk:
                  return None
              out += chunk
          return out

      def varint(value):
          out = bytearray()
          while value > 0x7f:
              out.append((value & 0x7f) | 0x80)
              value >>= 7
          out.append(value)
          return bytes(out)

      def parse_varint(data, index):
          value = 0
          shift = 0
          while True:
              byte = data[index]
              index += 1
              value |= (byte & 0x7f) << shift
              if byte < 0x80:
                  return value, index
              shift += 7

      def parse(data):
          result = {}
          index = 0
          while index < len(data):
              key, index = parse_varint(data, index)
              field = key >> 3
              wire = key & 7
              if wire == 0:
                  value, index = parse_varint(data, index)
              elif wire == 2:
                  size, index = parse_varint(data, index)
                  value = data[index:index + size]
                  index += size
              else:
                  raise RuntimeError("unsupported wire type")
              result[field] = value
          return result

      def scalar(field, value):
          return varint(field << 3) + varint(value)

      def blob(field, value):
          return varint((field << 3) | 2) + varint(len(value)) + value

      while True:
          header = read_exact(4)
          if header is None:
              break
          size = struct.unpack(">I", header)[0]
          payload = read_exact(size)
          if payload is None:
              break
          fields = parse(payload)
          request_id = fields[2]
          operation = fields.get(3, 0)

          if MODE == "timeout":
              time.sleep(1)
              continue
          if MODE == "oversized":
              sys.stdout.buffer.write(struct.pack(">I", 4194305))
              sys.stdout.buffer.flush()
              continue

          response_id = request_id + (1 if MODE == "mismatch" else 0)
          dataset_sha = (
              "0" * 64 if MODE == "wrong_dataset_ping" else SHA
          ).encode("ascii")
          if operation == 2:
              response = (
                  scalar(1, 1)
                  + scalar(2, response_id)
                  + scalar(3, 1)
                  + blob(7, dataset_sha)
              )
              sys.stdout.buffer.write(
                  struct.pack(">I", len(response)) + response
              )
              sys.stdout.buffer.flush()
              continue

          reading = fields[4].decode("utf-8")
          target = fields.get(5, 0)
          with open(CONVERSIONS, "a", encoding="utf-8") as f:
              f.write(reading + "\\n")

          if (MODE == "eof_after_convert_once"
                  and os.path.getsize(MARKER) == len("launch\\n")):
              sys.exit(0)

          if (MODE == "partial_body_eof_after_convert_once"
                  and os.path.getsize(MARKER) == len("launch\\n")):
              sys.stdout.buffer.write(struct.pack(">I", 16) + b"\\x08\\x01")
              sys.stdout.buffer.flush()
              sys.exit(0)

          if (MODE == "timeout_after_convert_once"
                  and os.path.getsize(MARKER) == len("launch\\n")):
              with open(STALLS, "a", encoding="utf-8") as f:
                  f.write("stall\\n")
              while True:
                  time.sleep(60)

          key_size = target or len(reading)
          response_key_size = (
              max(1, key_size - 1)
              if MODE == "forced_target_mismatch"
              else key_size
          )
          candidate_key_size = (
              max(1, key_size - 1)
              if MODE == "candidate_mismatch"
              else response_key_size
          )
          candidate = (
              blob(1, "仮名".encode("utf-8"))
              + blob(2, b"fixture")
              + scalar(3, candidate_key_size)
          )
          response = (
              scalar(1, 1)
              + scalar(2, response_id)
              + scalar(3, 1)
              + blob(5, candidate)
              + scalar(6, response_key_size)
              + blob(7, dataset_sha)
          )
          sys.stdout.buffer.write(struct.pack(">I", len(response)) + response)
          sys.stdout.buffer.flush()
      """
    try script.write(to: helper, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes(
      [.posixPermissions: 0o700],
      ofItemAtPath: helper.path
    )
    return ProcessFixture(
      directory: directory,
      helper: helper,
      data: data,
      marker: marker,
      conversions: conversions,
      stalls: stalls,
      temporaryRoots: temporaryRoots
    )
  }
}
