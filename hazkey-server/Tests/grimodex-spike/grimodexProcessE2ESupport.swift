import Foundation
import SwiftProtobuf

#if canImport(Glibc)
import Glibc
#endif

@testable import hazkey_server

enum GrimodexProcessE2EError: Error, CustomStringConvertible {
  case invalidResponse(String)
  case processExited(status: Int32, log: String)
  case socketFailure(String, errno: Int32)
  case timeout(String)

  var description: String {
    switch self {
    case .invalidResponse(let message):
      return message
    case .processExited(let status, let log):
      return "Grimodex server exited with status \(status):\n\(log)"
    case .socketFailure(let operation, let number):
      return "\(operation) failed with errno \(number)"
    case .timeout(let operation):
      return "Timed out while \(operation)"
    }
  }
}

final class GrimodexProcessSnapshotFixture {
  let sandboxURL: URL
  let rootURL: URL

  private var projectsURL: URL {
    rootURL.appendingPathComponent("projects", isDirectory: true)
  }

  init() throws {
    sandboxURL = FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-process-snapshot-\(UUID().uuidString)",
      isDirectory: true
    )
    rootURL = sandboxURL.appendingPathComponent("ime", isDirectory: true)
    try FileManager.default.createDirectory(
      at: projectsURL,
      withIntermediateDirectories: true
    )
  }

  func publish(projectID: String, surface: String) throws {
    let project: [String: Any] = [
      "format_version": 1,
      "project_id": projectID,
      "project_name": projectID,
      "generated_at": "2026-07-12T00:00:00.000Z",
      "entries": [
        [
          "yomi": "せつな",
          "surface": surface,
          "category": "person",
          "priority": 3,
          "entry_id": "term-\(projectID)",
        ]
      ],
    ]
    try writeJSON(
      project,
      to: projectsURL.appendingPathComponent("\(projectID).json")
    )
    try publishState(projectID: projectID)
  }

  func removeState() throws {
    let stateURL = rootURL.appendingPathComponent("state.json")
    if FileManager.default.fileExists(atPath: stateURL.path) {
      try FileManager.default.removeItem(at: stateURL)
    }
  }

  func publishInvalidProject(projectID: String) throws {
    try Data("{ this is not valid JSON".utf8).write(
      to: projectsURL.appendingPathComponent("\(projectID).json"),
      options: .atomic
    )
    try publishState(projectID: projectID)
  }

  func remove() {
    try? FileManager.default.removeItem(at: sandboxURL)
  }

  private func publishState(projectID: String) throws {
    try writeJSON(
      [
        "format_version": 1,
        "active_project_id": projectID,
        "updated_at": "2026-07-12T00:00:00.000Z",
      ],
      to: rootURL.appendingPathComponent("state.json")
    )
  }

  private func writeJSON(_ object: Any, to url: URL) throws {
    let data = try JSONSerialization.data(
      withJSONObject: object,
      options: [.prettyPrinted, .sortedKeys]
    )
    try data.write(to: url, options: .atomic)
  }
}

final class GrimodexProcessHarness {
  let socketURL: URL

  private let executableURL: URL
  private let grimodexRootURL: URL
  private let sandboxURL: URL
  private let logURL: URL
  private let process = Process()
  private var launched = false
  private var logHandle: FileHandle?

  var isRunning: Bool { launched && process.isRunning }

  init(executableURL: URL, grimodexRootURL: URL) {
    self.executableURL = executableURL
    self.grimodexRootURL = grimodexRootURL
    sandboxURL = FileManager.default.temporaryDirectory.appendingPathComponent(
      "grimodex-process-server-\(UUID().uuidString)",
      isDirectory: true
    )
    let runtimeURL = sandboxURL.appendingPathComponent("runtime", isDirectory: true)
    socketURL = runtimeURL
      .appendingPathComponent(GrimodexProductPaths.packageName, isDirectory: true)
      .appendingPathComponent("server.sock")
    logURL = sandboxURL.appendingPathComponent("server.log")
  }

