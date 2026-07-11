import Foundation
import XCTest

@testable import hazkey_server

final class GrimodexProcessIdentityTests: XCTestCase {
  func testOnlyTheCurrentUsersGrimodexServerCanBeReplaced() throws {
    let proc = FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-proc-tests-\(UUID().uuidString)",
      isDirectory: true
    )
    defer { try? FileManager.default.removeItem(at: proc) }

    try writeProcess(
      pid: 101,
      uid: 1000,
      executable: "/usr/lib/fcitx5-grimodex/fcitx5-grimodex-server",
      proc: proc
    )
    try writeProcess(
      pid: 102,
      uid: 1001,
      executable: "/usr/lib/fcitx5-grimodex/fcitx5-grimodex-server",
      proc: proc
    )
    try writeProcess(
      pid: 103,
      uid: 1000,
      executable: "/usr/lib/hazkey/hazkey-server",
      proc: proc
    )

    let verifier = ProcessIdentityVerifier(
      procRoot: proc,
      uid: 1000,
      expectedExecutableName: "fcitx5-grimodex-server"
    )

    XCTAssertTrue(verifier.canTerminate(pid: 101))
    XCTAssertFalse(verifier.canTerminate(pid: 102))
    XCTAssertFalse(verifier.canTerminate(pid: 103))
    XCTAssertFalse(verifier.canTerminate(pid: 404))
  }

  private func writeProcess(pid: Int, uid: Int, executable: String, proc: URL) throws {
    let directory = proc.appendingPathComponent(String(pid), isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    try Data("Name:\ttest\nUid:\t\(uid)\t\(uid)\t\(uid)\t\(uid)\n".utf8).write(
      to: directory.appendingPathComponent("status")
    )
    try FileManager.default.createSymbolicLink(
      atPath: directory.appendingPathComponent("exe").path,
      withDestinationPath: executable
    )
  }
}
