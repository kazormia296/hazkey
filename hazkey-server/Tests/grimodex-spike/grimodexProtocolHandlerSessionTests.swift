import Foundation
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
}