  func start(timeout: TimeInterval = 15) throws {
    let fileManager = FileManager.default
    try fileManager.createDirectory(at: sandboxURL, withIntermediateDirectories: true)
    let homeURL = sandboxURL.appendingPathComponent("home", isDirectory: true)
    let runtimeURL = sandboxURL.appendingPathComponent("runtime", isDirectory: true)
    try fileManager.createDirectory(at: homeURL, withIntermediateDirectories: true)
    try fileManager.createDirectory(
      at: runtimeURL,
      withIntermediateDirectories: true,
      attributes: [.posixPermissions: 0o700]
    )
    try fileManager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: runtimeURL.path)
    _ = fileManager.createFile(atPath: logURL.path, contents: nil)
    let handle = try FileHandle(forWritingTo: logURL)
    logHandle = handle

    var environment = ProcessInfo.processInfo.environment
    environment["HOME"] = homeURL.path
    environment["GRIMODEX_IME_ROOT"] = grimodexRootURL.path
    environment["XDG_RUNTIME_DIR"] = runtimeURL.path
    environment["XDG_CONFIG_HOME"] = sandboxURL.appendingPathComponent("config").path
    environment["XDG_DATA_HOME"] = sandboxURL.appendingPathComponent("data").path
    environment["XDG_STATE_HOME"] = sandboxURL.appendingPathComponent("state").path
    environment["XDG_CACHE_HOME"] = sandboxURL.appendingPathComponent("cache").path
    if environment["FCITX5_GRIMODEX_DICTIONARY"] == nil {
      let sourceDictionary = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("azooKey_dictionary_storage/Dictionary", isDirectory: true)
      if fileManager.fileExists(atPath: sourceDictionary.path) {
        environment["FCITX5_GRIMODEX_DICTIONARY"] = sourceDictionary.path
      }
    }

    process.executableURL = executableURL
    process.environment = environment
    process.standardOutput = handle
    process.standardError = handle
    do {
      try process.run()
      launched = true
      try waitForSocket(timeout: timeout)
    } catch {
      stop()
      throw error
    }
  }

  func stop() {
    if launched, process.isRunning {
      process.terminate()
      let deadline = Date().addingTimeInterval(3)
      while process.isRunning && Date() < deadline {
        usleep(25_000)
      }
      if process.isRunning {
        _ = kill(process.processIdentifier, SIGKILL)
      }
    }
    if launched {
      process.waitUntilExit()
    }
    try? logHandle?.close()
    logHandle = nil
    try? FileManager.default.removeItem(at: sandboxURL)
  }

  func assertPrivateIPC() throws {
    let runtimeURL = socketURL.deletingLastPathComponent()
    let runtimeAttributes = try FileManager.default.attributesOfItem(
      atPath: runtimeURL.path
    )
    let socketAttributes = try FileManager.default.attributesOfItem(
      atPath: socketURL.path
    )
    guard
      let runtimeMode = runtimeAttributes[.posixPermissions] as? NSNumber,
      runtimeMode.intValue & 0o777 == 0o700
    else {
      throw GrimodexProcessE2EError.invalidResponse(
        "real server runtime directory is not mode 0700"
      )
    }
    guard
      let socketMode = socketAttributes[.posixPermissions] as? NSNumber,
      socketMode.intValue & 0o777 == 0o600
    else {
      throw GrimodexProcessE2EError.invalidResponse(
        "real server socket is not mode 0600"
      )
    }
  }

  private func waitForSocket(timeout: TimeInterval) throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
      if FileManager.default.fileExists(atPath: socketURL.path) {
        return
      }
      if !process.isRunning {
        try? logHandle?.synchronize()
        let log = (try? String(contentsOf: logURL, encoding: .utf8)) ?? ""
        throw GrimodexProcessE2EError.processExited(
          status: process.terminationStatus,
          log: log
        )
      }
      usleep(25_000)
    }
    throw GrimodexProcessE2EError.timeout("waiting for the real server socket")
  }
}

