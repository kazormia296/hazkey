import Foundation
import XCTest

@testable import hazkey_server

private struct ContractScenario: Decodable {
  struct Initial: Decodable {
    let phase: String
    let composition: String
    let revision: UInt64
  }

  struct Action: Decodable {
    let type: String
    let text: String?
    let offset: Int?
    let delta: Int?
    let candidateID: String?
    let generation: UInt64?
    let secureInput: Bool?

    enum CodingKeys: String, CodingKey {
      case type, text, offset, delta, generation
      case candidateID = "candidate_id"
      case secureInput = "secure_input"
    }
  }

  struct ExpectedSnapshot: Decodable {
    struct Span: Decodable {
      let text: String
      let style: String
    }

    struct Effect: Decodable {
      let effectID: UInt64
      let type: String
      let text: String?
      let before: Int?
      let after: Int?
      let mode: String?
      let message: String?

      enum CodingKeys: String, CodingKey {
        case effectID = "effect_id"
        case type, text, before, after, mode, message
      }
    }

    struct CandidateWindow: Decodable {
      struct Item: Decodable {
        let id: String
        let text: String
        let annotation: String?
        let consumingCount: Int

        enum CodingKeys: String, CodingKey {
          case id, text, annotation
          case consumingCount = "consuming_count"
        }
      }

      let generation: UInt64
      let items: [Item]
      let selectedIndex: Int?
      let pageSize: Int

      enum CodingKeys: String, CodingKey {
        case generation, items
        case selectedIndex = "selected_index"
        case pageSize = "page_size"
      }
    }

    let revision: UInt64
    let phase: String
    let preedit: [Span]
    let caretUtf8ByteOffset: UInt32?
    let candidateWindow: CandidateWindow
    let effects: [Effect]

    enum CodingKeys: String, CodingKey {
      case revision, phase, preedit, effects
      case caretUtf8ByteOffset = "caret_utf8_byte_offset"
      case candidateWindow = "candidate_window"
    }
  }

  struct ExpectedLearning: Decodable {
    let completed: Int
    let updated: Int
    let committed: Int
    let forgotten: Int
  }

  let contractVersion: String
  let scenarioID: String
  let initial: Initial
  let actions: [Action]
  let statuses: [String]
  let snapshots: [ExpectedSnapshot]
  let converterFault: String?
  let expectedLearning: ExpectedLearning

  enum CodingKeys: String, CodingKey {
    case contractVersion = "contract_version"
    case scenarioID = "scenario_id"
    case initial, actions, statuses, snapshots
    case converterFault = "converter_fault"
    case expectedLearning = "expected_learning"
  }
}

private protocol ContractScenarioConverting: KanaKanjiConverting {
  var completed: Int { get }
  var updated: Int { get }
  var committed: Int { get }
  var forgotten: Int { get }
}

private final class ContractFixtureConverter: ContractScenarioConverting {
  enum FixtureError: Error { case requestedFailure }

  let shouldFail: Bool
  private(set) var completed = 0
  private(set) var updated = 0
  private(set) var committed = 0
  private(set) var forgotten = 0

  init(shouldFail: Bool) {
    self.shouldFail = shouldFail
  }

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    if shouldFail { throw FixtureError.requestedFailure }
    let target = min(
      max(composition.targetCount ?? composition.elements.count, 1),
      composition.elements.count
    )
    let consuming = composition.targetCount == nil ? min(2, target) : target
    let text = composition.elements.prefix(consuming).map(\.text).joined()
    return ConversionOutput(
      candidates: [
        ConverterCandidate(text: "変換", consumingCount: consuming),
        ConverterCandidate(text: text, annotation: "reading", consumingCount: consuming),
      ],
      pageSize: 2
    )
  }

  func display(for composition: CompositionInput) -> CompositionDisplay {
    let cursor = min(max(composition.cursor, 0), composition.elements.count)
    return CompositionDisplay(
      text: composition.elements.map(\.text).joined(),
      caretUtf8ByteOffset: UInt32(
        composition.elements.prefix(cursor).reduce(0) { $0 + $1.text.utf8.count }
      )
    )
  }

  func setCompletedData(_ candidate: ConverterCandidate) { completed += 1 }
  func updateLearningData(_ candidate: ConverterCandidate) { updated += 1 }
  func commitLearning() { committed += 1 }
  func forget(_ candidate: ConverterCandidate) { forgotten += 1 }
  func stopComposition() {}
}

