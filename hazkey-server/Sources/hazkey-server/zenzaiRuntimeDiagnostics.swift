import Foundation

enum ZenzaiRuntimeDecision: Equatable, Sendable {
    case enabled(modelURL: URL)
    case profileDisabled
    case policyDisabled
    case backendUnavailable
    case modelMissing

    var status: ZenzaiRuntimeDiagnosticsSnapshot.Status {
        switch self {
        case .enabled:
            .ready
        case .profileDisabled:
            .profileDisabled
        case .policyDisabled:
            .policyDisabled
        case .backendUnavailable:
            .backendUnavailable
        case .modelMissing:
            .modelMissing
        }
    }
}

struct ZenzaiRuntimeDiagnosticsSnapshot: Equatable, Sendable {
    enum Status: Equatable, Sendable {
        case ready
        case modelLoadVerified
        case profileDisabled
        case policyDisabled
        case backendUnavailable
        case modelMissing
        case modelLoadFailed
    }

    let status: Status
    let modelLoadVerified: Bool
    let zenzaiEnabledRequestCount: UInt64
    let modelLoadFailureCount: UInt64
    let lastZenzaiRequestUnixMillis: UInt64?
    let detail: String

    static func configurationOnly(
        decision: ZenzaiRuntimeDecision
    ) -> ZenzaiRuntimeDiagnosticsSnapshot {
        ZenzaiRuntimeDiagnosticsSnapshot(
            status: decision.status,
            modelLoadVerified: false,
            zenzaiEnabledRequestCount: 0,
            modelLoadFailureCount: 0,
            lastZenzaiRequestUnixMillis: nil,
            detail: ""
        )
    }

    var protobuf: Hazkey_Config_ZenzaiRuntimeDiagnostics {
        Hazkey_Config_ZenzaiRuntimeDiagnostics.with {
            switch status {
            case .ready:
                $0.status = .ready
            case .modelLoadVerified:
                $0.status = .modelLoadVerified
            case .profileDisabled:
                $0.status = .profileDisabled
            case .policyDisabled:
                $0.status = .policyDisabled
            case .backendUnavailable:
                $0.status = .backendUnavailable
            case .modelMissing:
                $0.status = .modelMissing
            case .modelLoadFailed:
                $0.status = .modelLoadFailed
            }
            $0.modelLoadVerified = modelLoadVerified
            $0.zenzaiEnabledRequestCount = zenzaiEnabledRequestCount
            $0.modelLoadFailureCount = modelLoadFailureCount
            if let lastZenzaiRequestUnixMillis {
                $0.lastZenzaiRequestUnixMillis = lastZenzaiRequestUnixMillis
            }
            $0.detail = detail
        }
    }
}

/// Tracks model loading for primary conversion requests across the server
/// lifetime (or until configuration/model reload resets the generation).
///
/// AzooKey currently exposes model loading through `zenzStatus`, but does not
/// expose each internal candidate evaluation. The counter therefore records
/// requests where a model URL was supplied, not model inference calls or
/// successful AI evaluations.
final class ZenzaiRuntimeDiagnosticsStore {
    private var lastDecision: ZenzaiRuntimeDecision?
    private var configurationModelURL: URL?
    private var lastStatus: ZenzaiRuntimeDiagnosticsSnapshot.Status = .ready
    private var modelLoadVerified = false
    private var zenzaiEnabledRequestCount: UInt64 = 0
    private var modelLoadFailureCount: UInt64 = 0
    private var lastZenzaiRequestUnixMillis: UInt64?
    private var detail = ""

    func reset(decision: ZenzaiRuntimeDecision) {
        lastDecision = decision
        configurationModelURL = if case .enabled(let modelURL) = decision {
            modelURL
        } else {
            nil
        }
        lastStatus = decision.status
        modelLoadVerified = false
        zenzaiEnabledRequestCount = 0
        modelLoadFailureCount = 0
        lastZenzaiRequestUnixMillis = nil
        detail = ""
    }

    func record(
        decision: ZenzaiRuntimeDecision,
        converterStatus: String,
        at date: Date = Date()
    ) {
        if case .enabled(let modelURL) = decision,
           configurationModelURL != modelURL {
            reset(decision: decision)
        }
        lastDecision = decision
        switch decision {
        case .enabled(let modelURL):
            zenzaiEnabledRequestCount &+= 1
            lastZenzaiRequestUnixMillis = UInt64(
                max(0, date.timeIntervalSince1970 * 1_000)
            )
            let expectedStatus = "load \(modelURL.absoluteString)"
            if converterStatus == expectedStatus {
                lastStatus = .modelLoadVerified
                modelLoadVerified = true
                detail = ""
            } else {
                lastStatus = .modelLoadFailed
                modelLoadVerified = false
                modelLoadFailureCount &+= 1
                detail = converterStatus.isEmpty
                    ? "The converter did not report a loaded Zenzai model."
                    : converterStatus
            }
        case .backendUnavailable, .modelMissing:
            lastStatus = decision.status
            modelLoadVerified = false
            detail = ""
        case .profileDisabled, .policyDisabled:
            lastStatus = decision.status
            modelLoadVerified = false
            detail = ""
        }
    }

    func snapshot() -> ZenzaiRuntimeDiagnosticsSnapshot {
        return ZenzaiRuntimeDiagnosticsSnapshot(
            status: lastStatus,
            modelLoadVerified: modelLoadVerified,
            zenzaiEnabledRequestCount: zenzaiEnabledRequestCount,
            modelLoadFailureCount: modelLoadFailureCount,
            lastZenzaiRequestUnixMillis: lastZenzaiRequestUnixMillis,
            detail: detail
        )
    }
}
