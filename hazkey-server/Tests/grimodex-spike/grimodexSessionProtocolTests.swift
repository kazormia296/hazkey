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

  func testMoveActiveSegmentRoundTripsAndDecodesAsASemanticAction() throws {
    let request = Hazkey_Commands_HandleImeAction.with {
      $0.requestID = "move-segment"
      $0.expectedRevision = 0
      $0.moveActiveSegment = Hazkey_Commands_MoveActiveSegment.with {
        $0.offset = -1
      }
    }

    let decoded = try Hazkey_Commands_HandleImeAction(
      serializedBytes: request.serializedData()
    )
    guard case .moveActiveSegment(let command) = decoded.action else {
      return XCTFail("move_active_segment action was not preserved")
    }
    XCTAssertEqual(command.offset, -1)

    let response = ImeV2SessionController().handle(decoded)
    XCTAssertEqual(response.status, .invalidAction)
    XCTAssertEqual(response.handleImeActionResult.status, .invalidAction)
  }

  func testPendingLearningResolutionAndSnapshotFlagRoundTrip() throws {
    let request = Hazkey_Commands_HandleImeAction.with {
      $0.requestID = "resolve-learning"
      $0.resolvePendingLearning = Hazkey_Commands_ResolvePendingLearning.with {
        $0.commit = false
      }
    }

    let decoded = try Hazkey_Commands_HandleImeAction(
      serializedBytes: request.serializedData()
    )
    guard case .resolvePendingLearning(let command) = decoded.action else {
      return XCTFail("resolve_pending_learning action was not preserved")
    }
    XCTAssertFalse(command.commit)
    XCTAssertEqual(
      ImeV2SessionController().handle(decoded).handleImeActionResult.status,
      .success
    )

    let snapshot = Hazkey_SessionSnapshot.with { $0.pendingLearning = true }
    let roundTripped = try Hazkey_SessionSnapshot(
      serializedBytes: snapshot.serializedData()
    )
    XCTAssertTrue(roundTripped.pendingLearning)
  }

  func testLegacyCodableSnapshotDefaultsPendingLearningToFalse() throws {
    let encoded = try JSONEncoder().encode(ImeReducer().currentSnapshot())
    var object = try XCTUnwrap(
      JSONSerialization.jsonObject(with: encoded) as? [String: Any]
    )
    object.removeValue(forKey: "pendingLearning")
    let legacy = try JSONSerialization.data(withJSONObject: object)

    let decoded = try JSONDecoder().decode(SessionSnapshot.self, from: legacy)
    XCTAssertFalse(decoded.pendingLearning)
  }

  func testOpenAndCloseSessionMessagesRoundTripClientIdentity() throws {
    let open = Hazkey_RequestEnvelope.with {
      $0.openSession = Hazkey_OpenSession.with {
        $0.clientFeatureBits = ImeV2ClientFeatures.current
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
    XCTAssertEqual(command.clientFeatureBits, ImeV2ClientFeatures.current)

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
