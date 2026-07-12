import Foundation
import XCTest

@testable import hazkey_server

final class GrimodexDiagnosticsTests: XCTestCase {
  func testDiagnosticSnapshotPreservesRuntimeSessionAndScopeReasons() {
    let snapshot = GrimodexDiagnosticsSnapshot(
      watcherActive: true,
      consumerRegistered: true,
      loadDiagnostic: .loaded,
      generation: 7,
      activeProjectID: "project-a",
      activeSessions: 2,
      clientContext: GrimodexClientContext(
        program: "firefox",
        frontend: "wayland",
        secureInput: false
      ),
      scopeDecision: GrimodexScopeDecision(
        allowsGrimodexIntegration: false,
        allowsLearning: true,
        reason: .otherProgram
      )
    )

    let wire = snapshot.protobuf
    XCTAssertTrue(wire.watcherActive)
    XCTAssertTrue(wire.consumerRegistered)
    XCTAssertEqual(wire.snapshotStatus, "loaded")
    XCTAssertEqual(wire.generation, 7)
    XCTAssertEqual(wire.activeProjectID, "project-a")
    XCTAssertEqual(wire.activeSessions, 2)
    XCTAssertEqual(wire.program, "firefox")
    XCTAssertEqual(wire.frontend, "wayland")
    XCTAssertFalse(wire.secureInput)
    XCTAssertFalse(wire.integrationAllowed)
    XCTAssertEqual(wire.scopeReason, .otherProgram)
  }

  func testEveryScopeReasonHasAStableWireValue() {
    let cases: [(GrimodexScopeReason, Hazkey_Config_GrimodexDiagnostics.ScopeReason)] = [
      (.allowedGrimodex, .allowedGrimodex),
      (.allowedAllApplications, .allowedAllApplications),
      (.disabled, .disabled),
      (.secureInput, .secureInput),
      (.unknownProgram, .unknownProgram),
      (.otherProgram, .otherProgram),
    ]

    for (reason, expected) in cases {
      XCTAssertEqual(reason.protobuf, expected)
    }
  }
}
