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

private final class AsyncSocketResult: @unchecked Sendable {
  private let lock = NSLock()
  private var stored: Result<Data, Error>?

  func store(_ result: Result<Data, Error>) {
    lock.lock()
    stored = result
    lock.unlock()
  }

  func get() -> Result<Data, Error>? {
    lock.lock()
    defer { lock.unlock() }
    return stored
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

  func testPartialFrameCannotBlockAnotherClient() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-partial-frame-tests-\(UUID().uuidString)",
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

    var slowFd: Int32 = -1
    var healthyFd: Int32 = -1
    defer {
      if slowFd >= 0 { close(slowFd) }
      if healthyFd >= 0 { close(healthyFd) }
      manager.stop()
      XCTAssertEqual(runner.stopped.wait(timeout: .now() + 2), .success)
    }

    slowFd = try connectClient(socketPath)
    var slowLength = UInt32(4).bigEndian
    let slowHeader = withUnsafeBytes(of: &slowLength) { Data($0) }
    XCTAssertEqual(slowHeader.prefix(2).withUnsafeBytes { write(slowFd, $0.baseAddress, 2) }, 2)

    healthyFd = try connectClient(socketPath)
    let completed = DispatchSemaphore(value: 0)
    let result = AsyncSocketResult()
    let fd = healthyFd
    DispatchQueue.global().async { [self] in
      result.store(Result { try transact(fd, payload: Data("healthy".utf8)) })
      completed.signal()
    }

    XCTAssertEqual(
      completed.wait(timeout: .now() + 1),
      .success,
      "a client that stalls mid-frame must not block unrelated clients"
    )
    XCTAssertEqual(try result.get()?.get(), Data("healthy".utf8))

    XCTAssertEqual(
      slowHeader.dropFirst(2).withUnsafeBytes { write(slowFd, $0.baseAddress, 2) },
      2
    )
    XCTAssertEqual(Data("slow".utf8).withUnsafeBytes { write(slowFd, $0.baseAddress, 4) }, 4)
    let slowResponseHeader = try readData(from: slowFd, count: 4)
    let slowResponseLength = slowResponseHeader.withUnsafeBytes {
      $0.load(as: UInt32.self).bigEndian
    }
    XCTAssertEqual(try readData(from: slowFd, count: Int(slowResponseLength)), Data("slow".utf8))
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
