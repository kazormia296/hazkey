import Foundation
import XCTest

@testable import hazkey_server

private final class EchoSocketManagerDelegate: SocketManagerDelegate, @unchecked Sendable {
  private let lock = NSLock()
  private(set) var connected: [Int32] = []
  private(set) var disconnected: [Int32] = []

  func socketManager(
    _ manager: SocketManager,
    didReceiveData data: Data,
    from clientFd: Int32
  ) -> Data {
    data
  }

  func socketManager(_ manager: SocketManager, clientDidConnect clientFd: Int32) {
    lock.lock()
    connected.append(clientFd)
    lock.unlock()
  }

  func socketManager(_ manager: SocketManager, clientDidDisconnect clientFd: Int32) {
    lock.lock()
    disconnected.append(clientFd)
    lock.unlock()
  }
}

private final class SocketManagerRunner: @unchecked Sendable {
  let manager: SocketManager
  let stopped = DispatchSemaphore(value: 0)

  init(manager: SocketManager) {
    self.manager = manager
  }
}

final class GrimodexSocketManagerTests: XCTestCase {
  func testTwoClientsRemainConnectedAndCanInterleaveRequests() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-socket-manager-tests-\(UUID().uuidString)",
      isDirectory: true
    )
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: root) }

    let socketPath = root.appendingPathComponent("server.sock").path
    let manager = SocketManager(socketPath: socketPath)
    let delegate = EchoSocketManagerDelegate()
    manager.delegate = delegate
    try manager.setupSocket()
    let runner = SocketManagerRunner(manager: manager)
    DispatchQueue.global().async {
      runner.manager.startListening()
      runner.stopped.signal()
    }

    var firstFd: Int32 = -1
    var secondFd: Int32 = -1
    defer {
      if firstFd >= 0 { close(firstFd) }
      if secondFd >= 0 { close(secondFd) }
      manager.stop()
      XCTAssertEqual(runner.stopped.wait(timeout: .now() + 2), .success)
    }

    firstFd = try connectClient(socketPath)
    XCTAssertEqual(try transact(firstFd, payload: Data("first-1".utf8)), Data("first-1".utf8))

    secondFd = try connectClient(socketPath)
    XCTAssertEqual(try transact(secondFd, payload: Data("second".utf8)), Data("second".utf8))

    XCTAssertEqual(try transact(firstFd, payload: Data("first-2".utf8)), Data("first-2".utf8))
    XCTAssertEqual(delegate.connected.count, 2)
  }

  private func connectClient(_ path: String) throws -> Int32 {
    let fd = socket(AF_UNIX, Int32(SOCK_STREAM.rawValue), 0)
    guard fd >= 0 else {
      throw SocketError.readFailed("create test client socket", errno)
    }

    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    strncpy(&address.sun_path.0, path, MemoryLayout.size(ofValue: address.sun_path) - 1)
    let result = withUnsafePointer(to: &address) {
      $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        connect(fd, $0, socklen_t(MemoryLayout.size(ofValue: address)))
      }
    }
    guard result == 0 else {
      let code = errno
      close(fd)
      throw SocketError.readFailed("connect test client socket", code)
    }
    return fd
  }

  private func transact(_ fd: Int32, payload: Data) throws -> Data {
    var length = UInt32(payload.count).bigEndian
    try writeData(to: fd, data: withUnsafeBytes(of: &length) { Data($0) })
    try writeData(to: fd, data: payload)

    let header = try readData(from: fd, count: 4)
    let responseLength = header.withUnsafeBytes { $0.load(as: UInt32.self).bigEndian }
    return try readData(from: fd, count: Int(responseLength))
  }
}