private final class ContractMozcCore: MozcCoreConverting {
  enum FixtureError: Error { case requestedFailure }

  let shouldFail: Bool

  init(shouldFail: Bool) {
    self.shouldFail = shouldFail
  }

  func convert(
    reading: String,
    targetKeySize: Int?,
    maxCandidates: Int
  ) throws -> MozcCoreConversion {
    if shouldFail { throw FixtureError.requestedFailure }
    let keySize = targetKeySize ?? min(2, reading.unicodeScalars.count)
    let prefix = String(reading.unicodeScalars.prefix(keySize))
    return MozcCoreConversion(
      candidates: [
        MozcCoreCandidate(
          value: "変換",
          description: nil,
          consumedKeySize: keySize
        ),
        MozcCoreCandidate(
          value: prefix,
          description: "reading",
          consumedKeySize: keySize
        ),
      ],
      segmentKeySize: keySize
    )
  }

  func purgeSensitiveState() {}
}

/// Runs the locked cross-platform fixture through the real Mozc adapter and a
/// fake process contract. This wrapper keeps the versioned v1 snapshots on
/// their compatibility path; production segmentation is exercised by the
/// dedicated Mozc reducer integration test.
private final class ContractMozcAdapter: ContractScenarioConverting {
  private let adapter: MozcKanaKanjiConverterAdapter
  let completed = 0
  let updated = 0
  let committed = 0
  let forgotten = 0

  init(shouldFail: Bool) {
    adapter = MozcKanaKanjiConverterAdapter(
      core: ContractMozcCore(shouldFail: shouldFail)
    )
  }

  func display(for composition: CompositionInput) -> CompositionDisplay {
    adapter.display(for: composition)
  }

  func inputCursorPosition(
    for composition: CompositionInput,
    movingBy offset: Int
  ) -> Int {
    adapter.inputCursorPosition(for: composition, movingBy: offset)
  }

  func candidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    if composition.targetCount == nil {
      return try adapter.segmentCandidates(for: composition, options: options)
    }
    return try adapter.candidates(for: composition, options: options)
  }

  func realtimeCandidates(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> RealtimeConversionOutput {
    try adapter.realtimeCandidates(for: composition, options: options)
  }

  func predictions(
    for composition: CompositionInput,
    options: ConversionOptions
  ) throws -> ConversionOutput {
    try adapter.predictions(for: composition, options: options)
  }

  func setCompletedData(_ candidate: ConverterCandidate) {}
  func updateLearningData(_ candidate: ConverterCandidate) {}
  func commitLearning() {}
  func forget(_ candidate: ConverterCandidate) {}
  func stopComposition() { adapter.stopComposition() }
  func purgeSensitiveState() { adapter.purgeSensitiveState() }
}

final class GrimodexCompositionContractAdapterTests: XCTestCase {
  func testAllLockedCompositionBehaviorScenariosRunThroughTheLinuxReducer() throws {
    try runLockedScenarios(
      converterFactory: {
        ContractFixtureConverter(
          shouldFail: $0.converterFault == "converter_throws"
        )
      },
      expectedLearning: { $0.expectedLearning }
    )
  }

  func testAllLockedCompositionBehaviorScenariosRunThroughMozcAdapter() throws {
    try runLockedScenarios(
      converterFactory: {
        ContractMozcAdapter(
          shouldFail: $0.converterFault == "converter_throws"
        )
      },
      expectedLearning: { _ in
        ContractScenario.ExpectedLearning(
          completed: 0,
          updated: 0,
          committed: 0,
          forgotten: 0
        )
      }
    )
  }

