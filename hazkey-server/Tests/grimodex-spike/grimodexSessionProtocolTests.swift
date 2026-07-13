import Foundation
import SwiftProtobuf
import XCTest

@testable import hazkey_server

final class GrimodexSessionProtocolTests: XCTestCase {
  func testSessionIDRoundTripsAlongsideAConversionPayload() throws {
    let request = Hazkey_RequestEnvelope.with {
      $0.sessionID = "session-a"
      $0.handleImeAction = Hazkey_Commands_HandleImeAction.with {
        $0.requestID = "request-a"
        $0.expectedRevision = 4
        $0.insertText = Hazkey_Commands_InsertText.with { $0.text = "a" }
      }
    }

    let decoded = try Hazkey_RequestEnvelope(serializedBytes: request.serializedData())

    XCTAssertEqual(decoded.sessionID, "session-a")
    guard case .handleImeAction(let command) = decoded.payload else {
      return XCTFail("handle_ime_action payload was not preserved")
    }
    XCTAssertEqual(command.requestID, "request-a")
    XCTAssertEqual(command.expectedRevision, 4)
    XCTAssertEqual(command.insertText.text, "a")
  }

  func testOpenAndCloseSessionMessagesRoundTripClientIdentity() throws {
    let open = Hazkey_RequestEnvelope.with {
      $0.openSession = Hazkey_OpenSession.with {
        $0.client = Hazkey_ClientContext.with {
          $0.program = "grimodex"
          $0.frontend = "wayland"
          $0.secureInput = true
        }
      }
    }
    let decodedOpen = try Hazkey_RequestEnvelope(serializedBytes: open.serializedData())
    guard case .openSession(let command) = decodedOpen.payload else {
      return XCTFail("open_session payload was not preserved")
    }
    XCTAssertEqual(command.client.program, "grimodex")
    XCTAssertEqual(command.client.frontend, "wayland")
    XCTAssertTrue(command.client.secureInput)

    let close = Hazkey_RequestEnvelope.with {
      $0.closeSession = Hazkey_CloseSession.with { $0.sessionID = "session-a" }
    }
    let decodedClose = try Hazkey_RequestEnvelope(serializedBytes: close.serializedData())
    guard case .closeSession(let command) = decodedClose.payload else {
      return XCTFail("close_session payload was not preserved")
    }
    XCTAssertEqual(command.sessionID, "session-a")
  }

  func testOpenResultAndSessionNotFoundStatusRoundTrip() throws {
    let response = Hazkey_ResponseEnvelope.with {
      $0.status = .sessionNotFound
      $0.openSessionResult = Hazkey_OpenSessionResult.with {
        $0.sessionID = "replacement-session"
      }
    }

    let decoded = try Hazkey_ResponseEnvelope(serializedBytes: response.serializedData())

    XCTAssertEqual(decoded.status, .sessionNotFound)
    XCTAssertEqual(decoded.status.rawValue, 3)
    XCTAssertEqual(decoded.openSessionResult.sessionID, "replacement-session")
  }

  func testGrimodexScopeDefaultsFailClosedAndRoundTripsExplicitAllApps() throws {
    var profile = Hazkey_Config_Profile()
    XCTAssertEqual(profile.grimodexScopeMode, .grimodexOnly)

    profile.grimodexScopeMode = .grimodexAllApplications
    let decoded = try Hazkey_Config_Profile(serializedBytes: profile.serializedData())

    XCTAssertTrue(decoded.hasGrimodexScopeMode)
    XCTAssertEqual(decoded.grimodexScopeMode, .grimodexAllApplications)
  }
}
