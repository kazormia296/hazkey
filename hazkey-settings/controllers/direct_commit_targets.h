#ifndef HAZKEY_SETTINGS_CONTROLLERS_DIRECT_COMMIT_TARGETS_H_
#define HAZKEY_SETTINGS_CONTROLLERS_DIRECT_COMMIT_TARGETS_H_

#include <cstdint>

namespace hazkey::settings {

inline constexpr uint32_t kPunctuationDirectCommitTargets = 0x0F;

constexpr bool hasPunctuationDirectCommitTarget(uint32_t targets) {
    return (targets & kPunctuationDirectCommitTargets) != 0;
}

constexpr uint32_t withPunctuationDirectCommitEnabled(uint32_t targets,
                                                       bool enabled) {
    if (enabled) {
        // The checkbox represents the punctuation group. Preserve a partial
        // mask when it was already enabled; only a newly enabled group gains
        // all four standard punctuation targets.
        return hasPunctuationDirectCommitTarget(targets)
                   ? targets
                   : targets | kPunctuationDirectCommitTargets;
    }
    // Unknown/future target bits do not belong to this checkbox.
    return targets & ~kPunctuationDirectCommitTargets;
}

}  // namespace hazkey::settings

#endif  // HAZKEY_SETTINGS_CONTROLLERS_DIRECT_COMMIT_TARGETS_H_
