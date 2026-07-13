import Foundation
import XCTest

@testable import hazkey_server

final class GrimodexProcessE2ETests: XCTestCase {
  func testRealServerAppliesProjectSnapshotsOnlyInsideTheConfiguredScope() throws {
    guard
      let executablePath = ProcessInfo.processInfo.environment[
        "GRIMODEX_PROCESS_E2E_SERVER"
      ],
      !executablePath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER to run the real-server Unix socket acceptance test"
      )
    }
    XCTAssertTrue(
      FileManager.default.isExecutableFile(atPath: executablePath),
      "The process E2E server must be an executable built from this checkout"
    )

    let fixture = try GrimodexProcessSnapshotFixture()
    defer { fixture.remove() }
    try fixture.publish(
      projectID: "project-a",
      surface: "Grimodex工程A"
    )

    let server = GrimodexProcessHarness(
      executableURL: URL(fileURLWithPath: executablePath),
      grimodexRootURL: fixture.rootURL
    )
    try server.start()
    defer { server.stop() }
    try server.assertPrivateIPC()

    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }

    let grimodexSession = try client.openSession(program: "grimodex")
    let initialDiagnostics = try client.waitForDiagnostics {
      $0.snapshotStatus == "loaded" && $0.activeProjectID == "project-a"
    }
    XCTAssertTrue(initialDiagnostics.integrationAllowed)
    XCTAssertEqual(initialDiagnostics.scopeReason, .allowedGrimodex)

    let projectACandidates = try client.convertDirect(
      "せつな",
      sessionID: grimodexSession
    )
    XCTAssertEqual(
      projectACandidates.first,
      "Grimodex工程A",
      "the active Grimodex term must be the real converter's top candidate"
    )

    try fixture.publish(
      projectID: "project-b",
      surface: "Grimodex工程B"
    )
    let projectBDiagnostics = try client.waitForDiagnostics {
      $0.snapshotStatus == "loaded" && $0.activeProjectID == "project-b"
    }
    XCTAssertGreaterThan(projectBDiagnostics.generation, initialDiagnostics.generation)
    XCTAssertEqual(
      try client.candidates(sessionID: grimodexSession).first,
      "Grimodex工程A",
      "a project update must not mutate an in-progress composition"
    )
    XCTAssertEqual(
      try client.convertDirect("せつな", sessionID: grimodexSession).first,
      "Grimodex工程B",
      "the next composition must use the newly active project"
    )

    let otherApplicationSession = try client.openSession(program: "firefox")
    XCTAssertFalse(
      try client.convertDirect("せつな", sessionID: otherApplicationSession)
        .contains("Grimodex工程B"),
      "Grimodex-only mode must not leak project terms into another application"
    )

    try client.setScope(.grimodexOff)
    XCTAssertFalse(
      try client.convertDirect("せつな", sessionID: grimodexSession)
        .contains("Grimodex工程B"),
      "turning integration off must remove the imported project dictionary"
    )

    try client.setScope(.grimodexOnly)
    try fixture.removeState()
    _ = try client.waitForDiagnostics { $0.snapshotStatus == "missingState" }
    XCTAssertFalse(
      try client.convertDirect("こんにちは", sessionID: grimodexSession).isEmpty,
      "the standalone IME must keep converting when Grimodex is not running"
    )

    try fixture.publishInvalidProject(projectID: "project-invalid")
    _ = try client.waitForDiagnostics { $0.snapshotStatus == "invalidSnapshot" }
    XCTAssertTrue(server.isRunning, "invalid Grimodex JSON must not terminate the server")
    XCTAssertFalse(
      try client.convertDirect("こんにちは", sessionID: grimodexSession).isEmpty,
      "invalid snapshot JSON must fail closed without breaking normal conversion"
    )
  }

  func testProtocolV2RealServerRoundTripIsIdempotentAndRecoverable() throws {
    guard
      let executablePath = ProcessInfo.processInfo.environment[
        "GRIMODEX_PROCESS_E2E_SERVER"
      ],
      !executablePath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER to run the protocol-v2 process test"
      )
    }

    let fixture = try GrimodexProcessSnapshotFixture()
    defer { fixture.remove() }
    try fixture.publish(projectID: "project-v2", surface: "Grimodex工程V2")

    let server = GrimodexProcessHarness(
      executableURL: URL(fileURLWithPath: executablePath),
      grimodexRootURL: fixture.rootURL
    )
    try server.start()
    defer { server.stop() }

    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }
    let open = try client.openSessionInfo(program: "grimodex")
    XCTAssertEqual(open.protocolVersion, 2)
    XCTAssertTrue(open.idempotentRequestSupport)
    XCTAssertTrue(open.recoverySupport)

    let inserted = try client.transactV2(
      sessionID: open.sessionID,
      requestID: "v2-insert",
      expectedRevision: 0
    ) {
      $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "setsuna" }
    }
    XCTAssertEqual(inserted.status, .success)
    let insertedSnapshot = inserted.handleImeActionResult.snapshot
    XCTAssertEqual(insertedSnapshot.phase, .composing)
    XCTAssertEqual(insertedSnapshot.preedit.map(\.text).joined(), "せつな")
    XCTAssertTrue(insertedSnapshot.hasRecovery)

    let converted = try client.transactV2(
      sessionID: open.sessionID,
      requestID: "v2-convert",
      expectedRevision: insertedSnapshot.revision
    ) {
      $0.startConversion = .init()
    }
    XCTAssertEqual(converted.status, .success)
    let convertedSnapshot = converted.handleImeActionResult.snapshot
    XCTAssertEqual(convertedSnapshot.phase, .previewing)
    XCTAssertEqual(
      convertedSnapshot.candidateWindow.items.first?.text,
      "Grimodex工程V2"
    )

    let duplicate = try client.transactV2(
      sessionID: open.sessionID,
      requestID: "v2-convert",
      expectedRevision: insertedSnapshot.revision
    ) {
      $0.startConversion = .init()
    }
    XCTAssertEqual(try duplicate.serializedData(), try converted.serializedData())

    let candidate = try XCTUnwrap(convertedSnapshot.candidateWindow.items.first)
    let stale = try client.transactV2(
      sessionID: open.sessionID,
      requestID: "v2-stale-candidate",
      expectedRevision: convertedSnapshot.revision
    ) {
      $0.selectCandidate = Hazkey_Commands_SelectCandidate.with {
        $0.candidateID = candidate.id
        $0.generation = convertedSnapshot.candidateWindow.generation &- 1
      }
    }
    XCTAssertEqual(stale.status, .staleCandidate)
    XCTAssertTrue(stale.handleImeActionResult.snapshot.effects.isEmpty)

    let committed = try client.transactV2(
      sessionID: open.sessionID,
      requestID: "v2-commit-candidate",
      expectedRevision: convertedSnapshot.revision
    ) {
      $0.selectCandidate = Hazkey_Commands_SelectCandidate.with {
        $0.candidateID = candidate.id
        $0.generation = convertedSnapshot.candidateWindow.generation
      }
    }
    XCTAssertEqual(committed.status, .success)
    XCTAssertEqual(committed.handleImeActionResult.snapshot.effects.first?.text, candidate.text)

    let recoveryClient = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { recoveryClient.close() }
    let recoverySession = try recoveryClient.openSessionInfo(program: "grimodex")
    let restored = try recoveryClient.transactV2(
      sessionID: recoverySession.sessionID,
      requestID: "v2-restore",
      expectedRevision: 0
    ) {
      $0.restoreCheckpoint = Hazkey_Commands_RestoreCheckpoint.with {
        $0.opaqueState = convertedSnapshot.recovery.opaqueState
      }
    }
    XCTAssertEqual(restored.status, .success)
    XCTAssertEqual(
      restored.handleImeActionResult.snapshot.preedit.map(\.text).joined(),
      "せつな"
    )
    XCTAssertTrue(restored.handleImeActionResult.snapshot.candidateWindow.items.isEmpty)
  }

  func testProtocolV2P1UserDictionaryUnicodeForgetAndReconversion() throws {
    guard
      let executablePath = ProcessInfo.processInfo.environment[
        "GRIMODEX_PROCESS_E2E_SERVER"
      ],
      !executablePath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER to run the protocol-v2 P1 process test"
      )
    }

    let fixture = try GrimodexProcessSnapshotFixture()
    defer { fixture.remove() }
    try fixture.publish(projectID: "project-p1", surface: "再変換成功")
    let server = GrimodexProcessHarness(
      executableURL: URL(fileURLWithPath: executablePath),
      grimodexRootURL: fixture.rootURL
    )
    try server.start()
    defer { server.stop() }
    let client = try GrimodexProcessClient.connect(to: server.socketURL)
    defer { client.close() }

    let dictionary = try client.addUserDictionaryEntry(
      id: "p1-personal-entry",
      reading: "ぐりもでっくすじしょ",
      surface: "個人辞書成功"
    )
    XCTAssertEqual(dictionary.entries.map(\.id), ["p1-personal-entry"])

    let dictionarySession = try client.openSessionInfo(program: "firefox")
    let inserted = try client.transactV2(
      sessionID: dictionarySession.sessionID,
      requestID: "p1-dictionary-insert",
      expectedRevision: 0
    ) {
      $0.insertText = Hazkey_Commands_InsertText.with {
        $0.text = "ぐりもでっくすじしょ"
      }
    }
    let converted = try client.transactV2(
      sessionID: dictionarySession.sessionID,
      requestID: "p1-dictionary-convert",
      expectedRevision: inserted.handleImeActionResult.snapshot.revision
    ) {
      $0.startConversion = .init()
    }
    let personalCandidates = converted.handleImeActionResult.snapshot
      .candidateWindow.items.map(\.text)
    XCTAssertTrue(
      personalCandidates.contains("個人辞書成功"),
      "personal dictionary candidate missing: \(personalCandidates)"
    )

    let unicodeSession = try client.openSessionInfo(program: "firefox")
    var unicode = try client.transactV2(
      sessionID: unicodeSession.sessionID,
      requestID: "p1-unicode-begin",
      expectedRevision: 0
    ) {
      $0.beginUnicodeInput = .init()
    }
    for (index, digit) in ["1", "f", "6", "0", "0"].enumerated() {
      unicode = try client.transactV2(
        sessionID: unicodeSession.sessionID,
        requestID: "p1-unicode-digit-\(index)",
        expectedRevision: unicode.handleImeActionResult.snapshot.revision
      ) {
        $0.appendUnicodeDigit = Hazkey_Commands_AppendUnicodeDigit.with {
          $0.digit = digit
        }
      }
    }
    unicode = try client.transactV2(
      sessionID: unicodeSession.sessionID,
      requestID: "p1-unicode-finish",
      expectedRevision: unicode.handleImeActionResult.snapshot.revision
    ) {
      $0.commitUnicodeInput = .init()
    }
    XCTAssertEqual(
      unicode.handleImeActionResult.snapshot.preedit.map(\.text).joined(),
      "😀"
    )
    let unicodeCommit = try client.transactV2(
      sessionID: unicodeSession.sessionID,
      requestID: "p1-unicode-commit",
      expectedRevision: unicode.handleImeActionResult.snapshot.revision
    ) {
      $0.commitAll = .init()
    }
    XCTAssertEqual(unicodeCommit.handleImeActionResult.snapshot.effects.first?.text, "😀")

    let reconversionSession = try client.openSessionInfo(program: "grimodex")
    let reconverted = try client.transactV2(
      sessionID: reconversionSession.sessionID,
      requestID: "p1-reconvert",
      expectedRevision: 0
    ) {
      $0.reconvert = Hazkey_Commands_Reconvert.with {
        $0.text = "せつな"
        $0.leftContext = "左"
        $0.rightContext = "右"
        $0.deleteBefore = 3
      }
    }
    let candidate = try XCTUnwrap(
      reconverted.handleImeActionResult.snapshot.candidateWindow.items.first
    )
    let forgotten = try client.transactV2(
      sessionID: reconversionSession.sessionID,
      requestID: "p1-forget",
      expectedRevision: reconverted.handleImeActionResult.snapshot.revision
    ) {
      $0.forgetCandidate = Hazkey_Commands_ForgetCandidate.with {
        $0.candidateID = candidate.id
        $0.generation = reconverted.handleImeActionResult.snapshot
          .candidateWindow.generation
      }
    }
    XCTAssertEqual(forgotten.status, .success)
    XCTAssertTrue(forgotten.handleImeActionResult.snapshot.effects.isEmpty)

    let replacement = try client.transactV2(
      sessionID: reconversionSession.sessionID,
      requestID: "p1-reconvert-commit",
      expectedRevision: forgotten.handleImeActionResult.snapshot.revision
    ) {
      $0.commitSelected = .init()
    }
    let effects = replacement.handleImeActionResult.snapshot.effects
    XCTAssertEqual(effects.count, 2)
    XCTAssertEqual(effects[0].type, .deleteSurroundingText)
    XCTAssertEqual(effects[0].before, 3)
    XCTAssertEqual(effects[1].type, .commitText)
  }

  func testProtocolV2KeepsThirtyTwoLiveCompositionsIsolated() throws {
    guard
      let executablePath = ProcessInfo.processInfo.environment[
        "GRIMODEX_PROCESS_E2E_SERVER"
      ],
      !executablePath.isEmpty
    else {
      throw XCTSkip(
        "Set GRIMODEX_PROCESS_E2E_SERVER to run the protocol-v2 session stress test"
      )
    }

    let fixture = try GrimodexProcessSnapshotFixture()
    defer { fixture.remove() }
    try fixture.publish(projectID: "project-stress", surface: "並行変換")
    let server = GrimodexProcessHarness(
      executableURL: URL(fileURLWithPath: executablePath),
      grimodexRootURL: fixture.rootURL
    )
    try server.start()
    defer { server.stop() }

    let clientCount = 8
    let sessionsPerClient = 4
    var clients: [GrimodexProcessClient] = []
    defer { clients.forEach { $0.close() } }
    for _ in 0..<clientCount {
      clients.append(try GrimodexProcessClient.connect(to: server.socketURL))
    }

    var sessions: [(
      clientIndex: Int,
      sessionID: String,
      text: String,
      revision: UInt64
    )] = []
    let clientLabels = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
    let sessionLabels = ["一", "二", "三", "四"]
    for clientIndex in clients.indices {
      for localIndex in 0..<sessionsPerClient {
        let session = try clients[clientIndex].openSessionInfo(program: "stress")
        let text = "識別\(clientLabels[clientIndex])・\(sessionLabels[localIndex])"
        let inserted = try clients[clientIndex].transactV2(
          sessionID: session.sessionID,
          requestID: "stress-insert-\(clientIndex)-\(localIndex)",
          expectedRevision: 0
        ) {
          $0.insertText = Hazkey_Commands_InsertText.with { $0.text = text }
        }
        XCTAssertEqual(inserted.status, .success)
        XCTAssertEqual(
          inserted.handleImeActionResult.snapshot.preedit.map(\.text).joined(),
          text
        )
        sessions.append((
          clientIndex,
          session.sessionID,
          text,
          inserted.handleImeActionResult.snapshot.revision
        ))
      }
    }
    XCTAssertEqual(sessions.count, clientCount * sessionsPerClient)

    // Interleave owners and sessions in reverse order. A no-op cursor move
    // still returns a fresh authoritative snapshot and therefore detects
    // accidental state sharing without changing the expected text.
    for index in sessions.indices.reversed() {
      let value = sessions[index]
      let checked = try clients[value.clientIndex].transactV2(
        sessionID: value.sessionID,
        requestID: "stress-check-\(index)",
        expectedRevision: value.revision
      ) {
        $0.moveCursorV2 = Hazkey_Commands_MoveCursor.with { $0.offset = 0 }
      }
      XCTAssertEqual(checked.status, .success)
      XCTAssertEqual(
        checked.handleImeActionResult.snapshot.preedit.map(\.text).joined(),
        value.text,
        "session \(index) observed another composition"
      )
      sessions[index].revision = checked.handleImeActionResult.snapshot.revision
    }

    for index in sessions.indices where index.isMultiple(of: 2) {
      let value = sessions[index]
      let cancelled = try clients[value.clientIndex].transactV2(
        sessionID: value.sessionID,
        requestID: "stress-cancel-\(index)",
        expectedRevision: value.revision
      ) {
        $0.cancel = .init()
      }
      XCTAssertEqual(cancelled.handleImeActionResult.snapshot.phase, .idle)
    }

    for index in sessions.indices where !index.isMultiple(of: 2) {
      let value = sessions[index]
      let survivor = try clients[value.clientIndex].transactV2(
        sessionID: value.sessionID,
        requestID: "stress-survivor-\(index)",
        expectedRevision: value.revision
      ) {
        $0.moveCursorV2 = Hazkey_Commands_MoveCursor.with { $0.offset = 0 }
      }
      XCTAssertEqual(survivor.status, .success)
      XCTAssertEqual(
        survivor.handleImeActionResult.snapshot.preedit.map(\.text).joined(),
        value.text,
        "cancelling another session changed survivor \(index)"
      )
    }
  }
}
