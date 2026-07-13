import Foundation
import XCTest

@testable import hazkey_server

final class GrimodexProductIdentityTests: XCTestCase {
  func testProductNamesAndXdgPathsAreIndependentFromHazkey() {
    let paths = GrimodexProductPaths(
      environment: [
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "XDG_CONFIG_HOME": "/xdg/config",
        "XDG_DATA_HOME": "/xdg/data",
        "XDG_STATE_HOME": "/xdg/state",
        "XDG_CACHE_HOME": "/xdg/cache",
      ],
      homeDirectory: URL(fileURLWithPath: "/home/writer", isDirectory: true),
      uid: 1000
    )

    XCTAssertEqual(GrimodexProductPaths.packageName, "fcitx5-grimodex")
    XCTAssertEqual(GrimodexProductPaths.serverExecutableName, "fcitx5-grimodex-server")
    XCTAssertEqual(GrimodexProductPaths.settingsExecutableName, "fcitx5-grimodex-settings")
    XCTAssertEqual(paths.runtimeDirectory.path, "/run/user/1000/fcitx5-grimodex")
    XCTAssertEqual(paths.socketURL.path, "/run/user/1000/fcitx5-grimodex/server.sock")
    XCTAssertEqual(paths.lockURL.path, "/run/user/1000/fcitx5-grimodex/server.lock")
    XCTAssertEqual(paths.configDirectory.path, "/xdg/config/fcitx5-grimodex")
    XCTAssertEqual(paths.dataDirectory.path, "/xdg/data/fcitx5-grimodex")
    XCTAssertEqual(paths.stateDirectory.path, "/xdg/state/fcitx5-grimodex")
    XCTAssertEqual(paths.cacheDirectory.path, "/xdg/cache/fcitx5-grimodex")
  }

  func testFallbackPathsStayPrivateAndMatchEveryClient() {
    let paths = GrimodexProductPaths(
      environment: [:],
      homeDirectory: URL(fileURLWithPath: "/home/writer", isDirectory: true),
      uid: 42
    )

    XCTAssertEqual(paths.runtimeDirectory.path, "/tmp/fcitx5-grimodex-42")
    XCTAssertEqual(paths.socketURL.path, "/tmp/fcitx5-grimodex-42/server.sock")
    XCTAssertEqual(paths.lockURL.path, "/tmp/fcitx5-grimodex-42/server.lock")
    XCTAssertEqual(paths.configDirectory.path, "/home/writer/.config/fcitx5-grimodex")
    XCTAssertEqual(paths.dataDirectory.path, "/home/writer/.local/share/fcitx5-grimodex")
    XCTAssertEqual(paths.stateDirectory.path, "/home/writer/.local/state/fcitx5-grimodex")
    XCTAssertEqual(paths.cacheDirectory.path, "/home/writer/.cache/fcitx5-grimodex")
  }

  func testOverlongRuntimePathUsesSharedFallback() {
    let paths = GrimodexProductPaths(
      environment: [
        "XDG_RUNTIME_DIR":
          "/tmp/grimodex-fcitx5-grimodex-server-process-e2e-12345678-1234-1234-1234-123456789012/runtime"
      ],
      homeDirectory: URL(fileURLWithPath: "/home/writer", isDirectory: true),
      uid: 1000
    )

    XCTAssertEqual(paths.runtimeDirectory.path, "/tmp/fcitx5-grimodex-1000")
    XCTAssertEqual(paths.socketURL.path, "/tmp/fcitx5-grimodex-1000/server.sock")
    XCTAssertLessThan(paths.socketURL.path.utf8.count, 108)
  }
}
