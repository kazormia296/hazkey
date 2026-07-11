import Foundation
import XCTest

@testable import hazkey_server

final class GrimodexRuntimeDirectoryTests: XCTestCase {
  func testCreatesAPrivateRuntimeDirectory() throws {
    let parent = temporaryParent()
    defer { try? FileManager.default.removeItem(at: parent) }
    let runtime = parent.appendingPathComponent("runtime", isDirectory: true)

    try GrimodexRuntimeDirectory.prepare(at: runtime, uid: getuid())

    let attributes = try FileManager.default.attributesOfItem(atPath: runtime.path)
    XCTAssertEqual(
      ((attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0) & 0o777,
      0o700
    )
  }

  func testRejectsSymlinkAndOverlyPermissiveRuntimeDirectories() throws {
    let parent = temporaryParent()
    defer { try? FileManager.default.removeItem(at: parent) }
    let target = parent.appendingPathComponent("target", isDirectory: true)
    try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)

    let symlink = parent.appendingPathComponent("symlink", isDirectory: true)
    try FileManager.default.createSymbolicLink(at: symlink, withDestinationURL: target)
    XCTAssertThrowsError(
      try GrimodexRuntimeDirectory.prepare(at: symlink, uid: getuid())
    )

    let publicDirectory = parent.appendingPathComponent("public", isDirectory: true)
    try FileManager.default.createDirectory(
      at: publicDirectory,
      withIntermediateDirectories: true,
      attributes: [.posixPermissions: 0o777]
    )
    try FileManager.default.setAttributes(
      [.posixPermissions: 0o777],
      ofItemAtPath: publicDirectory.path
    )
    XCTAssertThrowsError(
      try GrimodexRuntimeDirectory.prepare(at: publicDirectory, uid: getuid())
    )
  }

  private func temporaryParent() -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-runtime-dir-tests-\(UUID().uuidString)",
      isDirectory: true
    )
  }
}
