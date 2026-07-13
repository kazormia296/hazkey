import XCTest

@testable import hazkey_server

final class ZenzaiBackendDeviceSelectionTests: XCTestCase {
  private let cpu = ZenzaiBackendDeviceCandidate(name: "CPU", kind: .cpu)
  private let gpu = ZenzaiBackendDeviceCandidate(name: "Vulkan0", kind: .gpu)

  func testDefaultProfileUsesAutomaticBackendSelection() {
    XCTAssertEqual(
      HazkeyServerConfig.genDefaultConfig().zenzaiBackendDeviceName,
      ""
    )
  }

  func testAutomaticSelectionPrefersGPUOverCPU() {
    XCTAssertEqual(
      resolveZenzaiBackendDeviceName(
        configuredName: "",
        availableDevices: [cpu, gpu]
      ),
      "Vulkan0"
    )
  }

  func testAutomaticSelectionFallsBackToEnumeratedCPU() {
    let namedCPU = ZenzaiBackendDeviceCandidate(name: "CPU0", kind: .cpu)
    XCTAssertEqual(
      resolveZenzaiBackendDeviceName(
        configuredName: "",
        availableDevices: [namedCPU]
      ),
      "CPU0"
    )
    XCTAssertEqual(
      resolveZenzaiBackendDeviceName(
        configuredName: "",
        availableDevices: []
      ),
      "CPU"
    )
  }

  func testExplicitDeviceIsPreservedAndStaleDeviceFallsBackToCPU() {
    XCTAssertEqual(
      resolveZenzaiBackendDeviceName(
        configuredName: "CPU",
        availableDevices: [gpu, cpu]
      ),
      "CPU"
    )
    XCTAssertEqual(
      resolveZenzaiBackendDeviceName(
        configuredName: "Vulkan0",
        availableDevices: [gpu, cpu]
      ),
      "Vulkan0"
    )
    XCTAssertEqual(
      resolveZenzaiBackendDeviceName(
        configuredName: "Vulkan9",
        availableDevices: [gpu, cpu]
      ),
      "CPU"
    )
  }
}
