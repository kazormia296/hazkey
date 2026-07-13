import Foundation
import SwiftProtobuf
import XCTest

@testable import hazkey_server

final class GrimodexSessionCapacityProtocolTests: XCTestCase {
  func testOpenSessionReturnsFailedWithoutEvictingForeignOwnersAtGlobalCapacity() throws {
    let registry = HazkeySessionRegistry(
      maximumSessions: 2,
      maximumSessionsPerOwner: 2
    )
    let firstSession = registry.open(
      clientContext: context(program: "grimodex"),
      ownerFd: 10
    )
    let secondSession = registry.open(
      clientContext: context(program: "firefox"),
      ownerFd: 11
    )
    let handler = ProtocolHandler(sessionRegistry: registry)
    let request = Hazkey_RequestEnvelope.with {
      $0.openSession = Hazkey_OpenSession.with {
        $0.client = Hazkey_ClientContext.with {
          $0.program = "attacker"
          $0.frontend = "wayland"
        }
      }
    }

    let responseData = handler.processProto(
      data: try request.serializedData(),
      clientFd: 12
    )
    let response = try Hazkey_ResponseEnvelope(serializedBytes: responseData)

    XCTAssertEqual(response.status, .failed)
    XCTAssertEqual(response.errorMessage, "Session capacity exhausted")
    XCTAssertNil(response.payload)
    XCTAssertNotNil(registry.semanticController(for: firstSession, ownerFd: 10))
    XCTAssertNotNil(registry.semanticController(for: secondSession, ownerFd: 11))
    XCTAssertEqual(registry.count, 2)
  }

  private func context(program: String) -> GrimodexClientContext {
    GrimodexClientContext(
      program: program,
      frontend: "wayland",
      secureInput: false
    )
  }
}