final class GrimodexProcessClient {
  private static let maximumFrameBytes = 1024 * 1024
  private static let transactionTimeout: TimeInterval = 10

  private var fileDescriptor: Int32
  private var revisions: [String: UInt64] = [:]
  private var snapshots: [String: Hazkey_SessionSnapshot] = [:]

  private init(fileDescriptor: Int32) {
    self.fileDescriptor = fileDescriptor
  }

  static func connect(to socketURL: URL, timeout: TimeInterval = 5) throws
    -> GrimodexProcessClient
  {
    let deadline = Date().addingTimeInterval(timeout)
    var lastErrno: Int32 = 0
    repeat {
      let fd = socket(AF_UNIX, Int32(SOCK_STREAM.rawValue), 0)
      guard fd >= 0 else {
        throw GrimodexProcessE2EError.socketFailure("socket", errno: errno)
      }
      var address = sockaddr_un()
      address.sun_family = sa_family_t(AF_UNIX)
      _ = socketURL.path.withCString { source in
        strncpy(
          &address.sun_path.0,
          source,
          MemoryLayout.size(ofValue: address.sun_path) - 1
        )
      }
      let result = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
          Glibc.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
      }
      if result == 0 {
        let flags = fcntl(fd, F_GETFL, 0)
        guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 else {
          let number = errno
          Glibc.close(fd)
          throw GrimodexProcessE2EError.socketFailure("fcntl", errno: number)
        }
        return GrimodexProcessClient(fileDescriptor: fd)
      }
      lastErrno = errno
      Glibc.close(fd)
      usleep(25_000)
    } while Date() < deadline
    throw GrimodexProcessE2EError.socketFailure("connect", errno: lastErrno)
  }

  func close() {
    if fileDescriptor >= 0 {
      Glibc.close(fileDescriptor)
      fileDescriptor = -1
    }
  }

  func openSession(program: String) throws -> String {
    try openSessionInfo(program: program).sessionID
  }

  func openSessionInfo(program: String) throws -> Hazkey_OpenSessionResult {
    let response = try transact(
      Hazkey_RequestEnvelope.with {
        $0.openSession = Hazkey_OpenSession.with {
          $0.client = Hazkey_ClientContext.with {
            $0.program = program
            $0.frontend = "wayland"
            $0.secureInput = false
          }
        }
      }
    )
    try requireSuccess(response, operation: "open session")
    guard !response.openSessionResult.sessionID.isEmpty else {
      throw GrimodexProcessE2EError.invalidResponse("Open session returned an empty ID")
    }
    revisions[response.openSessionResult.sessionID] = 0
    snapshots[response.openSessionResult.sessionID] = Hazkey_SessionSnapshot.with {
      $0.phase = .idle
    }
    return response.openSessionResult
  }

  func transactV2(
    sessionID: String,
    requestID: String,
    expectedRevision: UInt64,
    configure: (inout Hazkey_Commands_HandleImeAction) -> Void
  ) throws -> Hazkey_ResponseEnvelope {
    var action = Hazkey_Commands_HandleImeAction()
    action.requestID = requestID
    action.expectedRevision = expectedRevision
    configure(&action)
    let response = try transact(
      sessionRequest(sessionID) { $0.handleImeAction = action }
    )
    if case .handleImeActionResult(let result)? = response.payload,
       result.hasSnapshot {
      let snapshot = result.snapshot
      revisions[sessionID] = snapshot.revision
      snapshots[sessionID] = snapshot
    }
    return response
  }

  func addUserDictionaryEntry(
    id: String,
    reading: String,
    surface: String,
    partOfSpeech: String = "noun",
    layer: Hazkey_Config_UserDictionaryLayer = .personal
  ) throws -> Hazkey_Config_UserDictionaryResult {
    let response = try transact(
      Hazkey_RequestEnvelope.with {
        $0.addUserDictionaryEntry = Hazkey_Config_AddUserDictionaryEntry.with {
          $0.entry = Hazkey_Config_UserDictionaryEntry.with {
            $0.id = id
            $0.reading = reading
            $0.surface = surface
            $0.partOfSpeech = partOfSpeech
            $0.layer = layer
          }
        }
      }
    )
    try requireSuccess(response, operation: "add user dictionary entry")
    return response.userDictionaryResult
  }

  func convertDirect(_ text: String, sessionID: String) throws -> [String] {
    for attempt in 0..<3 {
      guard snapshots[sessionID]?.phase != .idle else { break }
      let response = try transactV2(
        sessionID: sessionID,
        requestID: "process-reset-\(attempt)-\(UUID().uuidString)",
        expectedRevision: revisions[sessionID] ?? 0
      ) {
        $0.cancel = .init()
      }
      try requireSuccess(response, operation: "reset v2 composition")
    }
    let inserted = try transactV2(
      sessionID: sessionID,
      requestID: "process-insert-\(UUID().uuidString)",
      expectedRevision: revisions[sessionID] ?? 0
    ) {
      $0.insertText = Hazkey_Commands_InsertText.with { $0.text = text }
    }
    try requireSuccess(inserted, operation: "v2 direct input")
    let converted = try transactV2(
      sessionID: sessionID,
      requestID: "process-convert-\(UUID().uuidString)",
      expectedRevision: revisions[sessionID] ?? 0
    ) {
      $0.startConversion = .init()
    }
    try requireSuccess(converted, operation: "v2 conversion")
    return converted.handleImeActionResult.snapshot.candidateWindow.items.map(\.text)
  }

  func candidates(sessionID: String) throws -> [String] {
    guard let snapshot = snapshots[sessionID] else {
      throw GrimodexProcessE2EError.invalidResponse(
        "No confirmed v2 snapshot exists for session"
      )
    }
    return snapshot.candidateWindow.items.map(\.text)
  }

  func setScope(_ mode: Hazkey_Config_Profile.GrimodexScopeMode) throws {
    let current = try getConfig()
    guard !current.profiles.isEmpty else {
      throw GrimodexProcessE2EError.invalidResponse("Configuration contains no profile")
    }
    var profiles = current.profiles
    profiles[0].grimodexScopeMode = mode
    let response = try transact(
      Hazkey_RequestEnvelope.with {
        $0.setConfig = Hazkey_Config_SetConfig.with {
          $0.profiles = profiles
        }
      }
    )
    try requireSuccess(response, operation: "set Grimodex scope")
  }

  func waitForDiagnostics(
    timeout: TimeInterval = 8,
    matching predicate: (Hazkey_Config_GrimodexDiagnostics) -> Bool
  ) throws -> Hazkey_Config_GrimodexDiagnostics {
    let deadline = Date().addingTimeInterval(timeout)
    var latest = Hazkey_Config_GrimodexDiagnostics()
    repeat {
      let config = try getConfig()
      latest = config.grimodexDiagnostics
      if config.hasGrimodexDiagnostics, predicate(latest) {
        return latest
      }
      usleep(50_000)
    } while Date() < deadline
    throw GrimodexProcessE2EError.timeout(
      "waiting for diagnostics; latest status was \(latest.snapshotStatus)"
    )
  }

  func zenzaiRuntimeDiagnostics() throws
    -> Hazkey_Config_ZenzaiRuntimeDiagnostics
  {
    let config = try getConfig()
    guard config.hasZenzaiRuntimeDiagnostics else {
      throw GrimodexProcessE2EError.invalidResponse(
        "Configuration does not contain Zenzai runtime diagnostics"
      )
    }
    return config.zenzaiRuntimeDiagnostics
  }

  private func getConfig() throws -> Hazkey_Config_CurrentConfig {
    let response = try transact(
      Hazkey_RequestEnvelope.with { $0.getConfig = .init() }
    )
    try requireSuccess(response, operation: "get configuration")
    return response.currentConfig
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

  private func requireSuccess(
    _ response: Hazkey_ResponseEnvelope,
    operation: String
  ) throws {
    guard response.status == .success else {
      throw GrimodexProcessE2EError.invalidResponse(
        "\(operation) failed: \(response.status) \(response.errorMessage)"
      )
    }
  }

  private func transact(_ request: Hazkey_RequestEnvelope) throws
    -> Hazkey_ResponseEnvelope
  {
    let requestData = try request.serializedData()
    guard requestData.count <= Self.maximumFrameBytes else {
      throw GrimodexProcessE2EError.invalidResponse("Request frame is too large")
    }
    var networkLength = UInt32(requestData.count).bigEndian
    let header = withUnsafeBytes(of: &networkLength) { Data($0) }
    let deadline = Date().addingTimeInterval(Self.transactionTimeout)
    try writeAll(header, deadline: deadline)
    try writeAll(requestData, deadline: deadline)

    let responseHeader = try readExactly(4, deadline: deadline)
    let responseLength = responseHeader.reduce(UInt32(0)) {
      ($0 << 8) | UInt32($1)
    }
    guard responseLength <= UInt32(Self.maximumFrameBytes) else {
      throw GrimodexProcessE2EError.invalidResponse("Response frame is too large")
    }
    let responseData = try readExactly(Int(responseLength), deadline: deadline)
    return try Hazkey_ResponseEnvelope(serializedBytes: responseData)
  }

  private func writeAll(_ data: Data, deadline: Date) throws {
    var offset = 0
    try data.withUnsafeBytes { bytes in
      while offset < bytes.count {
        try waitForSocket(events: Int16(POLLOUT), deadline: deadline)
        let count = Glibc.write(
          fileDescriptor,
          bytes.baseAddress?.advanced(by: offset),
          bytes.count - offset
        )
        if count > 0 {
          offset += count
        } else if count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR {
          throw GrimodexProcessE2EError.socketFailure("write", errno: errno)
        }
      }
    }
  }

  private func readExactly(_ count: Int, deadline: Date) throws -> Data {
    var result = [UInt8](repeating: 0, count: count)
    var offset = 0
    try result.withUnsafeMutableBytes { bytes in
      while offset < count {
        try waitForSocket(events: Int16(POLLIN), deadline: deadline)
        let readCount = Glibc.read(
          fileDescriptor,
          bytes.baseAddress?.advanced(by: offset),
          count - offset
        )
        if readCount > 0 {
          offset += readCount
        } else if readCount == 0 {
          throw GrimodexProcessE2EError.invalidResponse(
            "Server disconnected during a response"
          )
        } else if errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR {
          throw GrimodexProcessE2EError.socketFailure("read", errno: errno)
        }
      }
    }
    return Data(result)
  }

  private func waitForSocket(events: Int16, deadline: Date) throws {
    while true {
      let remaining = deadline.timeIntervalSinceNow
      guard remaining > 0 else {
        throw GrimodexProcessE2EError.timeout("waiting for a socket transaction")
      }
      var descriptor = pollfd(fd: fileDescriptor, events: events, revents: 0)
      let result = poll(
        &descriptor,
        1,
        Int32(min(remaining * 1_000, Double(Int32.max)))
      )
      if result > 0 {
        let failureEvents = Int16(POLLERR) | Int16(POLLHUP) | Int16(POLLNVAL)
        if descriptor.revents & failureEvents != 0 {
          throw GrimodexProcessE2EError.invalidResponse("Socket closed during transaction")
        }
        if descriptor.revents & events != 0 {
          return
        }
      } else if result == 0 {
        throw GrimodexProcessE2EError.timeout("waiting for a socket transaction")
      } else if errno != EINTR {
        throw GrimodexProcessE2EError.socketFailure("poll", errno: errno)
      }
    }
  }
}
