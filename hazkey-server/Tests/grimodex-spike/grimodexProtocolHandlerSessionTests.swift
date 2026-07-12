import Foundation
import Glibc
import SwiftProtobuf
import XCTest

@testable import hazkey_server

final class GrimodexProtocolHandlerSessionTests: XCTestCase {
  func testOpenSessionReturnsAnOwnedSessionAndPreservesClientContext() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)

    let response = try process(
      Hazkey_RequestEnvelope.with {
        $0.openSession = Hazkey_OpenSession.with {
          $0.client = Hazkey_ClientContext.with {
            $0.program = "grimodex"
            $0.frontend = "wayland"
            $0.secureInput = false
          }
        }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(response.status, .success)
    let sessionID = response.openSessionResult.sessionID
    XCTAssertFalse(sessionID.isEmpty)
    XCTAssertEqual(
      registry.clientContext(for: sessionID, ownerFd: 10),
      GrimodexClientContext(
        program: "grimodex",
        frontend: "wayland",
        secureInput: false
      )
    )
  }

  func testConversionRequestsAreRoutedToIndependentSessions() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let sessionA = try openSession(handler: handler, clientFd: 10, program: "grimodex")
    let sessionB = try openSession(handler: handler, clientFd: 10, program: "firefox")

    XCTAssertEqual(
      try process(
        sessionRequest(sessionA) {
          $0.inputChar = Hazkey_Commands_InputChar.with { $0.text = "a" }
        },
        handler: handler,
        clientFd: 10
      ).status,
      .success
    )
    XCTAssertEqual(
      try process(
        sessionRequest(sessionB) {
          $0.inputChar = Hazkey_Commands_InputChar.with { $0.text = "i" }
        },
        handler: handler,
        clientFd: 10
      ).status,
      .success
    )

    XCTAssertEqual(try composingText(sessionA, handler: handler, clientFd: 10), "あ")
    XCTAssertEqual(try composingText(sessionB, handler: handler, clientFd: 10), "い")
  }

  func testMissingAndForeignOwnerSessionsReturnDedicatedStatus() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")

    for (sessionID, clientFd) in [("missing", Int32(10)), (session, Int32(11))] {
      let response = try process(
        sessionRequest(sessionID) { $0.getCurrentInputMode = .init() },
        handler: handler,
        clientFd: clientFd
      )
      XCTAssertEqual(response.status, .sessionNotFound)
      XCTAssertFalse(response.errorMessage.contains(sessionID))
    }
  }

  func testCloseSessionEnforcesOwnershipAndInvalidatesTheID() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")
    let close = Hazkey_RequestEnvelope.with {
      $0.closeSession = Hazkey_CloseSession.with { $0.sessionID = session }
    }

    XCTAssertEqual(
      try process(close, handler: handler, clientFd: 11).status,
      .sessionNotFound
    )
    XCTAssertEqual(
      try process(close, handler: handler, clientFd: 10).status,
      .success
    )
    XCTAssertEqual(
      try process(
        sessionRequest(session) { $0.getCurrentInputMode = .init() },
        handler: handler,
        clientFd: 10
      ).status,
      .sessionNotFound
    )
  }

  func testConfigurationRequestsRemainSessionless() throws {
    let handler = ProtocolHandler(sessionRegistry: HazkeySessionRegistry())

    let response = try process(
      Hazkey_RequestEnvelope.with { $0.getConfig = .init() },
      handler: handler,
      clientFd: 20
    )

    XCTAssertEqual(response.status, .success)
    guard case .currentConfig = response.payload else {
      return XCTFail("configuration response payload was missing")
    }
  }

  func testConfigurationResponseIncludesLiveGrimodexDiagnostics() throws {
    let diagnostics = GrimodexDiagnosticsSnapshot(
      watcherActive: true,
      consumerRegistered: true,
      loadDiagnostic: .loaded,
      generation: 9,
      activeProjectID: "project-a",
      activeSessions: 1,
      clientContext: GrimodexClientContext(
        program: "grimodex",
        frontend: "wayland",
        secureInput: false
      ),
      scopeDecision: GrimodexScopeDecision(
        allowsGrimodexIntegration: true,
        allowsLearning: true,
        reason: .allowedGrimodex
      )
    )
    let handler = ProtocolHandler(
      sessionRegistry: HazkeySessionRegistry(),
      diagnosticsProvider: { diagnostics }
    )

    let response = try process(
      Hazkey_RequestEnvelope.with { $0.getConfig = .init() },
      handler: handler,
      clientFd: 20
    )

    XCTAssertEqual(response.status, .success)
    XCTAssertTrue(response.currentConfig.hasGrimodexDiagnostics)
    XCTAssertEqual(response.currentConfig.grimodexDiagnostics.generation, 9)
    XCTAssertEqual(
      response.currentConfig.grimodexDiagnostics.scopeReason,
      .allowedGrimodex
    )
  }

  func testNegativeSetContextAnchorReturnsFailed() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")

    let response = try process(
      sessionRequest(session) {
        $0.setContext = Hazkey_Commands_SetContext.with {
          $0.context = "abc"
          $0.anchor = -1
        }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(response.status, .failed)
    XCTAssertFalse(response.errorMessage.isEmpty)
  }

  func testOutOfRangeSetContextAnchorReturnsFailed() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")

    let response = try process(
      sessionRequest(session) {
        $0.setContext = Hazkey_Commands_SetContext.with {
          $0.context = "abc"
          $0.anchor = 4
        }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(response.status, .failed)
    XCTAssertFalse(response.errorMessage.isEmpty)
  }

  func testSetContextAcceptsFcitxUnicodeScalarAnchor() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")
    let context = "e\u{301}👨‍👩‍👧‍👦後"

    let response = try process(
      sessionRequest(session) {
        $0.setContext = Hazkey_Commands_SetContext.with {
          $0.context = context
          $0.anchor = Int32(context.unicodeScalars.count - 1)
        }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(context.count, 3)
    XCTAssertEqual(context.unicodeScalars.count, 10)
    XCTAssertEqual(response.status, .success)
  }

  func testNegativePrefixCompleteIndexReturnsFailed() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")
    registry.state(for: session, ownerFd: 10)?.currentCandidateList = []

    let response = try process(
      sessionRequest(session) {
        $0.prefixComplete = Hazkey_Commands_PrefixComplete.with { $0.index = -1 }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(response.status, .failed)
    XCTAssertFalse(response.errorMessage.isEmpty)
  }

  func testOutOfRangePrefixCompleteIndexReturnsFailed() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")
    registry.state(for: session, ownerFd: 10)?.currentCandidateList = []

    let response = try process(
      sessionRequest(session) {
        $0.prefixComplete = Hazkey_Commands_PrefixComplete.with { $0.index = 0 }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(response.status, .failed)
    XCTAssertFalse(response.errorMessage.isEmpty)
  }

  func testEmptyProfilesAreRejectedWithoutChangingExistingConfiguration() throws {
    try withTemporaryConfigHome { configHome in
      let config = HazkeyServerConfig()
      var preservedProfile = HazkeyServerConfig.genDefaultConfig()
      preservedProfile.profileName = "preserved-profile"
      XCTAssertEqual(
        config.setCurrentConfig([], [preservedProfile]).status,
        .success
      )

      let configPath = configHome
        .appendingPathComponent(GrimodexProductPaths.packageName, isDirectory: true)
        .appendingPathComponent("config.json", isDirectory: false)
      let contentsBeforeRequest = try Data(contentsOf: configPath)
      let handler = ProtocolHandler(
        sessionRegistry: HazkeySessionRegistry(serverConfig: config)
      )

      let response = try process(
        Hazkey_RequestEnvelope.with {
          $0.setConfig = Hazkey_Config_SetConfig.with { $0.profiles = [] }
        },
        handler: handler,
        clientFd: 20
      )

      XCTAssertEqual(response.status, .failed)
      XCTAssertFalse(response.errorMessage.isEmpty)
      XCTAssertEqual(try Data(contentsOf: configPath), contentsBeforeRequest)
      XCTAssertEqual(config.profiles.map(\.profileName), ["preserved-profile"])
      XCTAssertEqual(config.currentProfile.profileName, "preserved-profile")
    }
  }

  func testObjectShapedConfigFallsBackAndRemainsRecoverableThroughProtocol() throws {
    try withTemporaryConfigHome { configHome in
      let configDirectory = configHome.appendingPathComponent(
        GrimodexProductPaths.packageName,
        isDirectory: true
      )
      try FileManager.default.createDirectory(
        at: configDirectory,
        withIntermediateDirectories: true
      )
      let malformedConfig = Data(#"{"profiles":[]}"#.utf8)
      let configPath = configDirectory.appendingPathComponent("config.json")
      try malformedConfig.write(to: configPath)

      let config = HazkeyServerConfig()
      let handler = ProtocolHandler(
        sessionRegistry: HazkeySessionRegistry(serverConfig: config)
      )

      let fallbackResponse = try process(
        Hazkey_RequestEnvelope.with { $0.getConfig = .init() },
        handler: handler,
        clientFd: 20
      )

      XCTAssertEqual(config.profiles.count, 1)
      XCTAssertEqual(config.currentProfile.profileName, "Default")
      XCTAssertEqual(fallbackResponse.status, .success)
      XCTAssertEqual(fallbackResponse.currentConfig.profiles.map(\.profileName), ["Default"])
      XCTAssertEqual(try Data(contentsOf: configPath), malformedConfig)

      var recoveredProfile = HazkeyServerConfig.genDefaultConfig()
      recoveredProfile.profileName = "recovered-profile"
      let saveResponse = try process(
        Hazkey_RequestEnvelope.with {
          $0.setConfig = Hazkey_Config_SetConfig.with {
            $0.profiles = [recoveredProfile]
          }
        },
        handler: handler,
        clientFd: 20
      )
      let recoveredResponse = try process(
        Hazkey_RequestEnvelope.with { $0.getConfig = .init() },
        handler: handler,
        clientFd: 20
      )

      XCTAssertEqual(saveResponse.status, .success)
      XCTAssertEqual(recoveredResponse.status, .success)
      XCTAssertEqual(
        recoveredResponse.currentConfig.profiles.map(\.profileName),
        ["recovered-profile"]
      )
      XCTAssertNotEqual(try Data(contentsOf: configPath), malformedConfig)
    }
  }

  private func openSession(
    handler: ProtocolHandler,
    clientFd: Int32,
    program: String
  ) throws -> String {
    let response = try process(
      Hazkey_RequestEnvelope.with {
        $0.openSession = Hazkey_OpenSession.with {
          $0.client = Hazkey_ClientContext.with {
            $0.program = program
            $0.frontend = "wayland"
          }
        }
      },
      handler: handler,
      clientFd: clientFd
    )
    XCTAssertEqual(response.status, .success)
    return response.openSessionResult.sessionID
  }

  private func sessionRequest(
    _ sessionID: String,
    configure: (inout Hazkey_RequestEnvelope) -> Void
  ) -> Hazkey_RequestEnvelope {
    var request = Hazkey_RequestEnvelope()
    request.sessionID = sessionID
    configure(&request)
    return request
  }

  private func composingText(
    _ sessionID: String,
    handler: ProtocolHandler,
    clientFd: Int32
  ) throws -> String {
    let response = try process(
      sessionRequest(sessionID) {
        $0.getComposingString = Hazkey_Commands_GetComposingString.with {
          $0.charType = .hiragana
        }
      },
      handler: handler,
      clientFd: clientFd
    )
    XCTAssertEqual(response.status, .success)
    return response.text
  }

  private func process(
    _ request: Hazkey_RequestEnvelope,
    handler: ProtocolHandler,
    clientFd: Int32
  ) throws -> Hazkey_ResponseEnvelope {
    let data = handler.processProto(
      data: try request.serializedData(),
      clientFd: clientFd
    )
    return try Hazkey_ResponseEnvelope(serializedBytes: data)
  }

  private func withTemporaryConfigHome(
    _ body: (URL) throws -> Void
  ) throws {
    let fileManager = FileManager.default
    let temporaryRoot = fileManager.temporaryDirectory.appendingPathComponent(
      "grimodex-malformed-config-\(UUID().uuidString)",
      isDirectory: true
    )
    let configHome = temporaryRoot.appendingPathComponent("config", isDirectory: true)
    let previousConfigHome = ProcessInfo.processInfo.environment["XDG_CONFIG_HOME"]
    XCTAssertEqual(setenv("XDG_CONFIG_HOME", configHome.path, 1), 0)
    defer {
      if let previousConfigHome {
        _ = setenv("XDG_CONFIG_HOME", previousConfigHome, 1)
      } else {
        _ = unsetenv("XDG_CONFIG_HOME")
      }
      try? fileManager.removeItem(at: temporaryRoot)
    }

    try body(configHome)
  }
}