  private func runLockedScenarios(
    converterFactory: (ContractScenario) -> any ContractScenarioConverting,
    expectedLearning: (ContractScenario) -> ContractScenario.ExpectedLearning
  ) throws {
    let root = try XCTUnwrap(Bundle.module.resourceURL)
      .appendingPathComponent("Fixtures/composition-behavior-v1/scenarios")
    let urls = try FileManager.default.contentsOfDirectory(
      at: root,
      includingPropertiesForKeys: nil
    ).filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }

    let required: Set<String> = [
      "composing-basic", "cursor-editing", "escape-backspace", "partial-commit",
      "secure-input", "segment-editing", "server-failure", "stale-candidate",
      "unicode-caret",
    ]
    var observed = Set<String>()

    for url in urls {
      let scenario = try JSONDecoder().decode(
        ContractScenario.self,
        from: Data(contentsOf: url)
      )
      XCTAssertEqual(scenario.contractVersion, "composition-behavior-v1")
      observed.insert(scenario.scenarioID)

      var session = CompositionSession()
      session.phase = ImePhase(rawValue: scenario.initial.phase) ?? .idle
      session.revision = scenario.initial.revision
      session.composingText.insert(scenario.initial.composition, inputStyle: .direct)
      let converter = converterFactory(scenario)
      let reducer = ImeReducer(
        session: session,
        converter: converter
      )

      XCTAssertEqual(scenario.snapshots.count, scenario.actions.count, scenario.scenarioID)
      XCTAssertEqual(scenario.statuses.count, scenario.actions.count, scenario.scenarioID)

      for (index, action) in scenario.actions.enumerated() {
        let result = reducer.reduce(
          try semanticAction(action),
          requestID: "contract-\(scenario.scenarioID)-\(index)"
        )
        let preeditLength = result.snapshot.preedit.reduce(0) {
          $0 + $1.text.utf8.count
        }
        if let caret = result.snapshot.caretUtf8ByteOffset {
          XCTAssertLessThanOrEqual(Int(caret), preeditLength, scenario.scenarioID)
        }
        if let selected = result.snapshot.candidateWindow.selectedIndex {
          XCTAssertTrue(
            result.snapshot.candidateWindow.items.indices.contains(selected),
            scenario.scenarioID
          )
        }
        XCTAssertEqual(
          contractStatus(result.status),
          scenario.statuses[index],
          "\(scenario.scenarioID) action \(index)"
        )
        assert(
          result.snapshot,
          matches: scenario.snapshots[index],
          scenarioID: "\(scenario.scenarioID) action \(index)"
        )
        if scenario.scenarioID == "secure-input" {
          if index < 3 {
            XCTAssertNil(result.snapshot.recovery, "secure action \(index)")
          } else {
            XCTAssertNotNil(result.snapshot.recovery, "normal policy must resume")
          }
        }
      }

      let learning = expectedLearning(scenario)
      XCTAssertEqual(converter.completed, learning.completed, scenario.scenarioID)
      XCTAssertEqual(converter.updated, learning.updated, scenario.scenarioID)
      XCTAssertEqual(converter.committed, learning.committed, scenario.scenarioID)
      XCTAssertEqual(converter.forgotten, learning.forgotten, scenario.scenarioID)
    }

