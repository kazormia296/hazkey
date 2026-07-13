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

    let responseA = try process(
      actionRequest(sessionA, requestID: "insert-a") {
        $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "あ" }
      },
      handler: handler,
      clientFd: 10
    )
    let responseB = try process(
      actionRequest(sessionB, requestID: "insert-b") {
        $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "い" }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(responseA.status, .success)
    XCTAssertEqual(responseB.status, .success)
    XCTAssertEqual(snapshotText(responseA), "あ")
    XCTAssertEqual(snapshotText(responseB), "い")
  }

  func testMissingAndForeignOwnerSessionsReturnDedicatedStatus() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")

    for (sessionID, clientFd) in [("missing", Int32(10)), (session, Int32(11))] {
      let response = try process(
        actionRequest(sessionID, requestID: "owned-\(clientFd)") {
          $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "あ" }
        },
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
        actionRequest(session, requestID: "after-close") {
          $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "あ" }
        },
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

  func testUserDictionaryCrudImportAndExportRemainSessionless() throws {
    let store = UserDictionaryStore()
    let registry = HazkeySessionRegistry(userDictionaryStore: store)
    let handler = ProtocolHandler(sessionRegistry: registry)

    let added = try process(
      Hazkey_RequestEnvelope.with {
        $0.addUserDictionaryEntry = Hazkey_Config_AddUserDictionaryEntry.with {
          $0.entry = Hazkey_Config_UserDictionaryEntry.with {
            $0.id = "entry-a"
            $0.reading = "せつな"
            $0.surface = "刹那"
            $0.partOfSpeech = "person"
            $0.layer = .personal
          }
        }
      },
      handler: handler,
      clientFd: 20
    )
    XCTAssertEqual(added.status, .success)
    XCTAssertEqual(added.userDictionaryResult.entries.map(\.id), ["entry-a"])

    let exported = try process(
      Hazkey_RequestEnvelope.with { $0.exportUserDictionary = .init() },
      handler: handler,
      clientFd: 21
    )
    XCTAssertEqual(exported.status, .success)
    XCTAssertFalse(exported.userDictionaryResult.exportedJson.isEmpty)

    let removed = try process(
      Hazkey_RequestEnvelope.with {
        $0.removeUserDictionaryEntry = Hazkey_Config_RemoveUserDictionaryEntry.with {
          $0.id = "entry-a"
        }
      },
      handler: handler,
      clientFd: 22
    )
    XCTAssertEqual(removed.status, .success)
    XCTAssertTrue(removed.userDictionaryResult.entries.isEmpty)

    let imported = try process(
      Hazkey_RequestEnvelope.with {
        $0.importUserDictionary = Hazkey_Config_ImportUserDictionary.with {
          $0.json = exported.userDictionaryResult.exportedJson
          $0.merge = true
        }
      },
      handler: handler,
      clientFd: 23
    )
    XCTAssertEqual(imported.status, .success)
    XCTAssertEqual(imported.userDictionaryResult.entries.map(\.id), ["entry-a"])
  }

  func testUserDictionaryRejectsProjectLayerMutation() throws {
    let handler = ProtocolHandler(
      sessionRegistry: HazkeySessionRegistry(userDictionaryStore: UserDictionaryStore())
    )
    let response = try process(
      Hazkey_RequestEnvelope.with {
        $0.addUserDictionaryEntry = Hazkey_Config_AddUserDictionaryEntry.with {
          $0.entry = Hazkey_Config_UserDictionaryEntry.with {
            $0.id = "project-entry"
            $0.reading = "せつな"
            $0.surface = "刹那"
            $0.partOfSpeech = "person"
            $0.layer = .project
          }
        }
      },
      handler: handler,
      clientFd: 20
    )

    XCTAssertEqual(response.status, .failed)
    XCTAssertFalse(response.errorMessage.isEmpty)
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
    XCTAssertTrue(response.currentConfig.hasZenzaiRuntimeDiagnostics)
    XCTAssertEqual(response.currentConfig.grimodexDiagnostics.generation, 9)
    XCTAssertEqual(
      response.currentConfig.grimodexDiagnostics.scopeReason,
      .allowedGrimodex
    )
  }

  func testHandleImeActionWithoutAnActionReturnsMalformedRequest() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")

    let response = try process(
      actionRequest(session, requestID: "missing-action") { action in
        action.expectedRevision = 0
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(response.status, .malformedRequest)
    XCTAssertFalse(response.errorMessage.isEmpty)
  }

  func testOutOfRangeSurroundingContextAnchorReturnsMalformedRequest() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")

    let response = try process(
      actionRequest(session, requestID: "bad-context") {
        $0.updateSurroundingContext = Hazkey_Commands_UpdateSurroundingContext.with {
          $0.text = "abc"
          $0.anchor = 4
        }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(response.status, .malformedRequest)
    XCTAssertFalse(response.errorMessage.isEmpty)
  }

  func testSurroundingContextAcceptsFcitxUnicodeScalarAnchor() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")
    let context = "e\u{301}👨‍👩‍👧‍👦後"

    let response = try process(
      actionRequest(session, requestID: "unicode-context") {
        $0.updateSurroundingContext = Hazkey_Commands_UpdateSurroundingContext.with {
          $0.text = context
          $0.anchor = UInt32(context.unicodeScalars.count - 1)
        }
      },
      handler: handler,
      clientFd: 10
    )

    XCTAssertEqual(context.count, 3)
    XCTAssertEqual(context.unicodeScalars.count, 10)
    XCTAssertEqual(response.status, .success)
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

  func testV2ActionRoundTripIsIdempotentAndRejectsStaleCandidates() throws {
    let registry = HazkeySessionRegistry()
    let handler = ProtocolHandler(sessionRegistry: registry)
    let session = try openSession(handler: handler, clientFd: 10, program: "grimodex")

    let insert = sessionRequest(session) {
      $0.handleImeAction = Hazkey_Commands_HandleImeAction.with {
        $0.requestID = "insert-1"
        $0.expectedRevision = 0
        $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "かな" }
      }
    }
    let insertResponse = try process(insert, handler: handler, clientFd: 10)
    XCTAssertEqual(insertResponse.status, .success)
    XCTAssertEqual(insertResponse.handleImeActionResult.snapshot.phase, .composing)

    let duplicate = try process(insert, handler: handler, clientFd: 10)
    XCTAssertEqual(duplicate, insertResponse)

    let convert = sessionRequest(session) {
      $0.handleImeAction = Hazkey_Commands_HandleImeAction.with {
        $0.requestID = "convert-1"
        $0.expectedRevision = insertResponse.handleImeActionResult.snapshot.revision
        $0.startConversion = .init()
      }
    }
    let convertResponse = try process(convert, handler: handler, clientFd: 10)
    XCTAssertEqual(convertResponse.status, .success)
    let generation = convertResponse.handleImeActionResult.snapshot.candidateWindow.generation
    XCTAssertGreaterThan(generation, 0)

    let stale = sessionRequest(session) {
      $0.handleImeAction = Hazkey_Commands_HandleImeAction.with {
        $0.requestID = "stale-candidate"
        $0.expectedRevision = convertResponse.handleImeActionResult.snapshot.revision
        $0.selectCandidate = Hazkey_Commands_SelectCandidate.with {
          $0.candidateID = "old"
          $0.generation = generation - 1
        }
      }
    }
    let staleResponse = try process(stale, handler: handler, clientFd: 10)
    XCTAssertEqual(staleResponse.status, .staleCandidate)
    XCTAssertTrue(staleResponse.handleImeActionResult.snapshot.effects.isEmpty)
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

  private func actionRequest(
    _ sessionID: String,
    requestID: String,
    expectedRevision: UInt64 = 0,
    configure: (inout Hazkey_Commands_HandleImeAction) -> Void
  ) -> Hazkey_RequestEnvelope {
    sessionRequest(sessionID) {
      $0.handleImeAction = Hazkey_Commands_HandleImeAction.with {
        $0.requestID = requestID
        $0.expectedRevision = expectedRevision
        configure(&$0)
      }
    }
  }

  private func snapshotText(_ response: Hazkey_ResponseEnvelope) -> String {
    response.handleImeActionResult.snapshot.preedit.map(\.text).joined()
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
