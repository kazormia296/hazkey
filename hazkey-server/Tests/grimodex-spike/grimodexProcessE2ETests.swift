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
}