    XCTAssertEqual(observed, required)
  }

  private func semanticAction(_ action: ContractScenario.Action) throws -> ImeAction {
    switch action.type {
    case "insert_text": return .insertText(action.text ?? "")
    case "delete_backward": return .deleteBackward
    case "delete_forward": return .deleteForward
    case "move_cursor": return .moveCursor(action.offset ?? 0)
    case "move_cursor_to_start": return .moveCursorToStart
    case "move_cursor_to_end": return .moveCursorToEnd
    case "start_conversion": return .startConversion
    case "navigate_candidate": return .navigateCandidate(action.delta ?? 0)
    case "navigate_candidate_page": return .navigateCandidatePage(action.delta ?? 0)
    case "resize_segment": return .resizeSegment(action.delta ?? 0)
    case "commit_selected": return .commitSelected
    case "commit_all": return .commitAll
    case "cancel": return .cancel
    case "select_candidate":
      return .selectCandidate(
        id: action.candidateID ?? "",
        generation: action.generation ?? 0
      )
    case "secure_input_changed":
      return .lifecycle(.secureInputChanged(action.secureInput ?? false))
    default:
      throw NSError(
        domain: "GrimodexCompositionContractAdapterTests",
        code: 1,
        userInfo: [NSLocalizedDescriptionKey: "Unsupported action \(action.type)"]
      )
    }
  }

  private func contractStatus(_ status: ImeReductionStatus) -> String {
    switch status {
    case .success: return "success"
    case .staleRevision: return "stale_revision"
    case .staleCandidate: return "stale_candidate"
    case .invalidAction: return "invalid_action"
    case .converterUnavailable: return "converter_unavailable"
    case .secureInputViolation: return "secure_input_violation"
    }
  }

  private func assert(
    _ actual: SessionSnapshot,
    matches expected: ContractScenario.ExpectedSnapshot,
    scenarioID: String
  ) {
    XCTAssertEqual(actual.revision, expected.revision, scenarioID)
    XCTAssertEqual(actual.phase.rawValue, expected.phase, scenarioID)
    XCTAssertEqual(actual.preedit.map(\.text), expected.preedit.map(\.text), scenarioID)
    XCTAssertEqual(
      actual.preedit.map { $0.style.rawValue },
      expected.preedit.map(\.style),
      scenarioID
    )
    XCTAssertEqual(actual.caretUtf8ByteOffset, expected.caretUtf8ByteOffset, scenarioID)
    XCTAssertEqual(actual.candidateWindow.generation, expected.candidateWindow.generation, scenarioID)
    XCTAssertEqual(actual.candidateWindow.selectedIndex, expected.candidateWindow.selectedIndex, scenarioID)
    XCTAssertEqual(actual.candidateWindow.pageSize, expected.candidateWindow.pageSize, scenarioID)
    XCTAssertEqual(actual.candidateWindow.items.count, expected.candidateWindow.items.count, scenarioID)
    for (actualItem, expectedItem) in zip(
      actual.candidateWindow.items,
      expected.candidateWindow.items
    ) {
      XCTAssertEqual(actualItem.id, expectedItem.id, scenarioID)
      XCTAssertEqual(actualItem.text, expectedItem.text, scenarioID)
      XCTAssertEqual(actualItem.annotation, expectedItem.annotation, scenarioID)
      XCTAssertEqual(actualItem.consumingCount, expectedItem.consumingCount, scenarioID)
    }
    XCTAssertEqual(actual.effects.count, expected.effects.count, scenarioID)
    for (actualEffect, expectedEffect) in zip(actual.effects, expected.effects) {
      switch actualEffect {
      case .commitText(let effectID, let text):
        XCTAssertEqual(expectedEffect.type, "commit_text", scenarioID)
        XCTAssertEqual(effectID, expectedEffect.effectID, scenarioID)
        XCTAssertEqual(text, expectedEffect.text, scenarioID)
      case .deleteSurroundingText(let effectID, let before, let after):
        XCTAssertEqual(expectedEffect.type, "delete_surrounding_text", scenarioID)
        XCTAssertEqual(effectID, expectedEffect.effectID, scenarioID)
        XCTAssertEqual(before, expectedEffect.before, scenarioID)
        XCTAssertEqual(after, expectedEffect.after, scenarioID)
      case .switchInputMode(let effectID, let mode):
        XCTAssertEqual(expectedEffect.type, "switch_input_mode", scenarioID)
        XCTAssertEqual(effectID, expectedEffect.effectID, scenarioID)
        XCTAssertEqual(mode, expectedEffect.mode, scenarioID)
      case .notify(let effectID, let message):
        XCTAssertEqual(expectedEffect.type, "notify", scenarioID)
        XCTAssertEqual(effectID, expectedEffect.effectID, scenarioID)
        XCTAssertEqual(message, expectedEffect.message, scenarioID)
      case .scheduleLiveConversion:
        XCTFail("Unexpected schedule_live_conversion effect: \(scenarioID)")
      }
    }
  }
}

private extension Collection {
  subscript(safe index: Index) -> Element? {
    indices.contains(index) ? self[index] : nil
  }
}
